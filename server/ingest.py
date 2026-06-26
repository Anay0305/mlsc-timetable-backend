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
) -> dict[str, object]:
    """Parse `xlsx_path` and upsert results into Mongo. Returns a small summary."""
    settings = settings or get_settings()
    xlsx_path = Path(xlsx_path).resolve()
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Spreadsheet not found: {xlsx_path}")

    logger.info("Loading workbook: %s (sheet=%s)", xlsx_path, sheet)
    workbook = load_workbook(xlsx_path, data_only=True)
    sheets = _select_sheets(workbook.worksheets, workbook.active, sheet)
    if not sheets:
        raise ValueError(f"No worksheets matched selector {sheet!r}")

    catalog = load_default_subject_catalog()

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

    payloads = class_blocks_to_api(merged, semester_label)
    batches = sorted(merged.keys())

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
    summary = {
        "batches": len(batches),
        "classes": total_classes,
        "sheets_used": used_sheets,
        "multi_sheet_batches": [
            {"batch": code, "sheets": sheets}
            for code, sheets in sorted(multi_sheet.items())
        ],
        "doctor": doctor,
    }
    logger.info(
        "Ingest complete: %d batches, %d classes (sheets=%s, multi-sheet=%d, mismatched-groups=%d)",
        summary["batches"], summary["classes"], used_sheets,
        len(multi_sheet), doctor["mismatched_groups"],
    )
    return summary


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
