"""End-to-end ingest: spreadsheet path + semester label → MongoDB.

Sheet selection (`sheet` arg):

    "all"            iterate every worksheet (default)
    "active"         only `workbook.active`
    "<title>"        exact title match (whitespace tolerated)
    "@<n>"           sheet by 0-based index
    "<glob>"         fnmatch-style title pattern (e.g. ``"1ST YEAR *"``)

When the same batch code appears in multiple sheets (common for elective
groups like ``3O11`` that are scheduled identically for CSE-A, CSE-B, ECE,
etc.) the blocks are merged and deduplicated rather than overwritten.
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from server import storage
from server.config import Settings, get_settings
from server.doctor import build_doctor_report, count_classes
from timetable_parser.core.models import ClassBlock
from timetable_parser.core.subject_catalog import load_default_subject_catalog
from timetable_parser.extractors.class_blocks import ClassBlockExtractor
from timetable_parser.serializers.api import class_blocks_to_api

logger = logging.getLogger(__name__)


def _block_dedup_key(block: ClassBlock) -> tuple:
    """Identity tuple used to deduplicate blocks merged from multiple sheets."""
    return (
        block.day,
        block.start_slot,
        block.periods,
        block.subject_code,
        block.type,
        block.block_kind,
    )


def _merge_day_blocks(
    existing: dict[str, list[ClassBlock]],
    incoming: dict[str, list[ClassBlock]],
) -> dict[str, list[ClassBlock]]:
    """Combine two day → blocks maps, deduplicating identical entries."""
    merged: dict[str, list[ClassBlock]] = {day: list(blocks) for day, blocks in existing.items()}
    for day, blocks in incoming.items():
        bucket = merged.setdefault(day, [])
        seen = {_block_dedup_key(b) for b in bucket}
        for b in blocks:
            key = _block_dedup_key(b)
            if key in seen:
                continue
            bucket.append(b)
            seen.add(key)
    return merged


async def parse_workbook(
    xlsx_path: Path,
    semester_label: str,
    settings: Settings | None = None,
    *,
    sheet: str = "all",
    actor_kind: str | None = None,
    actor_email: str | None = None,
    record_attempt: bool = True,
) -> dict[str, object]:
    """Parse `xlsx_path` and upsert results into Mongo. Returns a small summary."""
    settings = settings or get_settings()
    xlsx_path = Path(xlsx_path).resolve()
    started_at = datetime.now(timezone.utc)
    filename = xlsx_path.name

    if not xlsx_path.exists():
        if record_attempt:
            await storage.record_upload_attempt({
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc),
                "actor_kind": actor_kind,
                "actor_email": actor_email,
                "filename": filename,
                "sheet_selector": sheet,
                "semester_label": semester_label,
                "status": "failed",
                "failure_message": f"Spreadsheet not found: {xlsx_path}",
            })
        raise FileNotFoundError(f"Spreadsheet not found: {xlsx_path}")

    try:
        logger.info("Loading workbook: %s (sheet=%s)", xlsx_path, sheet)
        workbook = load_workbook(xlsx_path, data_only=True)
        sheets = _select_sheets(workbook.worksheets, workbook.active, sheet)
        if not sheets:
            raise ValueError(f"No worksheets matched selector {sheet!r}")

        catalog = load_default_subject_catalog()
        if not catalog.subjects:
            # Cold cache (first call after invalidate); warm it from Mongo.
            from timetable_parser.core.subject_catalog import ensure_catalog
            catalog = await ensure_catalog()

        merged: dict[str, dict[str, list[ClassBlock]]] = {}
        # First sheet that contributed each batch (kept as the primary source_sheet
        # in storage). The full list of contributors lives in `sheets_by_code`.
        sheet_by_code: dict[str, str] = {}
        sheets_by_code: dict[str, list[str]] = {}
        used_sheets: list[str] = []

        for ws in sheets:
            title = ws.title.strip()
            try:
                blocks_by_batch_day = ClassBlockExtractor.extract(ws, subject_catalog=catalog)
            except Exception:  # parser is best-effort across heterogeneous sheets
                logger.exception("Skipping sheet %r: extractor failed", title)
                continue
            if not blocks_by_batch_day:
                logger.info("Sheet %r contributed no batches", title)
                continue
            used_sheets.append(title)
            for code, day_blocks in blocks_by_batch_day.items():
                if code in merged:
                    before = sum(len(v) for v in merged[code].values())
                    merged[code] = _merge_day_blocks(merged[code], day_blocks)
                    after = sum(len(v) for v in merged[code].values())
                    added = after - before
                    if added:
                        logger.info(
                            "Batch %s: merged %d block(s) from %r (total %d)",
                            code, added, title, after,
                        )
                    contributors = sheets_by_code.setdefault(code, [sheet_by_code[code]])
                    if title not in contributors:
                        contributors.append(title)
                else:
                    merged[code] = day_blocks
                    sheet_by_code[code] = title
                    sheets_by_code[code] = [title]

        multi_sheet = {c: ss for c, ss in sheets_by_code.items() if len(ss) > 1}
        multi_sheet_list = [
            {"batch": code, "sheets": ss} for code, ss in sorted(multi_sheet.items())
        ]

        confidence_summary, error_rows = _summarize_blocks(merged, sheet_by_code)

        payloads = class_blocks_to_api(merged, semester_label)
        batches = sorted(merged.keys())

        # Snapshot the current state BEFORE we mutate anything, so a rollback
        # can undo this run. We only do this when there's at least one batch
        # to write — empty runs aren't worth snapshotting over (and would
        # destroy the previous good state's recoverability).
        if batches:
            try:
                snap_info = await storage.save_ingest_snapshot(settings=settings)
                logger.info(
                    "Pre-ingest snapshot saved: %d batches, %d timetables (expires %s)",
                    snap_info.get("batches"), snap_info.get("timetables"),
                    snap_info.get("expires_at"),
                )
            except Exception:
                logger.exception("Pre-ingest snapshot failed — continuing without rollback")

        await storage.write_current({"label": semester_label}, settings=settings)
        await storage.write_batch_list(batches, settings=settings, sheet_by_code=sheet_by_code)
        for code, payload in payloads.items():
            await storage.write_timetable(
                code,
                payload,
                settings=settings,
                source_sheet=sheet_by_code.get(code),
                source_file=xlsx_path.name,
            )
        # Prune ghost timetables (codes that survived from a previous ingest
        # but aren't in the new spreadsheet).
        try:
            pruned = await storage.replace_timetables(list(payloads.keys()))
            if pruned:
                logger.info("Pruned %d stale timetable(s) not in current ingest", pruned)
        except Exception:
            logger.exception("Stale-timetable prune failed (non-fatal)")

        total_classes = sum(len(payload["classes"]) for payload in payloads.values())
        storage.maybe_git_commit(
            f"ingest: {xlsx_path.name} ({len(batches)} batches, {total_classes} classes)",
            settings=settings,
        )

        prefix = storage.semester_prefix(semester_label)
        baselines = await storage.read_baselines_for_prefix(prefix, settings=settings)
        doctor = build_doctor_report(
            {code: count_classes(payload["classes"]) for code, payload in payloads.items()},
            baselines_by_group=baselines,
            semester_prefix=prefix,
        )

        total_blocks = sum(confidence_summary.values())
        attempt_status: str = "ok"
        if not batches:
            attempt_status = "failed"
        elif error_rows or doctor.get("mismatched_groups", 0) > 0:
            attempt_status = "partial"

        summary: dict[str, object] = {
            "batches": len(batches),
            "classes": total_classes,
            "sheets_used": used_sheets,
            "multi_sheet_batches": multi_sheet_list,
            "doctor": doctor,
            "total_blocks": total_blocks,
            "confidence_summary": confidence_summary,
            "error_count": len(error_rows),
        }
        logger.info(
            "Ingest complete: %d batches, %d classes (sheets=%s, multi-sheet=%d, "
            "mismatched-groups=%d, parser-errors=%d)",
            summary["batches"], summary["classes"], used_sheets,
            len(multi_sheet), doctor["mismatched_groups"], len(error_rows),
        )

        if record_attempt:
            recorded = await storage.record_upload_attempt({
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc),
                "actor_kind": actor_kind,
                "actor_email": actor_email,
                "filename": filename,
                "sheet_selector": sheet,
                "semester_label": semester_label,
                "status": attempt_status,
                "batches_written": len(batches),
                "classes_written": total_classes,
                "sheets_used": used_sheets,
                "multi_sheet_batches": multi_sheet_list,
                "total_blocks": total_blocks,
                "confidence_summary": confidence_summary,
                "doctor": doctor,
            })
            if recorded.get("id"):
                summary["attempt_id"] = recorded["id"]
            summary["status"] = attempt_status

            # Persist the parser warnings + doctor mismatches into the
            # ParsingErrorDoc collection so the admin Fix tab can list,
            # filter, and resolve them. Best-effort: never let this fail
            # the ingest summary.
            try:
                written = await storage.save_parsing_errors(
                    upload_id=recorded.get("id"),
                    error_rows=list(error_rows),
                    doctor=doctor,
                )
                summary["errors_persisted"] = written
            except Exception:
                logger.exception("save_parsing_errors failed (non-fatal)")

        return summary
    except Exception as exc:
        if record_attempt:
            await storage.record_upload_attempt({
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc),
                "actor_kind": actor_kind,
                "actor_email": actor_email,
                "filename": filename,
                "sheet_selector": sheet,
                "semester_label": semester_label,
                "status": "failed",
                "failure_message": f"{type(exc).__name__}: {exc}",
            })
        raise


def _summarize_blocks(
    merged: dict[str, dict[str, list[ClassBlock]]],
    sheet_by_code: dict[str, str],
) -> tuple[dict[str, int], list[dict[str, object]]]:
    """Tally confidence levels and collect per-reason error rows."""
    summary: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNRELIABLE": 0}
    errors: list[dict[str, object]] = []
    for code, day_blocks in merged.items():
        sheet_title = sheet_by_code.get(code)
        for day, blocks in day_blocks.items():
            for block in blocks:
                level = (block.confidence or "MEDIUM").upper()
                summary[level] = summary.get(level, 0) + 1
                if level in {"HIGH"}:
                    continue
                for reason in (block.confidence_reasons or ()):  # type: ignore[truthy-iterable]
                    errors.append({
                        "batch": code,
                        "sheet": sheet_title,
                        "day": day,
                        "start_time": block.start_time,
                        "severity": level,
                        "code": str(getattr(reason, "code", "UNKNOWN")),
                        "message": getattr(reason, "detail", "") or "",
                    })
    return summary, errors


def _select_sheets(
    worksheets: list[Worksheet],
    active: Worksheet,
    selector: str,
) -> list[Worksheet]:
    selector = (selector or "all").strip()
    if not selector or selector.lower() == "all":
        # Skip sheets the workbook author hid (often stale duplicates / drafts).
        return [ws for ws in worksheets if ws.sheet_state == "visible"]
    if selector.lower() == "active":
        return [active]
    if selector.startswith("@"):
        try:
            idx = int(selector[1:])
        except ValueError as exc:
            raise ValueError(f"invalid sheet index {selector!r}") from exc
        if idx < 0 or idx >= len(worksheets):
            raise ValueError(f"sheet index {idx} out of range (0..{len(worksheets) - 1})")
        return [worksheets[idx]]

    needle = selector.strip().lower()
    # Exact match (whitespace tolerated)
    exact = [ws for ws in worksheets if ws.title.strip().lower() == needle]
    if exact:
        return exact
    # Glob match
    glob = [ws for ws in worksheets if fnmatch.fnmatchcase(ws.title.strip().lower(), needle)]
    return glob
