"""MongoDB-backed data store (seam for the HTTP layer).

All routers go through this module. Beanie documents live in
:mod:`server.db.models`; this file exposes async helpers that return plain
dicts/lists in the exact shape the frontend expects, so the API contract is
independent of the underlying ODM.

Optional `JSON_MIRROR=1` toggles disk snapshots into ``data/`` for audit /
git-tracking — same layout the previous JSON store used.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from server.config import Settings, get_settings
from server.db.models import (
    AdminEmailDoc,
    AnnouncementDoc,
    BaselineCourseCheck,
    BaselineDoc,
    BatchDoc,
    CalendarOverrideDoc,
    ChangeRequestDoc,
    ContributorDoc,
    ExamDateDoc,
    IngestSnapshotDoc,
    ParsingErrorDoc,
    SemesterDoc,
    SubjectDoc,
    TimetableDoc,
    UploadAttemptDoc,
)

logger = logging.getLogger(__name__)


class BatchNotFound(Exception):
    def __init__(self, batch: str) -> None:
        super().__init__(f"Batch not found: {batch}")
        self.batch = batch


class DataMissing(Exception):
    """Raised when expected baseline data (semester label, batch list) is absent."""


# ── Reads ────────────────────────────────────────────────────────────────
async def read_batch_list(settings: Settings | None = None) -> list[str]:
    codes = [doc.code async for doc in BatchDoc.find_all(sort=[("code", 1)])]
    if not codes:
        # Fall back to whatever timetables exist, so the API isn't empty before
        # the BatchDoc collection has been seeded.
        codes = sorted({doc.code async for doc in TimetableDoc.find_all()})
    return codes


async def read_current(settings: Settings | None = None) -> dict[str, Any]:
    doc = await SemesterDoc.find_one(SemesterDoc.key == "current")
    if doc is None:
        raise DataMissing("no current semester set (PUT /admin/current)")
    result: dict[str, Any] = {"label": doc.label}
    if doc.term_end_dates:
        result["term_end_dates"] = doc.term_end_dates
    if doc.term_start_dates:
        result["term_start_dates"] = doc.term_start_dates
    return result


async def read_timetable(batch: str, settings: Settings | None = None) -> dict[str, Any]:
    code = _safe_batch(batch)
    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        raise BatchNotFound(batch)
    # Resolve any stripped subject names from the live catalog so the
    # response shape stays compatible with every existing client.
    from timetable_parser.core.subject_catalog import ensure_catalog
    catalog = await ensure_catalog()
    payload = _timetable_payload(doc, catalog)
    try:
        current = await read_current(settings=settings)
        year = str(code[0]) if code and code[0].isdigit() else "1"
        payload["term_start_date"] = (current.get("term_start_dates") or {}).get(year)
    except DataMissing:
        payload["term_start_date"] = None
    return payload


# ── Writes ───────────────────────────────────────────────────────────────
async def write_batch_list(
    batches: list[str],
    settings: Settings | None = None,
    *,
    sheet_by_code: dict[str, str] | None = None,
) -> None:
    """Replace the batch directory.

    Adds new codes, updates `source_sheet` on existing ones, and removes codes
    no longer present in `batches`.
    """
    settings = settings or get_settings()
    codes = sorted({str(b) for b in batches})
    sheet_by_code = sheet_by_code or {}
    now = datetime.now(timezone.utc)

    existing = {doc.code: doc async for doc in BatchDoc.find_all()}
    for code in codes:
        meta = _derive_batch_meta(code)
        doc = existing.pop(code, None)
        if doc is None:
            await BatchDoc(
                code=code,
                year=meta["year"],
                section=meta["section"],
                source_sheet=sheet_by_code.get(code),
            ).insert()
        else:
            updates: dict[str, Any] = {"updated_at": now}
            if sheet_by_code.get(code) and doc.source_sheet != sheet_by_code.get(code):
                updates["source_sheet"] = sheet_by_code.get(code)
            if meta["year"] != doc.year:
                updates["year"] = meta["year"]
            if meta["section"] != doc.section:
                updates["section"] = meta["section"]
            await doc.set(updates)
    for stale in existing.values():
        await stale.delete()

    if settings.json_mirror:
        _mirror_json(settings.data_dir / "batch.json", codes)


async def write_current(payload: dict[str, Any], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("payload must include a non-empty 'label' string")
    label = label.strip()
    # Baselines are keyed by an E/O prefix (see semester_prefix), so the label
    # must clearly indicate which parity it is. Anything else silently breaks
    # doctor lookups and produces false "no baseline" advisories.
    head = label.upper()
    if not (head.startswith("EVEN") or head.startswith("ODD")
            or head.startswith("E ") or head.startswith("O ")):
        raise ValueError(
            f"invalid semester label {label!r}: must start with 'EVEN' or 'ODD' "
            "(e.g. 'EVEN 25-26', 'ODD 2025')"
        )
    doc = await SemesterDoc.find_one(SemesterDoc.key == "current")
    updates: dict[str, Any] = {"label": label, "updated_at": datetime.now(timezone.utc)}
    if doc is None:
        await SemesterDoc(key="current", label=label).insert()
    else:
        await doc.set(updates)

    if settings.json_mirror:
        _mirror_json(settings.data_dir / "current.json", {"label": label})


async def write_term_end_dates(dates: dict[str, str]) -> None:
    """Patch only the ``term_end_dates`` field on the current SemesterDoc.

    ``dates`` is a dict keyed by UG year string (``"1"``..``"4"``).
    Silently no-ops if no semester doc exists yet.
    """
    doc = await SemesterDoc.find_one(SemesterDoc.key == "current")
    if doc is None:
        return
    await doc.set({"term_end_dates": dates, "updated_at": datetime.now(timezone.utc)})


async def write_term_start_dates(dates: dict[str, str]) -> None:
    """Patch only the ``term_start_dates`` field on the current SemesterDoc.

    ``dates`` is a dict keyed by UG year string (``"1"``..``"4"``).
    Silently no-ops if no semester doc exists yet.
    """
    doc = await SemesterDoc.find_one(SemesterDoc.key == "current")
    if doc is None:
        return
    await doc.set({"term_start_dates": dates, "updated_at": datetime.now(timezone.utc)})


async def write_timetable(
    batch: str,
    payload: dict[str, Any],
    settings: Settings | None = None,
    *,
    source_sheet: str | None = None,
    source_file: str | None = None,
) -> None:
    settings = settings or get_settings()
    code = _safe_batch(batch)
    semester_obj = payload.get("semester") or {}
    semester_label = (
        semester_obj.get("label") if isinstance(semester_obj, dict) else None
    )
    if not isinstance(semester_label, str) or not semester_label.strip():
        raise ValueError("payload.semester.label must be a non-empty string")
    classes = payload.get("classes") or []
    if not isinstance(classes, list):
        raise ValueError("payload.classes must be a list")

    # Strip subject names that match the catalog default so we don't store
    # the same string thousands of times. ``ensure_catalog`` is cheap when
    # the in-process snapshot is warm.
    from timetable_parser.core.subject_catalog import ensure_catalog
    catalog = await ensure_catalog()
    classes = _normalize_classes_for_write(classes, catalog)

    source = {
        "sheet": source_sheet,
        "file": source_file,
        "ingested_at": datetime.now(timezone.utc),
    }

    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        await TimetableDoc(
            code=code,
            semester=semester_label,
            classes=classes,
            source=source,
        ).insert()
    else:
        await doc.set({
            "semester": semester_label,
            "classes": classes,
            "source": source,
            "updated_at": datetime.now(timezone.utc),
        })

    if settings.json_mirror:
        path = settings.data_dir / "timetable" / f"{code}.json"
        _mirror_json(path, _timetable_payload_from_raw(code, semester_label, classes, catalog))


async def delete_timetable(batch: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    code = _safe_batch(batch)
    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        return False
    await doc.delete()
    if settings.json_mirror:
        path = settings.data_dir / "timetable" / f"{code}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return True


# ── Baselines ────────────────────────────────────────────────────────────
def semester_prefix(label: str | None) -> str:
    """Derive an ``E``/``O`` prefix from a semester label like ``"EVEN 25-26"``."""
    if not label:
        return "?"
    s = label.strip().upper()
    if s.startswith("EVEN") or s.startswith("E "):
        return "E"
    if s.startswith("ODD") or s.startswith("O "):
        return "O"
    return s[:1] if s else "?"


_BASELINE_KEY = re.compile(r"^[EO][1-9][A-Z]$")


def _safe_baseline_key(key: str) -> str:
    cleaned = "".join(ch for ch in (key or "").strip().upper() if ch.isalnum())
    if not _BASELINE_KEY.match(cleaned):
        raise ValueError(
            f"invalid baseline key {key!r}: expected '{{E|O}}{{year}}{{stream}}' e.g. 'E1A'"
        )
    return cleaned


def _baseline_payload(doc: BaselineDoc) -> dict[str, Any]:
    counts = dict(doc.counts)
    total = sum(int(v) for v in counts.values() if isinstance(v, int))
    courses = [
        (c.model_dump() if isinstance(c, BaselineCourseCheck) else dict(c))
        for c in (doc.courses or [])
    ]
    option_groups = [
        [c.model_dump() if isinstance(c, BaselineCourseCheck) else dict(c) for c in group]
        for group in (doc.option_groups or [])
    ]
    return {
        "key": doc.key,
        "semester_prefix": doc.semester_prefix,
        "group": doc.group,
        "counts": counts,
        "total": total,
        "courses": courses,
        "course_count": len(courses),
        "elective_count": doc.elective_count or 0,
        "option_groups": option_groups,
        "scheme_source": doc.scheme_source,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


def _build_baselines_query(
    q: str | None = None,
    parity: str | None = None,
    year: str | None = None,
    stream: str | None = None,
) -> dict[str, Any]:
    regex_pattern = "^"
    if parity and parity != "all":
        regex_pattern += re.escape(parity)
    else:
        regex_pattern += "[EO]"

    if year and year != "all":
        regex_pattern += re.escape(str(year))
    else:
        regex_pattern += r"\d+"

    if stream and stream != "all":
        regex_pattern += re.escape(stream)
    else:
        regex_pattern += "[A-Z]+"

    regex_pattern += "$"

    query: dict[str, Any] = {"key": {"$regex": regex_pattern, "$options": "i"}}
    if q:
        query = {"$and": [
            {"key": {"$regex": regex_pattern, "$options": "i"}},
            {"key": {"$regex": re.escape(q), "$options": "i"}}
        ]}
    return query


async def list_baselines(
    settings: Settings | None = None,
    *,
    q: str | None = None,
    parity: str | None = None,
    year: str | None = None,
    stream: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = _build_baselines_query(q, parity, year, stream)
    docs = BaselineDoc.find(query, sort=[("key", 1)]).skip(offset).limit(limit)
    return [_baseline_payload(doc) async for doc in docs]


async def count_baselines(
    q: str | None = None,
    parity: str | None = None,
    year: str | None = None,
    stream: str | None = None,
) -> int:
    query = _build_baselines_query(q, parity, year, stream)
    return await BaselineDoc.find(query).count()


async def read_baseline(key: str, settings: Settings | None = None) -> dict[str, Any]:
    cleaned = _safe_baseline_key(key)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned)
    if doc is None:
        raise DataMissing(f"no baseline for {cleaned}")
    return _baseline_payload(doc)


def _clean_courses(courses: Any) -> list[BaselineCourseCheck]:
    """Coerce a client-provided list of course dicts into `BaselineCourseCheck`s.

    Silently trims empty rows, but raises ValueError on anything malformed
    (non-list input, non-string fields).
    """
    if courses is None:
        return []
    if not isinstance(courses, list):
        raise ValueError("courses must be a list of objects")
    cleaned: list[BaselineCourseCheck] = []
    seen_codes: set[str] = set()
    for i, raw in enumerate(courses):
        if isinstance(raw, BaselineCourseCheck):
            course = raw
        elif isinstance(raw, dict):
            allowed = {"code", "title", "category", "L", "T", "P", "Cr", "alternate_weeks"}
            picked = {k: raw.get(k) for k in allowed if raw.get(k) not in (None, "")}
            if not picked:
                continue  # skip completely blank rows
            for field_name, value in list(picked.items()):
                if not isinstance(value, str):
                    if field_name == "alternate_weeks":
                        picked[field_name] = [str(v) for v in value] if isinstance(value, list) else []
                    else:
                        picked[field_name] = str(value)
            if isinstance(picked.get("title"), str):
                title = re.sub(r"\s*\*+\s*", " ", picked["title"]).strip()
                if title.isupper():
                    acronyms = {"AI", "API", "C++", "IOT", "UCS"}
                    title = " ".join(
                        word if word in acronyms else word.lower().capitalize()
                        for word in title.split()
                    )
                picked["title"] = title
            course = BaselineCourseCheck(**picked)
        else:
            raise ValueError(f"courses[{i}] must be an object, got {type(raw).__name__}")
        # de-dupe by code (case-insensitive); keep first occurrence
        code_key = (course.code or "").strip().upper()
        if code_key and code_key in seen_codes:
            continue
        if code_key:
            seen_codes.add(code_key)
        cleaned.append(course)
    return cleaned


async def write_baseline(
    key: str,
    counts: dict[str, int],
    settings: Settings | None = None,
    *,
    courses: Any = None,
    option_groups: Any = None,
    elective_count: int = 0,
    scheme_source: str | None = None,
    merge_courses: bool = False,
) -> dict[str, Any]:
    cleaned_key = _safe_baseline_key(key)
    if not isinstance(counts, dict) or not counts:
        raise ValueError("counts must be a non-empty mapping of type → integer")
    cleaned_counts: dict[str, int] = {}
    for type_name, value in counts.items():
        if not isinstance(type_name, str) or not type_name.strip():
            raise ValueError(f"invalid type name: {type_name!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"counts[{type_name!r}] must be a non-negative int, got {value!r}")
        cleaned_counts[type_name.strip()] = value
    if cleaned_key in {"total"}:  # paranoid: never let a reserved key sneak in
        raise ValueError("'total' is a reserved key")
    if "total" in cleaned_counts:
        raise ValueError("'total' is reserved; it is derived from the sum of the other counts")

    parsed_courses = _clean_courses(courses) if courses is not None else None
    parsed_groups = (
        [_clean_courses(group) for group in option_groups]
        if isinstance(option_groups, list) else None
    )

    prefix = cleaned_key[0]
    group = cleaned_key[1:]
    now = datetime.now(timezone.utc)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    if doc is None:
        doc = BaselineDoc(
            key=cleaned_key,
            semester_prefix=prefix,
            group=group,
            counts=cleaned_counts,
            courses=parsed_courses or [],
            elective_count=max(0, int(elective_count or 0)),
            option_groups=parsed_groups or [],
            scheme_source=scheme_source,
        )
        await doc.insert()
    else:
        update: dict[str, Any] = {"counts": cleaned_counts, "updated_at": now}
        if parsed_courses is not None:
            if merge_courses and doc.courses:
                by_code: dict[str, BaselineCourseCheck] = {}
                for existing in doc.courses:
                    key_ = (existing.code or existing.title or "").strip().upper()
                    if key_:
                        by_code[key_] = existing
                for incoming in parsed_courses:
                    key_ = (incoming.code or incoming.title or "").strip().upper()
                    by_code[key_ or str(len(by_code))] = incoming
                update["courses"] = list(by_code.values())
            else:
                update["courses"] = parsed_courses
        if parsed_groups is not None:
            update["option_groups"] = parsed_groups
        update["elective_count"] = max(0, int(elective_count or 0))
        if scheme_source is not None:
            update["scheme_source"] = scheme_source
        await doc.set(update)
        doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    return _baseline_payload(doc)


async def delete_baseline(key: str, settings: Settings | None = None) -> bool:
    cleaned = _safe_baseline_key(key)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned)
    if doc is None:
        return False
    await doc.delete()
    return True


async def backfill_baseline_counts(settings: Settings | None = None) -> dict[str, Any]:
    """For every baseline whose ``counts`` dict is empty, derive counts from
    the stored course L/T/P columns and write them back.

    Safe to run multiple times — docs that already have explicit counts are
    left untouched.  Returns ``{updated: N, skipped: M}`` where *skipped* is
    the number of docs that already had counts or had no courses to derive from.
    """
    updated = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    async for doc in BaselineDoc.find_all():
        if doc.counts:          # already has explicit counts — leave it alone
            skipped += 1
            continue
        if not doc.courses:     # nothing to derive from
            skipped += 1
            continue
        derived = _counts_from_courses(doc.courses)
        if not derived:         # all L/T/P were blank / zero
            skipped += 1
            continue
        await doc.set({"counts": derived, "updated_at": now})
        updated += 1
    return {"updated": updated, "skipped": skipped}


def _numeric_str(value: str | None) -> int:
    """Parse a credit-hour string like '3' or '1.5' to an int (rounds down)."""
    if not value:
        return 0
    try:
        return int(sum(float(token) for token in re.findall(r"\d+(?:\.\d+)?", str(value))))
    except (ValueError, TypeError):
        return 0


def _counts_from_courses(courses: list[BaselineCourseCheck]) -> dict[str, int]:
    """Derive {Lecture, Tutorial, Practical} weekly-count totals by summing the
    L/T/P credit-hour columns across all courses in the roster.

    Used both here (when a scheme PDF creates a baseline) and mirrored in
    ``BaselinesPage.jsx::countsFromCourses`` for the UI fallback display.
    """
    lecture = tutorial = practical = 0
    for c in courses:
        lecture += _numeric_str(c.L)
        tutorial += _numeric_str(c.T)
        practical += _numeric_str(c.P)
    out: dict[str, int] = {}
    if lecture:
        out["Lecture"] = lecture
    if tutorial:
        out["Tutorial"] = tutorial
    if practical:
        out["Practical"] = practical
    return out


async def upsert_baseline_courses(
    key: str,
    courses: Any,
    *,
    option_groups: Any = None,
    elective_count: int = 0,
    scheme_source: str | None = None,
    merge: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Set the ``courses`` roster for a baseline, deriving ``counts`` from the
    course L/T/P columns when none are configured yet.

    Both the manual form and the scheme-PDF flow now produce the same schema:
    ``counts`` always holds the per-type expected class counts, sourced from
    either the admin's explicit input or the summed course contact hours.
    """
    cleaned_key = _safe_baseline_key(key)
    prefix = cleaned_key[0]
    group = cleaned_key[1:]
    parsed_courses = _clean_courses(courses)
    parsed_groups = [_clean_courses(group) for group in (option_groups or [])] if isinstance(option_groups, list) else []
    # Derive counts from the course L/T/P columns so the schema is uniform
    # regardless of whether the admin used the manual form or a PDF upload.
    derived_counts = _counts_from_courses(parsed_courses)
    now = datetime.now(timezone.utc)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    if doc is None:
        doc = BaselineDoc(
            key=cleaned_key,
            semester_prefix=prefix,
            group=group,
            counts=derived_counts,  # populated, not empty
            courses=parsed_courses,
            elective_count=max(0, int(elective_count or 0)),
            option_groups=parsed_groups,
            scheme_source=scheme_source,
        )
        await doc.insert()
    else:
        update: dict[str, Any] = {"updated_at": now}
        if merge and doc.courses:
            by_code: dict[str, BaselineCourseCheck] = {}
            for existing in doc.courses:
                key_ = (existing.code or existing.title or "").strip().upper()
                if key_:
                    by_code[key_] = existing
            for incoming in parsed_courses:
                key_ = (incoming.code or incoming.title or "").strip().upper()
                by_code[key_ or str(len(by_code))] = incoming
            update["courses"] = list(by_code.values())
        else:
            update["courses"] = parsed_courses
        update["elective_count"] = max(0, int(elective_count or 0))
        update["option_groups"] = parsed_groups
        # If the baseline has no explicit counts yet, derive them from the
        # incoming courses so the schema stays uniform (same as on create).
        if not doc.counts:
            update["counts"] = _counts_from_courses(parsed_courses)
        if scheme_source is not None:
            update["scheme_source"] = scheme_source
        await doc.set(update)
        doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    return _baseline_payload(doc)


async def check_baseline_group(
    key: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the doctor for a single baseline group, refresh its error rows, and
    return a summary.

    Finds every ``TimetableDoc`` whose batch code belongs to the group (first
    two chars of the code equal the baseline's ``group`` field), compares
    against the baseline's counts, writes the results as ``ParsingErrorDoc``
    rows (clearing any existing open rows for that group first), and returns
    a compact result dict.
    """
    from server.doctor import build_doctor_report, codes_in, count_classes

    cleaned_key = _safe_baseline_key(key)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    if doc is None:
        raise DataMissing(f"no baseline for {cleaned_key}")

    group = doc.group   # e.g. "1A"
    prefix = doc.semester_prefix

    # Collect all timetables belonging to this group.
    counts_by_batch: dict[str, dict[str, int]] = {}
    codes_by_batch: dict[str, set[str]] = {}
    async for tt in TimetableDoc.find_all():
        code = tt.code
        if len(code) >= 2 and code[:2].upper() == group.upper():
            raw = getattr(tt, "classes", None) or []
            classes: list[dict[str, Any]] = [
                c.model_dump() if hasattr(c, "model_dump") else c
                for c in raw
                if isinstance(c, dict) or hasattr(c, "model_dump")
            ]
            counts_by_batch[code] = count_classes(classes)
            codes_by_batch[code] = codes_in(classes)

    if not counts_by_batch:
        return {
            "status": "no_timetables",
            "group": group,
            "baseline_key": cleaned_key,
            "batches": 0,
            "written": 0,
            "deleted": 0,
        }

    baselines_map = {group: dict(doc.counts)}
    course_rows = [
        c.model_dump(exclude_none=False) if hasattr(c, "model_dump") else dict(c)
        for c in (doc.courses or [])
        if (getattr(c, "code", None) or (c.get("code") if isinstance(c, dict) else ""))
    ]
    courses_map = {group: course_rows} if course_rows else {}

    report = build_doctor_report(
        counts_by_batch,
        baselines_by_group=baselines_map,
        semester_prefix=prefix,
        codes_by_batch=codes_by_batch,
        courses_by_group=courses_map,
    )

    # Wipe open error rows that belong to this group so a re-check is
    # idempotent — stale rows from a previous ingest or check are cleared.
    stale_types = ["BASELINE_MISMATCH", "BASELINE_MISSING", "doctor_mismatch"]
    batch_codes = list(counts_by_batch.keys())
    coll = ParsingErrorDoc.get_motor_collection()
    del_res = await coll.delete_many({
        "error_type": {"$in": stale_types},
        "status": "open",
        "$or": [
            {"batch_code": {"$in": batch_codes}},
            {"context.group": group},
        ],
    })
    deleted = del_res.deleted_count

    written = await save_parsing_errors(upload_id=None, error_rows=[], doctor=report)

    # Pull out the result for this specific group.
    all_entries = (report.get("ok") or []) + (report.get("mismatches") or [])
    group_entry = next((g for g in all_entries if g.get("group") == group), None)

    has_mismatch = bool(report.get("mismatches"))
    has_no_baseline = bool(report.get("no_baseline"))

    return {
        "status": "no_baseline" if has_no_baseline else ("mismatch" if has_mismatch else "ok"),
        "group": group,
        "baseline_key": cleaned_key,
        "batches": len(counts_by_batch),
        "mismatched_groups": report.get("mismatched_groups", 0),
        "written": written,
        "deleted": deleted,
        "result": group_entry,
    }


async def read_baselines_for_prefix(
    prefix: str,
    settings: Settings | None = None,
) -> dict[str, dict[str, int]]:
    """Return ``{group: counts}`` for the given semester prefix (e.g. ``"E"``)."""
    prefix = (prefix or "").strip().upper()[:1]
    if not prefix:
        return {}
    out: dict[str, dict[str, int]] = {}
    async for doc in BaselineDoc.find(BaselineDoc.semester_prefix == prefix):
        out[doc.group] = dict(doc.counts)
    return out


async def read_baseline_courses_for_prefix(
    prefix: str,
    settings: Settings | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{group: [expected_course_code, …]}`` for a semester prefix.

    Codes are uppercased and de-duplicated; placeholder rows without a code
    (e.g. ``ELECTIVE-II``) are skipped so the doctor doesn't complain about
    them missing from the timetable.
    """
    prefix = (prefix or "").strip().upper()[:1]
    if not prefix:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    async for doc in BaselineDoc.find(BaselineDoc.semester_prefix == prefix):
        courses: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in (doc.courses or []):
            code = (c.code or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            courses.append(c.model_dump(exclude_none=False) if hasattr(c, "model_dump") else dict(c))
        if courses:
            out[doc.group] = courses
    return out


async def apply_baseline_alternate_weeks(
    payloads: dict[str, dict[str, Any]],
    semester_label: str,
) -> int:
    """Apply baseline ``*`` L/T/P markers to matching timetable sections.

    Baseline markers do not specify an initial parity, so they default to week
    1. Explicit timetable metadata is preserved and wins over the baseline.
    """
    prefix = semester_prefix(semester_label)
    baselines = await read_baseline_courses_for_prefix(prefix)
    updated = 0
    for batch, payload in payloads.items():
        group = batch[1:3].upper() if len(batch) >= 3 else ""
        courses = baselines.get(group) or []
        marked = {
            str(c.get("code") or "").strip().upper(): {str(v).upper() for v in (c.get("alternate_weeks") or [])}
            for c in courses
        }
        for entry in payload.get("classes") or []:
            if entry.get("alternate_week_start"):
                continue
            code = str(entry.get("code") or "").strip().upper()
            section = code[-1:] if code and code[-1:] in {"L", "T", "P"} else ""
            base = code[:-1] if section else code
            if section and section in marked.get(base, set()):
                entry["alternate_week_start"] = 1
                updated += 1
    return updated


# ── Contributors ─────────────────────────────────────────────────────────
_GITHUB_USERNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,38})$")


def _safe_username(username: str) -> str:
    cleaned = (username or "").strip().lstrip("@")
    if not _GITHUB_USERNAME.match(cleaned):
        raise ValueError(
            f"invalid GitHub username {username!r}: alphanumeric + hyphen, 1-39 chars, "
            "cannot start with a hyphen"
        )
    if cleaned.endswith("-") or "--" in cleaned:
        raise ValueError(f"invalid GitHub username {username!r}")
    return cleaned


async def list_contributors(settings: Settings | None = None, *, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
    docs = ContributorDoc.find_all(sort=[("username", 1)]).skip(offset).limit(limit)
    return [
        {
            "username": doc.username,
            "display_name": doc.display_name,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
        }
        async for doc in docs
    ]


async def count_contributors() -> int:
    return await ContributorDoc.find_all().count()


async def list_contributor_usernames(settings: Settings | None = None) -> list[str]:
    return [doc.username async for doc in ContributorDoc.find_all(sort=[("username", 1)])]


async def add_contributor(
    username: str,
    display_name: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    cleaned = _safe_username(username)
    existing = await ContributorDoc.find_one(ContributorDoc.username == cleaned)
    if existing is not None:
        updates: dict[str, Any] = {}
        if display_name is not None and display_name != existing.display_name:
            updates["display_name"] = display_name or None
        if updates:
            await existing.set(updates)
            existing = await ContributorDoc.find_one(ContributorDoc.username == cleaned)
        return {
            "username": existing.username,
            "display_name": existing.display_name,
            "created_at": existing.created_at.isoformat() if existing.created_at else None,
        }
    doc = ContributorDoc(username=cleaned, display_name=display_name or None)
    await doc.insert()
    return {
        "username": doc.username,
        "display_name": doc.display_name,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


async def delete_contributor(username: str, settings: Settings | None = None) -> bool:
    cleaned = _safe_username(username)
    doc = await ContributorDoc.find_one(ContributorDoc.username == cleaned)
    if doc is None:
        return False
    await doc.delete()
    return True


# ── Overrides ──────────────────────────────────────────────────────
_BATCH_CODE = re.compile(r"^[A-Z0-9]{2,16}$")
_DAY_PATTERN = re.compile(r"^[A-Za-z]{3,12}$")
# Reasonable upper bound; storage layer rejects floods even if the HTTP rate
# limiter is bypassed/misconfigured. Keep tight — admin-reviewed queue.
MAX_PENDING_PER_REQUESTER = 20
MAX_PENDING_PER_BATCH = 100
MAX_PENDING_TOTAL = 1000
# Reject identical pending requests (same batch+slot+kind+entry signature) so
# a refresh-bug or accidental double-submit doesn't fan out the queue.


class ChangeRequestRefused(Exception):
    """Storage refused an override (validation, quota, duplicate)."""

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


def _safe_class_prefix(batch_code: str) -> str:
    """First-3-char prefix of a batch (e.g. ``"1B12"`` -> ``"1B1"``)."""
    cleaned = _safe_batch(batch_code)
    if len(cleaned) < 3:
        raise ChangeRequestRefused(
            f"batch {batch_code!r} is too short to derive a class prefix",
            code="bad_batch",
        )
    return cleaned[:3]


def _resolve_scope_batches(scope: str, requester_batch: str) -> list[str]:
    """Return the list of canonical batches an approved request should touch."""
    requester = _safe_batch(requester_batch)
    if scope == "batch":
        return [requester]
    if scope == "class":
        prefix = _safe_class_prefix(requester)
        return [prefix]  # placeholder; resolved against actual batches in approval
    raise ChangeRequestRefused(f"unknown scope {scope!r}", code="bad_scope")


async def get_existing_entry_for_slot(batch: str, day: str, start_time: str) -> dict[str, Any] | None:
    try:
        code = _safe_batch(batch)
        doc = await TimetableDoc.find_one(TimetableDoc.code == code)
        if doc is None or not doc.schedule:
            return None
        entries = doc.schedule.get(day) or []
        for e in entries:
            st = getattr(e, "start_time", None) if not isinstance(e, dict) else e.get("start_time")
            if st == start_time:
                return _serialize_class(e) if not isinstance(e, dict) else e
    except Exception:
        pass
    return None


def _serialize_change_request(doc) -> dict[str, Any]:
    existing = getattr(doc, "existing_entry", None)
    if existing is not None and not isinstance(existing, dict):
        existing = _serialize_class(existing)
    payload: dict[str, Any] = {
        "id": str(doc.id),
        "requester_id": doc.requester_id,
        "requester_email": getattr(doc, "requester_email", None),
        "requester_batch": doc.requester_batch,
        "semester": doc.semester,
        "scope": doc.scope,
        "kind": doc.kind,
        "day": doc.day,
        "start_time": doc.start_time,
        "entry": _serialize_class(doc.entry) if doc.entry is not None else None,
        "existing_entry": existing,
        "status": doc.status,
        "decision_note": doc.decision_note,
        "decided_by": doc.decided_by,
        "decided_at": doc.decided_at.isoformat() if doc.decided_at else None,
        "applied_batches": list(doc.applied_batches or []),
        "created_at": doc.created_at.isoformat(),
    }
    return payload


async def create_change_request(
    *,
    requester_batch: str,
    scope: str,
    kind: str,
    day: str,
    start_time: str,
    entry: dict[str, Any] | None = None,
    requester_id: str | None = None,
    requester_email: str | None = None,
) -> dict[str, Any]:
    """Validate + insert a pending ChangeRequestDoc.

    Raises ChangeRequestRefused with a stable ``code`` on quota / dupe / shape
    problems so the HTTP layer can map it to 400 / 409 cleanly.
    """
    if kind not in {"add", "edit", "delete"}:
        raise ChangeRequestRefused(f"unknown kind {kind!r}", code="bad_kind")
    if scope not in {"batch", "class"}:
        raise ChangeRequestRefused(f"unknown scope {scope!r}", code="bad_scope")
    if not isinstance(day, str) or not _DAY_PATTERN.match(day.strip()):
        raise ChangeRequestRefused("invalid day", code="bad_day")
    if not isinstance(start_time, str) or not start_time.strip():
        raise ChangeRequestRefused("missing start_time", code="bad_slot")

    requester_batch_safe = _safe_batch(requester_batch)
    if scope == "class":
        # Derive (and validate) the class prefix upfront so a malformed batch
        # is rejected at create time, not surfaced later during approval.
        _safe_class_prefix(requester_batch_safe)

    if kind in {"add", "edit"} and not isinstance(entry, dict):
        raise ChangeRequestRefused(
            f"{kind} requires an 'entry' object",
            code="missing_entry",
        )
    if kind == "delete" and entry is not None:
        # Be forgiving but ignore caller-provided entry on delete; storage
        # treats delete as a slot wipe regardless.
        entry = None

    # Class-scope is only allowed for Lecture changes (matches frontend
    # business rule: lab/tutorial sectioning may differ per batch).
    if scope == "class" and kind in {"add", "edit"}:
        entry_type = (entry or {}).get("type")
        if entry_type != "Lecture":
            raise ChangeRequestRefused(
                "class scope is only allowed for Lecture entries",
                code="scope_requires_lecture",
            )

    # Resolve current semester so the request is anchored to the live one
    # regardless of when the admin reviews it.
    current = await read_current()
    semester_label = current["label"]

    # Server-side flood guards (extra to HTTP rate limiter).
    if requester_id:
        per_user = await ChangeRequestDoc.find(
            ChangeRequestDoc.requester_id == requester_id,
            ChangeRequestDoc.status == "pending",
        ).count()
        if per_user >= MAX_PENDING_PER_REQUESTER:
            raise ChangeRequestRefused(
                "too many pending requests from this user",
                code="quota_user",
            )
    per_batch = await ChangeRequestDoc.find(
        ChangeRequestDoc.requester_batch == requester_batch_safe,
        ChangeRequestDoc.status == "pending",
    ).count()
    if per_batch >= MAX_PENDING_PER_BATCH:
        raise ChangeRequestRefused(
            "too many pending requests for this batch",
            code="quota_batch",
        )
    total = await ChangeRequestDoc.find(
        ChangeRequestDoc.status == "pending"
    ).count()
    if total >= MAX_PENDING_TOTAL:
        raise ChangeRequestRefused(
            "override queue is full; try again later",
            code="quota_global",
        )

    # Dupe guard: identical pending request for same (batch, scope, slot, kind)
    dupe = await ChangeRequestDoc.find_one(
        ChangeRequestDoc.requester_batch == requester_batch_safe,
        ChangeRequestDoc.scope == scope,
        ChangeRequestDoc.kind == kind,
        ChangeRequestDoc.day == day,
        ChangeRequestDoc.start_time == start_time,
        ChangeRequestDoc.status == "pending",
    )
    if dupe is not None:
        raise ChangeRequestRefused(
            "an identical pending request already exists",
            code="duplicate",
        )

    # Look up existing entry in slot for before/after comparison
    existing_entry = await get_existing_entry_for_slot(requester_batch_safe, day.strip(), start_time.strip())

    doc = ChangeRequestDoc(
        requester_id=requester_id,
        requester_email=requester_email,
        requester_batch=requester_batch_safe,
        semester=semester_label,
        scope=scope,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        day=day.strip(),
        start_time=start_time.strip(),
        entry=entry,  # type: ignore[arg-type]
        existing_entry=existing_entry,
    )
    await doc.insert()
    return _serialize_change_request(doc)


async def list_change_requests(
    *,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if status is not None and status not in {"pending", "approved", "rejected"}:
        raise ValueError(f"invalid status {status!r}")
    query = ChangeRequestDoc.find_all(sort=[("created_at", -1)]).skip(offset).limit(limit)
    if status:
        query = ChangeRequestDoc.find(ChangeRequestDoc.status == status, sort=[("created_at", -1)]).skip(offset).limit(limit)
    out: list[dict[str, Any]] = []
    async for doc in query:
        serialized = _serialize_change_request(doc)
        if serialized.get("existing_entry") is None:
            serialized["existing_entry"] = await get_existing_entry_for_slot(doc.requester_batch, doc.day, doc.start_time)
        out.append(serialized)
        if len(out) >= limit:
            break
    return out
            break
    return out


async def count_change_requests(*, status: str | None = None) -> int:
    if status:
        return await ChangeRequestDoc.find(ChangeRequestDoc.status == status).count()
    return await ChangeRequestDoc.find_all().count()


async def _resolve_target_batches(scope: str, requester_batch: str) -> list[str]:
    """Expand scope into the actual list of batch codes to mutate."""
    if scope == "batch":
        return [_safe_batch(requester_batch)]
    prefix = _safe_class_prefix(requester_batch)
    codes: list[str] = []
    async for doc in BatchDoc.find_all(sort=[("code", 1)]):
        if doc.code.startswith(prefix):
            codes.append(doc.code)
    if not codes:
        # Fall back to timetables so an unseeded BatchDoc collection still works.
        seen: set[str] = set()
        async for tt in TimetableDoc.find_all(sort=[("code", 1)]):
            if tt.code.startswith(prefix) and tt.code not in seen:
                seen.add(tt.code)
                codes.append(tt.code)
    return codes


def _apply_change_to_classes(
    classes: list[Any],
    *,
    kind: str,
    day: str,
    start_time: str,
    entry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Pure function: apply add/edit/delete to a list of class entries."""
    out: list[dict[str, Any]] = []
    target_key = (day, start_time)
    replaced = False
    for c in classes:
        c_dict = _serialize_class(c)
        if (c_dict.get("day"), c_dict.get("start_time")) == target_key:
            if kind == "delete":
                continue
            if kind == "edit":
                merged = {**c_dict, **(entry or {})}
                merged["day"] = day
                merged["start_time"] = start_time
                out.append(merged)
                replaced = True
                continue
        out.append(c_dict)
    if kind == "add" and entry is not None:
        new_entry = {**entry, "day": day, "start_time": start_time}
        out.append(new_entry)
    elif kind == "edit" and not replaced and entry is not None:
        # Slot was empty in the canonical data — treat the edit as an add.
        new_entry = {**entry, "day": day, "start_time": start_time}
        out.append(new_entry)
    return out


async def approve_change_request(
    request_id: str,
    *,
    decided_by: str | None = None,
    decision_note: str | None = None,
) -> dict[str, Any]:
    from bson import ObjectId  # local import: only needed by admin path

    try:
        oid = ObjectId(request_id)
    except Exception as exc:  # noqa: BLE001 — any bson decode error
        raise ChangeRequestRefused("invalid request id", code="bad_id") from exc

    doc = await ChangeRequestDoc.get(oid)
    if doc is None:
        raise ChangeRequestRefused("override not found", code="not_found")
    if doc.status != "pending":
        raise ChangeRequestRefused(
            f"request already {doc.status}",
            code="not_pending",
        )

    targets = await _resolve_target_batches(doc.scope, doc.requester_batch)
    if not targets:
        raise ChangeRequestRefused(
            "no matching batches found in scope",
            code="empty_scope",
        )

    entry_payload = (
        doc.entry.model_dump(exclude_none=False) if doc.entry is not None else None
    )
    applied: list[str] = []
    for code in targets:
        tt = await TimetableDoc.find_one(TimetableDoc.code == code)
        if tt is None:
            continue
        new_classes = _apply_change_to_classes(
            tt.classes,
            kind=doc.kind,
            day=doc.day,
            start_time=doc.start_time,
            entry=entry_payload,
        )
        await tt.set({
            "classes": new_classes,
            "updated_at": datetime.now(timezone.utc),
        })
        applied.append(code)
        settings = get_settings()
        if settings.json_mirror:
            path = settings.data_dir / "timetable" / f"{code}.json"
            _mirror_json(
                path,
                _timetable_payload_from_raw(code, tt.semester, new_classes),
            )

    if not applied:
        raise ChangeRequestRefused(
            "no canonical timetables found for resolved batches",
            code="empty_targets",
        )

    await doc.set({
        "status": "approved",
        "decided_by": decided_by,
        "decision_note": decision_note,
        "decided_at": datetime.now(timezone.utc),
        "applied_batches": applied,
    })
    return _serialize_change_request(doc)


async def reject_change_request(
    request_id: str,
    *,
    decided_by: str | None = None,
    decision_note: str | None = None,
) -> dict[str, Any]:
    from bson import ObjectId

    try:
        oid = ObjectId(request_id)
    except Exception as exc:  # noqa: BLE001
        raise ChangeRequestRefused("invalid request id", code="bad_id") from exc
    doc = await ChangeRequestDoc.get(oid)
    if doc is None:
        raise ChangeRequestRefused("override not found", code="not_found")
    if doc.status != "pending":
        raise ChangeRequestRefused(
            f"request already {doc.status}",
            code="not_pending",
        )
    await doc.set({
        "status": "rejected",
        "decided_by": decided_by,
        "decision_note": decision_note,
        "decided_at": datetime.now(timezone.utc),
    })
    return _serialize_change_request(doc)


# ── Misc ─────────────────────────────────────────────────────────────────
def maybe_git_commit(message: str, settings: Settings | None = None) -> None:
    """Commit the JSON mirror directory; no-op unless both git-auto-commit
    and JSON_MIRROR are enabled."""
    settings = settings or get_settings()
    if not settings.git_auto_commit or not settings.json_mirror:
        return
    try:
        subprocess.run(["git", "add", str(settings.data_dir)], check=True, cwd=settings.data_dir.parent)
        subprocess.run(
            ["git", "commit", "-m", message, "--", str(settings.data_dir)],
            check=True,
            cwd=settings.data_dir.parent,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.warning("git auto-commit skipped: %s", exc)


# ── Internals ────────────────────────────────────────────────────────────
def _timetable_payload(doc: TimetableDoc, catalog: Any = None) -> dict[str, Any]:
    return {
        "batch": doc.code,
        "semester": {"label": doc.semester},
        "classes": [_serialize_class(c, catalog) for c in doc.classes],
    }


def _timetable_payload_from_raw(
    code: str, semester_label: str, classes: list[Any], catalog: Any = None,
) -> dict[str, Any]:
    return {
        "batch": code,
        "semester": {"label": semester_label},
        "classes": [_serialize_class(c, catalog) for c in classes],
    }


def _serialize_class(entry: Any, catalog: Any = None) -> dict[str, Any]:
    """Coerce a ClassEntry or plain dict to a stable JSON dict.

    When ``catalog`` is supplied, any entry with an empty ``subject`` but a
    non-empty ``code`` gets filled in from the catalog — this is how the
    "strip on write, resolve on read" rule keeps the public payload looking
    identical to the pre-refactor shape (see ``_normalize_classes_for_write``).
    The same fill is applied to each elective option's ``subject_name``.
    """
    if hasattr(entry, "model_dump"):
        out = entry.model_dump(exclude_none=False)
    else:
        out = dict(entry)
    if catalog is not None:
        code = out.get("code")
        subject = out.get("subject")
        has_elective_options = isinstance(opts := out.get("options"), list) and len(opts) > 1
        if code and not has_elective_options and not (isinstance(subject, str) and subject.strip()):
            resolved = catalog.name_for(code)
            if resolved:
                out["subject"] = resolved
        if isinstance(opts, list):
            for opt in opts:
                if not isinstance(opt, dict):
                    continue
                opt_code = opt.get("subject_code")
                opt_name = opt.get("subject_name")
                if opt_code and not (isinstance(opt_name, str) and opt_name.strip()):
                    resolved = catalog.name_for(opt_code)
                    if resolved:
                        opt["subject_name"] = resolved
    return out


def _norm_subject(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().upper().split())


def _normalize_classes_for_write(classes: list[Any], catalog: Any) -> list[Any]:
    """Strip ``subject`` (and elective ``subject_name``) when it equals the
    catalog default for the entry's code. Keeps overrides intact. Pure
    function — returns a new list, leaves the caller's data alone.

    Catalog == ``None`` is a no-op so callers (or tests) without a cache
    behave like before.
    """
    if catalog is None:
        return classes
    out: list[Any] = []
    for raw in classes:
        if hasattr(raw, "model_dump"):
            entry = raw.model_dump(exclude_none=False)
        elif isinstance(raw, dict):
            entry = dict(raw)
        else:
            out.append(raw)
            continue
        code = entry.get("code")
        default = catalog.name_for(code) if code else None
        if default and _norm_subject(entry.get("subject")) == _norm_subject(default):
            entry["subject"] = None
        opts = entry.get("options")
        if isinstance(opts, list):
            if len(opts) > 1 and not entry.get("electiveChoice"):
                entry["subject"] = None
                entry["code"] = None
                entry["type"] = "Elective"
                entry["room"] = None
            new_opts = []
            for opt in opts:
                if not isinstance(opt, dict):
                    new_opts.append(opt)
                    continue
                opt = dict(opt)
                opt_code = opt.get("subject_code")
                opt_default = catalog.name_for(opt_code) if opt_code else None
                if opt_default and _norm_subject(opt.get("subject_name")) == _norm_subject(opt_default):
                    opt["subject_name"] = None
                new_opts.append(opt)
            entry["options"] = new_opts
        out.append(entry)
    return out


def _safe_batch(batch: str) -> str:
    cleaned = "".join(ch for ch in batch.strip().upper() if ch.isalnum())
    if not cleaned:
        raise BatchNotFound(batch)
    return cleaned


_BATCH_PATTERN = re.compile(r"^(?P<year>\d)(?P<section>[A-Z]+)")


def _derive_batch_meta(code: str) -> dict[str, Any]:
    """Best-effort split of a batch code like "1B11" into year=1, section="B"."""
    m = _BATCH_PATTERN.match(code)
    if not m:
        return {"year": None, "section": None}
    year = int(m.group("year"))
    return {"year": year if 1 <= year <= 9 else None, "section": m.group("section")}


def _mirror_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unserialisable: {type(value).__name__}")


# ── Admin emails (allowlist) ─────────────────────────────────────────────
_ADMIN_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_admin_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not _ADMIN_EMAIL_PATTERN.match(cleaned):
        raise ValueError("email must look like name@domain.tld")
    return cleaned


async def is_admin_email(email: str) -> bool:
    if not email:
        return False
    cleaned = email.strip().lower()
    settings = get_settings()
    if cleaned in settings.admin_emails:
        return True
    doc = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
    return doc is not None


async def count_admin_emails() -> int:
    return await AdminEmailDoc.find_all().count()


async def list_admin_emails() -> list[dict[str, Any]]:
    """Combine env-set + Mongo-set admins. Env entries marked ``source="env"``."""
    settings = get_settings()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for email in sorted(settings.admin_emails):
        out.append({
            "email": email,
            "display_name": None,
            "source": "env",
            "added_at": None,
            "added_by": "env",
        })
        seen.add(email)
    async for doc in AdminEmailDoc.find_all(sort=[("added_at", -1)]):
        if doc.email in seen:
            continue
        out.append({
            "email": doc.email,
            "display_name": doc.display_name,
            "source": "db",
            "added_at": doc.added_at.isoformat() if doc.added_at else None,
            "added_by": doc.added_by,
        })
    return out


async def add_admin_email(
    email: str,
    *,
    display_name: str | None = None,
    added_by: str | None = None,
) -> dict[str, Any]:
    cleaned = _safe_admin_email(email)
    settings = get_settings()
    if cleaned in settings.admin_emails:
        return {
            "email": cleaned,
            "display_name": display_name,
            "source": "env",
            "added_at": None,
            "added_by": "env",
        }
    existing = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
    if existing is not None:
        if display_name is not None and (display_name or None) != existing.display_name:
            await existing.set({"display_name": display_name or None})
            existing = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
        return {
            "email": existing.email,
            "display_name": existing.display_name,
            "source": "db",
            "added_at": existing.added_at.isoformat() if existing.added_at else None,
            "added_by": existing.added_by,
        }
    doc = AdminEmailDoc(
        email=cleaned,
        display_name=display_name or None,
        added_by=added_by,
    )
    await doc.insert()
    return {
        "email": doc.email,
        "display_name": doc.display_name,
        "source": "db",
        "added_at": doc.added_at.isoformat(),
        "added_by": doc.added_by,
    }


async def delete_admin_email(email: str) -> bool:
    cleaned = _safe_admin_email(email)
    settings = get_settings()
    if cleaned in settings.admin_emails:
        raise ValueError("env-managed admin; remove from ADMIN_EMAILS env var")
    doc = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
    if doc is None:
        return False
    await doc.delete()
    return True


# ── Upload attempts (audit log + dashboard fuel) ─────────────────────────
def _serialize_upload_attempt(doc: UploadAttemptDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "started_at": doc.started_at.isoformat() if doc.started_at else None,
        "finished_at": doc.finished_at.isoformat() if doc.finished_at else None,
        "actor_kind": doc.actor_kind,
        "actor_email": doc.actor_email,
        "filename": doc.filename,
        "sheet_selector": doc.sheet_selector,
        "semester_label": doc.semester_label,
        "status": doc.status,
        "batches_written": doc.batches_written,
        "classes_written": doc.classes_written,
        "sheets_used": list(doc.sheets_used or []),
        "multi_sheet_batches": list(doc.multi_sheet_batches or []),
        "total_blocks": doc.total_blocks,
        "confidence_summary": dict(doc.confidence_summary or {}),
        "doctor": doc.doctor,
        "failure_message": doc.failure_message,
    }


async def record_upload_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a single UploadAttemptDoc. Best-effort — never raises."""
    try:
        doc = UploadAttemptDoc(
            started_at=payload.get("started_at") or datetime.now(timezone.utc),
            finished_at=payload.get("finished_at") or datetime.now(timezone.utc),
            actor_kind=payload.get("actor_kind"),
            actor_email=payload.get("actor_email"),
            filename=payload.get("filename"),
            sheet_selector=payload.get("sheet_selector"),
            semester_label=payload.get("semester_label"),
            status=payload.get("status") or "ok",
            batches_written=int(payload.get("batches_written") or 0),
            classes_written=int(payload.get("classes_written") or 0),
            sheets_used=list(payload.get("sheets_used") or []),
            multi_sheet_batches=list(payload.get("multi_sheet_batches") or []),
            total_blocks=int(payload.get("total_blocks") or 0),
            confidence_summary=dict(payload.get("confidence_summary") or {}),
            doctor=payload.get("doctor"),
            failure_message=payload.get("failure_message"),
        )
        await doc.insert()
        return _serialize_upload_attempt(doc)
    except Exception:
        logger.exception("failed to persist UploadAttemptDoc")
        return {}


async def list_upload_attempts(
    *,
    limit: int = 50,
    status: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if status is not None and status not in {"ok", "partial", "failed"}:
        raise ValueError(f"invalid status {status!r}")
    if status:
        query = UploadAttemptDoc.find(UploadAttemptDoc.status == status).sort(-UploadAttemptDoc.started_at).skip(offset).limit(limit)
    else:
        query = UploadAttemptDoc.find_all().sort(-UploadAttemptDoc.started_at).skip(offset).limit(limit)
    out: list[dict[str, Any]] = []
    async for doc in query:
        out.append(_serialize_upload_attempt(doc))
        if len(out) >= limit:
            break

    # Enrich each upload with live error stats (open/resolved/ignored + top
    # error types). Two aggregations regardless of N so this stays O(1) round
    # trips even when the caller asks for 500 uploads.
    upload_ids = [row["id"] for row in out if row.get("id")]
    if upload_ids:
        coll = ParsingErrorDoc.get_motor_collection()

        stats_by_status: dict[str, dict[str, int]] = {}
        status_cursor = coll.aggregate([
            {"$match": {"upload_id": {"$in": upload_ids}}},
            {"$group": {
                "_id": {"upload_id": "$upload_id", "status": "$status"},
                "count": {"$sum": 1},
            }},
        ])
        async for row in status_cursor:
            uid = row["_id"]["upload_id"]
            st = row["_id"]["status"]
            bucket = stats_by_status.setdefault(uid, {"open": 0, "resolved": 0, "ignored": 0})
            bucket[st] = int(row.get("count") or 0)

        top_types_by_upload: dict[str, list[dict[str, Any]]] = {}
        type_cursor = coll.aggregate([
            {"$match": {"upload_id": {"$in": upload_ids}, "status": "open"}},
            {"$group": {
                "_id": {"upload_id": "$upload_id", "type": "$error_type"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"count": -1}},
        ])
        async for row in type_cursor:
            uid = row["_id"]["upload_id"]
            top_types_by_upload.setdefault(uid, []).append({
                "error_type": row["_id"]["type"],
                "count": int(row.get("count") or 0),
            })

        for row in out:
            uid = row.get("id")
            buckets = stats_by_status.get(uid, {"open": 0, "resolved": 0, "ignored": 0})
            row["errors_open"] = buckets.get("open", 0)
            row["errors_resolved"] = buckets.get("resolved", 0)
            row["errors_ignored"] = buckets.get("ignored", 0)
            row["errors_total"] = sum(buckets.values())
            row["errors_top_types"] = top_types_by_upload.get(uid, [])[:4]

        # BASELINE_MISSING / BASELINE_MISMATCH rows written by the doctor
        # backfill have upload_id=None — they aren't tied to a specific
        # ingest but describe the current state of the most recent one.
        # Merge them into the most recent upload's stats so the uploads
        # page reflects the full error picture.
        if out:
            orphan_status_cursor = coll.aggregate([
                {"$match": {"upload_id": None}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ])
            orphan_buckets: dict[str, int] = {}
            async for row in orphan_status_cursor:
                if row["_id"] in ("open", "resolved", "ignored"):
                    orphan_buckets[row["_id"]] = int(row.get("count") or 0)

            if orphan_buckets:
                top = out[0]
                for st in ("open", "resolved", "ignored"):
                    top[f"errors_{st}"] = (top.get(f"errors_{st}") or 0) + orphan_buckets.get(st, 0)
                top["errors_total"] = top["errors_open"] + top["errors_resolved"] + top["errors_ignored"]

                orphan_type_cursor = coll.aggregate([
                    {"$match": {"upload_id": None, "status": "open"}},
                    {"$group": {"_id": "$error_type", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ])
                type_map = {t["error_type"]: t["count"] for t in top.get("errors_top_types") or []}
                async for row in orphan_type_cursor:
                    et = row["_id"]
                    type_map[et] = type_map.get(et, 0) + int(row.get("count") or 0)
                top["errors_top_types"] = sorted(
                    [{"error_type": k, "count": v} for k, v in type_map.items()],
                    key=lambda x: -x["count"],
                )[:4]

    return out


async def count_upload_attempts(*, status: str | None = None) -> int:
    if status:
        return await UploadAttemptDoc.find(UploadAttemptDoc.status == status).count()
    return await UploadAttemptDoc.find_all().count()


async def get_upload_attempt(attempt_id: str) -> dict[str, Any] | None:
    from bson import ObjectId

    try:
        oid = ObjectId(attempt_id)
    except Exception:
        return None
    doc = await UploadAttemptDoc.get(oid)
    if doc is None:
        return None
    return _serialize_upload_attempt(doc)


async def compute_admin_stats() -> dict[str, Any]:
    """Aggregate numbers for the admin dashboard hero cards + donut.

    All-time aggregates across every recorded ingest.
    """
    batches_with_timetables = await TimetableDoc.find_all().count()

    total_uploads = await UploadAttemptDoc.find_all().count()
    failed_partial = await UploadAttemptDoc.find(
        {"status": {"$in": ["partial", "failed"]}}
    ).count()

    # Total open errors comes from the live ParsingErrorDoc collection so it
    # reflects triage state (fixed rows drop out of the count).
    total_errors = await ParsingErrorDoc.find(ParsingErrorDoc.status == "open").count()

    total_blocks = 0
    high = medium = low = unreliable = 0
    async for doc in UploadAttemptDoc.find_all():
        total_blocks += int(doc.total_blocks or 0)
        summary = dict(doc.confidence_summary or {})
        high += int(summary.get("HIGH", 0))
        medium += int(summary.get("MEDIUM", 0))
        low += int(summary.get("LOW", 0))
        unreliable += int(summary.get("UNRELIABLE", 0))

    parsed_ok = high + medium
    accuracy_pct: float | None = None
    if total_blocks > 0:
        accuracy_pct = round(parsed_ok * 100.0 / total_blocks, 2)

    return {
        "batches_with_timetables": batches_with_timetables,
        "uploads_logged": total_uploads,
        "failed_partial_uploads": failed_partial,
        "total_parsing_errors": total_errors,
        "parsing_accuracy_pct": accuracy_pct,
        "confidence_totals": {
            "HIGH": high,
            "MEDIUM": medium,
            "LOW": low,
            "UNRELIABLE": unreliable,
            "TOTAL": total_blocks,
        },
    }


# ── Ingest cooldown + snapshot/rollback ──────────────────────────────────
# A successful or partial ingest takes a single snapshot of the live data
# (batches + timetables + semester label) so an admin can roll the most
# recent run back. The snapshot self-destructs after
# INGEST_SNAPSHOT_TTL_HOURS via a Mongo TTL index. Cooldown is enforced
# against the most recent ``UploadAttemptDoc.started_at`` to prevent
# accidental double-runs.


async def last_ingest_started_at() -> datetime | None:
    doc = await UploadAttemptDoc.find_one(sort=[("started_at", -1)])
    return doc.started_at if doc else None


async def check_ingest_cooldown(
    settings: Settings | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Return ``{"ok": True}`` if a fresh ingest is allowed, otherwise a
    payload describing how long the caller must wait. Pass ``force=True`` to
    bypass the gate (the route enforces admin-only access on top of this).
    """
    settings = settings or get_settings()
    cooldown_h = max(0.0, float(settings.ingest_cooldown_hours))
    if force or cooldown_h <= 0:
        return {"ok": True}
    last = await last_ingest_started_at()
    if last is None:
        return {"ok": True}
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - last
    cooldown = timedelta(hours=cooldown_h)
    if elapsed >= cooldown:
        return {"ok": True}
    retry_after = cooldown - elapsed
    return {
        "ok": False,
        "last_ingest_at": last.isoformat(),
        "cooldown_hours": cooldown_h,
        "retry_after_seconds": int(retry_after.total_seconds()),
    }


def _strip_object_id(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove the Mongo _id so a snapshot row can be re-inserted cleanly."""
    if not isinstance(doc, dict):
        return doc
    out = dict(doc)
    out.pop("_id", None)
    return out


async def save_ingest_snapshot(settings: Settings | None = None) -> dict[str, Any]:
    """Replace any existing snapshot with one of the current state.

    Returns a small summary used by the caller for logging. Only ever stores
    one document — older snapshots are removed so rollback always undoes the
    most recent run.
    """
    settings = settings or get_settings()
    ttl_h = max(1.0, float(settings.ingest_snapshot_ttl_hours))
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_h)

    batches_raw = await BatchDoc.find_all().to_list()
    timetables_raw = await TimetableDoc.find_all().to_list()
    current_doc = await SemesterDoc.find_one()

    batches = [_strip_object_id(b.model_dump(mode="json")) for b in batches_raw]
    timetables = [_strip_object_id(t.model_dump(mode="json")) for t in timetables_raw]
    current = _strip_object_id(current_doc.model_dump(mode="json")) if current_doc else None

    # Replace: delete all existing snapshots, write a fresh one.
    await IngestSnapshotDoc.find_all().delete()
    snap = IngestSnapshotDoc(
        created_at=now,
        expires_at=expires_at,
        semester_label=(current.get("label") if isinstance(current, dict) else None),
        batch_count=len(batches),
        timetable_count=len(timetables),
        batches=batches,
        timetables=timetables,
        current=current,
    )
    await snap.insert()
    return {
        "id": str(snap.id),
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "batches": len(batches),
        "timetables": len(timetables),
    }


def _snapshot_meta(snap: IngestSnapshotDoc) -> dict[str, Any]:
    return {
        "id": str(snap.id),
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
        "expires_at": snap.expires_at.isoformat() if snap.expires_at else None,
        "semester_label": snap.semester_label,
        "batches": snap.batch_count,
        "timetables": snap.timetable_count,
    }


async def get_ingest_snapshot_meta() -> dict[str, Any] | None:
    snap = await IngestSnapshotDoc.find_one(sort=[("created_at", -1)])
    if snap is None:
        return None
    # Pretend it doesn't exist if the TTL date has already passed but Mongo
    # hasn't pruned yet (TTL is sweep-based, runs ~every minute).
    exp = snap.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp is not None and exp < datetime.now(timezone.utc):
        return None
    return _snapshot_meta(snap)


async def restore_ingest_snapshot() -> dict[str, Any]:
    """Wipe ``batches`` + ``timetables`` and rewrite them from the snapshot.

    Returns counts of what was restored. Raises ``LookupError`` if no
    snapshot is available. The snapshot is deleted on success — rollback
    is single-use, mirroring the "only the last run can be undone" promise.
    """
    snap = await IngestSnapshotDoc.find_one(sort=[("created_at", -1)])
    if snap is None:
        raise LookupError("No ingest snapshot available")

    # Wipe + rebuild batches + timetables.
    await BatchDoc.find_all().delete()
    await TimetableDoc.find_all().delete()

    now = datetime.now(timezone.utc)
    restored_batches = 0
    for row in snap.batches:
        try:
            payload = dict(row)
            await BatchDoc(**payload).insert()
            restored_batches += 1
        except Exception:
            logger.exception("snapshot: failed to restore batch row %r", row)

    restored_timetables = 0
    for row in snap.timetables:
        try:
            payload = dict(row)
            await TimetableDoc(**payload).insert()
            restored_timetables += 1
        except Exception:
            logger.exception("snapshot: failed to restore timetable row %r", row)

    # Restore semester label if we have one.
    if isinstance(snap.current, dict) and snap.current.get("label"):
        await SemesterDoc.find_all().delete()
        try:
            await SemesterDoc(**dict(snap.current)).insert()
        except Exception:
            logger.exception("snapshot: failed to restore semester doc")

    # Single-use: drop the snapshot now that we've used it.
    await snap.delete()

    return {
        "ok": True,
        "restored_at": now.isoformat(),
        "batches": restored_batches,
        "timetables": restored_timetables,
        "semester_label": snap.semester_label,
    }


async def replace_timetables(codes_to_keep: list[str]) -> int:
    """Delete TimetableDoc rows whose ``code`` is not in ``codes_to_keep``.

    Mirrors the prune behaviour of ``replace_batch_directory``. Returns the
    number of stale rows removed.
    """
    keep = {str(c).upper() for c in codes_to_keep}
    stale_count = 0
    async for doc in TimetableDoc.find_all():
        if doc.code.upper() not in keep:
            try:
                await doc.delete()
                stale_count += 1
            except Exception:
                logger.exception("replace_timetables: failed to delete %s", doc.code)
    return stale_count


# ── Parsing errors (admin Fix tab) ───────────────────────────────────────


def _parsing_error_payload(doc: ParsingErrorDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "upload_id": doc.upload_id,
        "batch_code": doc.batch_code,
        "error_type": doc.error_type,
        "severity": doc.severity,
        "day": doc.day,
        "start_time": doc.start_time,
        "period": doc.period,
        "code": doc.code,
        "message": doc.message,
        "context": doc.context,
        "status": doc.status,
        "resolved_by": doc.resolved_by,
        "resolved_at": doc.resolved_at.isoformat() if doc.resolved_at else None,
        "note": doc.note,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


# Mapping that turns the parser's confidence severity into the Fix-tab
# severity vocabulary. We also use the same map for doctor mismatches.
_PARSER_SEV_TO_FIX = {
    "HIGH": "info",
    "MEDIUM": "warn",
    "LOW": "warn",
    "UNRELIABLE": "error",
}


async def save_parsing_errors(
    *,
    upload_id: str | None,
    error_rows: list[dict[str, Any]],
    doctor: dict[str, Any] | None = None,
) -> int:
    """Persist parser warnings + doctor mismatches as ``ParsingErrorDoc`` rows.

    Called from two contexts:

    * **After an ingest** (``upload_id`` is the new UploadAttemptDoc id) —
      every prior *open* row is cleared first, because the canonical
      timetables those errors describe have just been replaced. Rows that
      an admin already marked ``resolved`` / ``ignored`` are preserved as
      audit history.
    * **From ``backfill_baseline_errors``** (``upload_id`` is ``None``) —
      only the orphan (``upload_id=None``) rows are refreshed; per-ingest
      history is left untouched.
    """
    if upload_id:
        # Fresh ingest: nuke open rows from every prior upload so the Fix
        # tab reflects the current timetable, not stale ones.
        await ParsingErrorDoc.find(
            ParsingErrorDoc.status == "open",
        ).delete()
    else:
        # Backfill path: only touch the orphan rows we own.
        await ParsingErrorDoc.find(
            {"upload_id": None, "status": "open"},
        ).delete()
    now = datetime.now(timezone.utc)
    written = 0

    for row in error_rows or []:
        sev = _PARSER_SEV_TO_FIX.get(str(row.get("severity") or "").upper(), "warn")
        try:
            await ParsingErrorDoc(
                upload_id=upload_id,
                batch_code=row.get("batch"),
                error_type=str(row.get("code") or "parser_warning"),
                severity=sev,
                day=row.get("day"),
                start_time=row.get("start_time"),
                code=None,
                message=str(row.get("message") or ""),
                context={k: v for k, v in row.items() if k not in {"batch", "day", "start_time", "severity", "code", "message"}},
                status="open",
                created_at=now,
                updated_at=now,
            ).insert()
            written += 1
        except Exception:
            logger.exception("save_parsing_errors: failed to write row %r", row)

    # Doctor mismatches: one row per outlier batch that deviates from its
    # admin-curated baseline. Grouped baseline-less streams also surface as an
    # advisory so the admin knows to define one.
    if isinstance(doctor, dict):
        for grp in doctor.get("mismatches", []) or []:
            group = grp.get("group")
            baseline_key = grp.get("baseline_key")
            expected = grp.get("expected") or {}
            outliers = {out.get("batch"): out for out in (grp.get("outliers") or [])}
            course_check = grp.get("course_check") or {}
            course_rows = dict(course_check.get("per_batch_detail") or {})
            affected_batches = sorted(set(outliers) | {
                batch for batch, row in course_rows.items()
                if row.get("missing") or row.get("extra") or row.get("course_deltas")
            })
            for batch in affected_batches:
                out = outliers.get(batch) or {}
                course_row = course_rows.get(batch) or {}
                counts = out.get("counts") or {}
                deltas = out.get("deltas") or {}
                delta_pieces = ", ".join(
                    f"{k} {'+' if v > 0 else ''}{v}"
                    for k, v in sorted(deltas.items())
                    if k != "total"
                )
                issue_count = len(course_row.get("course_deltas") or [])
                msg = (
                    f"{batch} deviates from baseline {baseline_key or group}: "
                    f"{delta_pieces or ('course roster mismatch' if course_row.get('missing') or course_row.get('extra') or issue_count else 'total mismatch')}"
                    + (f"; {issue_count} course count issue(s)" if issue_count else "")
                )
                try:
                    await ParsingErrorDoc(
                        upload_id=upload_id,
                        batch_code=batch,
                        error_type="BASELINE_MISMATCH",
                        severity="warn",
                        message=msg,
                        context={
                            "group": group,
                            "baseline_key": baseline_key,
                            "expected": expected,
                            "actual": counts,
                            "deltas": deltas,
                            "course_deltas": course_row.get("course_deltas", []),
                            "missing_courses": course_row.get("missing", []),
                            "extra_courses": course_row.get("extra", []),
                            "missing_course_details": course_row.get("missing_details", []),
                            "extra_course_details": course_row.get("extra_details", []),
                        },
                        status="open",
                        created_at=now,
                        updated_at=now,
                    ).insert()
                    written += 1
                except Exception:
                    logger.exception("save_parsing_errors: failed to write mismatch %r", out)

        for grp in doctor.get("no_baseline", []) or []:
            group = grp.get("group")
            baseline_key = grp.get("baseline_key")
            batch_codes = grp.get("batch_codes") or []
            msg = (
                f"Group {group} ({len(batch_codes)} batches) has no baseline defined "
                f"— add baseline {baseline_key or group} to enable consistency checks."
            )
            try:
                await ParsingErrorDoc(
                    upload_id=upload_id,
                    batch_code=None,
                    error_type="BASELINE_MISSING",
                    severity="info",
                    message=msg,
                    context={
                        "group": group,
                        "baseline_key": baseline_key,
                        "batch_codes": batch_codes,
                    },
                    status="open",
                    created_at=now,
                    updated_at=now,
                ).insert()
                written += 1
            except Exception:
                logger.exception("save_parsing_errors: failed to write no_baseline %r", grp)

    return written


async def backfill_baseline_errors(settings: Settings | None = None) -> dict[str, Any]:
    """Re-run the doctor against the current live timetables + baselines and
    replace all orphan (``upload_id=None``) baseline rows with fresh output.

    Use this to populate the Fix page with ``BASELINE_MISMATCH`` /
    ``BASELINE_MISSING`` rows without re-ingesting the source spreadsheet
    (e.g. after upgrading from the pre-baseline doctor, or after editing
    baselines and wanting the Fix page to reflect them immediately).
    """
    # Local import to avoid a top-level cycle with server.doctor.
    from server.doctor import build_doctor_report, codes_in, count_classes

    try:
        current = await read_current(settings=settings)
    except DataMissing:
        return {"status": "skipped", "reason": "no current semester", "batches": 0, "written": 0, "deleted": 0}

    label = current.get("label")
    prefix = semester_prefix(label)

    counts_by_batch: dict[str, dict[str, int]] = {}
    codes_by_batch: dict[str, set[str]] = {}
    async for doc in TimetableDoc.find_all():
        raw_classes = getattr(doc, "classes", None) or []
        classes: list[dict[str, Any]] = []
        for c in raw_classes:
            if hasattr(c, "model_dump"):
                classes.append(c.model_dump())
            elif isinstance(c, dict):
                classes.append(c)
        counts_by_batch[doc.code] = count_classes(classes)
        codes_by_batch[doc.code] = codes_in(classes)

    if not counts_by_batch:
        return {"status": "skipped", "reason": "no timetables", "batches": 0, "written": 0, "deleted": 0}

    baselines = await read_baselines_for_prefix(prefix, settings=settings)
    baseline_courses = await read_baseline_courses_for_prefix(prefix, settings=settings)
    report = build_doctor_report(
        counts_by_batch,
        baselines_by_group=baselines,
        semester_prefix=prefix,
        codes_by_batch=codes_by_batch,
        courses_by_group=baseline_courses,
    )

    # Wipe all open baseline rows (orphan or ingest-associated) so stale
    # BASELINE_MISSING entries from a pre-baseline ingest are cleared when the
    # admin adds baselines and triggers a backfill.  The fresh doctor output
    # written below supersedes them; resolved/ignored rows are untouched.
    stale_types = ["BASELINE_MISMATCH", "BASELINE_MISSING", "doctor_mismatch"]
    delete_res = await ParsingErrorDoc.find(
        {"error_type": {"$in": stale_types}, "status": "open"}
    ).delete()
    deleted = getattr(delete_res, "deleted_count", 0) or 0

    written = await save_parsing_errors(upload_id=None, error_rows=[], doctor=report)

    return {
        "status": "ok",
        "semester_label": label,
        "semester_prefix": prefix,
        "batches": len(counts_by_batch),
        "mismatched_groups": report.get("mismatched_groups", 0),
        "groups_without_baseline": report.get("groups_without_baseline", 0),
        "written": written,
        "deleted": deleted,
    }


async def list_parsing_errors(
    *,
    status: str | None = None,
    upload_id: str | None = None,
    error_type: str | None = None,
    batch_code: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = ParsingErrorDoc.find_all(sort=[("created_at", -1)])
    if status:
        query = ParsingErrorDoc.find(ParsingErrorDoc.status == status, sort=[("created_at", -1)])
    _ORPHAN_DOCTOR_TYPES = {"BASELINE_MISSING", "BASELINE_MISMATCH", "doctor_mismatch"}
    out: list[dict[str, Any]] = []
    skipped = 0
    async for doc in query:
        if upload_id and doc.upload_id != upload_id:
            # Also surface orphan doctor rows so upload detail pages show
            # BASELINE_MISSING / BASELINE_MISMATCH written by the backfill.
            if not (doc.upload_id is None and doc.error_type in _ORPHAN_DOCTOR_TYPES):
                continue
        if error_type and doc.error_type != error_type:
            continue
        if batch_code and (doc.batch_code or "").upper() != batch_code.upper():
            continue
        if skipped < offset:
            skipped += 1
            continue
        out.append(_parsing_error_payload(doc))
        if len(out) >= limit:
            break
    return out


async def count_parsing_errors(
    *, status: str | None = None, upload_id: str | None = None,
    error_type: str | None = None, batch_code: str | None = None,
) -> int:
    rows = await list_parsing_errors(status=status, upload_id=upload_id, error_type=error_type, batch_code=batch_code, limit=10**9)
    return len(rows)


async def parsing_errors_summary(upload_id: str | None = None) -> dict[str, Any]:
    """Return aggregate counts grouped by error_type + by status."""
    _ORPHAN_DOCTOR_TYPES = {"BASELINE_MISSING", "BASELINE_MISMATCH", "doctor_mismatch"}
    by_type: dict[str, dict[str, int]] = {}
    totals = {"open": 0, "resolved": 0, "ignored": 0}
    async for doc in ParsingErrorDoc.find_all():
        if upload_id and doc.upload_id != upload_id:
            if not (doc.upload_id is None and doc.error_type in _ORPHAN_DOCTOR_TYPES):
                continue
        bucket = by_type.setdefault(doc.error_type, {"open": 0, "resolved": 0, "ignored": 0})
        bucket[doc.status] = bucket.get(doc.status, 0) + 1
        totals[doc.status] = totals.get(doc.status, 0) + 1
    by_type_list = [
        {"error_type": k, **v, "total": sum(v.values())}
        for k, v in sorted(by_type.items(), key=lambda kv: -sum(kv[1].values()))
    ]
    return {"by_type": by_type_list, "totals": totals, "grand_total": sum(totals.values())}


async def update_parsing_error_status(
    *,
    error_id: str,
    new_status: str,
    resolved_by: str | None = None,
    note: str | None = None,
) -> dict[str, Any] | None:
    if new_status not in {"open", "resolved", "ignored"}:
        raise ValueError(f"invalid status: {new_status!r}")
    try:
        from beanie import PydanticObjectId

        doc = await ParsingErrorDoc.get(PydanticObjectId(error_id))
    except Exception:
        return None
    if doc is None:
        return None
    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
    }
    if new_status in {"resolved", "ignored"}:
        updates["resolved_by"] = resolved_by
        updates["resolved_at"] = now
    else:
        updates["resolved_by"] = None
        updates["resolved_at"] = None
    if note is not None:
        updates["note"] = note or None
    await doc.set(updates)
    refreshed = await ParsingErrorDoc.get(doc.id)
    return _parsing_error_payload(refreshed) if refreshed else None


async def bulk_update_parsing_errors(
    *,
    error_ids: list[str],
    new_status: str,
    resolved_by: str | None = None,
) -> int:
    """Apply the same status to multiple errors. Returns count updated."""
    if new_status not in {"open", "resolved", "ignored"}:
        raise ValueError(f"invalid status: {new_status!r}")
    count = 0
    for eid in error_ids or []:
        try:
            res = await update_parsing_error_status(
                error_id=eid, new_status=new_status, resolved_by=resolved_by
            )
            if res is not None:
                count += 1
        except Exception:
            logger.exception("bulk_update_parsing_errors: %s failed", eid)
    return count


# ── Subject catalog (DB-backed, replaces assets/subjects.json) ───────────
# All admin reads/writes flow through here. After every mutation we
# ``invalidate_catalog()`` so the next request rebuilds the in-process
# snapshot used by the parser and the read-side resolver.

def _subject_payload(doc: SubjectDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id) if doc.id else None,
        "code": doc.code,
        "name": doc.name,
        "aliases": list(doc.aliases or []),
        "source": doc.source,
        "created_by": doc.created_by,
        "note": doc.note,
        "created_at": _iso_z(doc.created_at) if hasattr(doc, "created_at") else None,
        "updated_at": _iso_z(doc.updated_at) if hasattr(doc, "updated_at") else None,
    }


def _normalize_subject_code(code: str) -> str:
    """Match ``base_subject_code``: upper, alnum, drop trailing L/T/P."""
    cleaned = "".join(ch for ch in (code or "").strip().upper() if ch.isalnum())
    if not cleaned:
        raise ValueError("subject code required")
    if cleaned[-1] in {"L", "T", "P"} and len(cleaned) > 1:
        cleaned = cleaned[:-1]
    return cleaned


def _camel_subject_name(value: str) -> str:
    acronyms = {"AI", "API", "CPU", "GPU", "IoT", "ML", "NLP", "UCS", "UI", "URL", "XML"}
    result = []
    for word in " ".join(str(value or "").split()).split(" "):
        bare = word.strip("()[],.:;/-")
        result.append(word if bare.upper() in {item.upper() for item in acronyms} else word[:1].upper() + word[1:].lower())
    return " ".join(result)


async def list_subjects(
    *,
    q: str | None = None,
    source: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    qnorm = (q or "").strip().upper()
    skipped = 0
    async for doc in SubjectDoc.find_all(sort=[("code", 1)]):
        if source and doc.source != source:
            continue
        if qnorm and qnorm not in (doc.code or "").upper() and qnorm not in (doc.name or "").upper():
            continue
        if skipped < max(0, offset):
            skipped += 1
            continue
        out.append(_subject_payload(doc))
        if len(out) >= limit:
            break
    return out


async def count_subjects(*, q: str | None = None, source: str | None = None) -> int:
    qnorm = (q or "").strip().upper()
    count = 0
    async for doc in SubjectDoc.find_all():
        if source and doc.source != source:
            continue
        if qnorm and qnorm not in (doc.code or "").upper() and qnorm not in (doc.name or "").upper():
            continue
        count += 1
    return count


async def upsert_subject(
    *,
    code: str,
    name: str,
    aliases: list[str] | None = None,
    source: str = "admin",
    created_by: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Insert or update a single subject. Idempotent on ``code``."""
    from timetable_parser.core.subject_catalog import invalidate_catalog

    norm = _normalize_subject_code(code)
    clean_name = _camel_subject_name(name)
    if not clean_name:
        raise ValueError("subject name required")
    now = datetime.now(timezone.utc)
    existing = await SubjectDoc.find_one(SubjectDoc.code == norm)
    if existing is None:
        doc = SubjectDoc(
            code=norm,
            name=clean_name,
            aliases=list(aliases or []),
            source=source,
            created_by=created_by,
            note=note,
        )
        await doc.insert()
    else:
        updates: dict[str, Any] = {"name": clean_name, "updated_at": now}
        if aliases is not None:
            updates["aliases"] = list(aliases)
        if note is not None:
            updates["note"] = note
        # ``source`` is never downgraded from "admin" back to "seed"; admin
        # writes always mark the row as admin-owned.
        if existing.source != "admin" and source == "admin":
            updates["source"] = "admin"
            updates["created_by"] = created_by or existing.created_by
        await existing.set(updates)
        doc = await SubjectDoc.find_one(SubjectDoc.code == norm)
    invalidate_catalog()
    return _subject_payload(doc)


async def delete_subject(code: str, *, force: bool = False) -> bool:
    from timetable_parser.core.subject_catalog import invalidate_catalog

    norm = _normalize_subject_code(code)
    doc = await SubjectDoc.find_one(SubjectDoc.code == norm)
    if doc is None:
        return False
    if doc.source == "seed" and not force:
        raise PermissionError(
            f"refusing to delete seed subject {norm!r} without force=True"
        )
    await doc.delete()
    invalidate_catalog()
    return True


async def bulk_upsert_subjects(
    items: list[dict[str, Any]],
    *,
    created_by: str | None = None,
) -> dict[str, int]:
    """Best-effort bulk import. Returns ``{added, updated, failed}``."""
    added = 0
    updated = 0
    failed = 0
    normalized_codes: set[str] = set()
    for raw in items or []:
        if not isinstance(raw, dict):
            failed += 1
            continue
        try:
            norm = _normalize_subject_code(raw.get("code", ""))
            normalized_codes.add(norm)
            existed = await SubjectDoc.find_one(SubjectDoc.code == norm)
            await upsert_subject(
                code=norm,
                name=str(raw.get("name", "")),
                aliases=raw.get("aliases") if isinstance(raw.get("aliases"), list) else None,
                source="import",
                created_by=created_by,
                note=raw.get("note") if isinstance(raw.get("note"), str) else None,
            )
            if existed is None:
                added += 1
            else:
                updated += 1
        except Exception:
            logger.exception("bulk_upsert_subjects: row failed: %r", raw)
            failed += 1
    resolved = 0
    if normalized_codes:
        candidates = await list_parsing_errors(
            status="open", error_type="SUBJECT_NOT_IN_CATALOG", limit=10000,
        )
        ids = [
            row["id"] for row in candidates
            if any(
                code in (row.get("code") or "").upper()
                or code in str(row.get("context") or {}).upper()
                or code in (row.get("message") or "").upper()
                for code in normalized_codes
            )
        ]
        if ids:
            resolved = await bulk_update_parsing_errors(
                error_ids=ids, new_status="resolved", resolved_by=created_by,
            )
    return {"added": added, "updated": updated, "failed": failed, "errors_resolved": resolved}


async def normalize_all_timetables() -> dict[str, int]:
    """One-shot pass: walk every TimetableDoc and re-run the catalog-strip
    rule against ``classes``. Idempotent — running it twice is a no-op.

    Returns counts of timetables scanned vs actually rewritten.
    """
    from timetable_parser.core.subject_catalog import ensure_catalog

    catalog = await ensure_catalog()
    scanned = 0
    rewritten = 0
    async for tt in TimetableDoc.find_all():
        scanned += 1
        before = [_serialize_class(c) for c in tt.classes]
        after = _normalize_classes_for_write(before, catalog)
        # Cheap structural compare — only rewrite when something actually
        # changed, so we don't bump updated_at on every doc.
        if before != after:
            await tt.set({
                "classes": after,
                "updated_at": datetime.now(timezone.utc),
            })
            rewritten += 1
    return {"scanned": scanned, "rewritten": rewritten}


# ── Announcements + exam dates ───────────────────────────────────────────
# These collections are managed explicitly through the admin API. They are
# intentionally not seeded from bundled files on startup or first read.

_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_ALLOWED_SEVERITY = {"info", "warn", "critical"}


def _parse_iso_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("posted_at must be an ISO-8601 datetime") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _announcement_payload(doc: AnnouncementDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "body": doc.body,
        "severity": doc.severity,
        "posted_at": _iso_z(doc.posted_at),
        "link": doc.link,
    }


def _exam_payload(doc: ExamDateDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "subject": doc.subject,
        "code": doc.code,
        "date": doc.date,
        "slot": doc.slot,
        "type": doc.type,
        "room": doc.room,
        "target_year": doc.target_year,
    }


async def list_announcements() -> list[dict[str, Any]]:
    return [
        _announcement_payload(doc)
        async for doc in AnnouncementDoc.find_all(sort=[("posted_at", -1)])
    ]


async def add_announcement(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not title:
        raise ValueError("title is required")
    if not body:
        raise ValueError("body is required")
    severity = str(payload.get("severity") or "info").strip().lower()
    if severity not in _ALLOWED_SEVERITY:
        raise ValueError("severity must be one of: info, warn, critical")
    posted_at_raw = payload.get("posted_at")
    posted_at = _parse_iso_dt(posted_at_raw) if posted_at_raw else datetime.now(timezone.utc)
    link = payload.get("link")
    if link is not None and not isinstance(link, str):
        raise ValueError("link must be a string when provided")
    doc = AnnouncementDoc(
        title=title,
        body=body,
        severity=severity,
        posted_at=posted_at,
        link=(link or None) if link is not None else None,
    )
    await doc.insert()
    return _announcement_payload(doc)


async def delete_announcement(announcement_id: str) -> bool:
    from beanie import PydanticObjectId
    try:
        oid = PydanticObjectId(announcement_id)
    except Exception:  # invalid id format → treated as not-found
        return False
    doc = await AnnouncementDoc.get(oid)
    if doc is None:
        return False
    await doc.delete()
    return True


async def reset_announcements() -> dict[str, int]:
    """Delete every AnnouncementDoc without reseeding."""
    deleted = await AnnouncementDoc.find_all().delete()
    deleted_count = getattr(deleted, "deleted_count", 0) or 0
    return {"deleted": deleted_count, "seeded": 0}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def list_exam_dates(batch: str | None = None) -> list[dict[str, Any]]:
    """Return all exam-date rows, earliest-first.

    When a ``batch`` code is supplied, the result is filtered to:
      * exams whose ``target_year`` matches the batch's year (or is ``None``);
      * exams whose ``code`` appears in that batch's timetable (canonical
        cells *and* elective option codes).

    If the batch has no timetable yet, only the year filter is applied so
    the user still sees something useful. When no batch is supplied (e.g.
    the home page) every exam row is returned.
    """
    subject_codes: set[str] | None = None
    user_year: int | None = None
    if batch:
        try:
            code = _safe_batch(batch)
        except BatchNotFound:
            code = None
        if code:
            meta = _derive_batch_meta(code)
            user_year = meta.get("year")
            subject_codes = await _collect_batch_subject_codes(code)

    out: list[dict[str, Any]] = []
    async for doc in ExamDateDoc.find_all(sort=[("date", 1), ("slot", 1)]):
        if user_year is not None and doc.target_year is not None and doc.target_year != user_year:
            continue
        if subject_codes is not None and doc.code.upper() not in subject_codes:
            continue
        out.append(_exam_payload(doc))
    return out


async def _collect_batch_subject_codes(batch: str) -> set[str]:
    """Return the upper-cased subject codes present in ``batch``'s timetable.

    Includes both canonical cell codes and any elective ``options[*].subject_code``.
    Also adds the base code for entries that carry a trailing class-type suffix
    (L / T / P for Lecture / Tutorial / Practical) so that an exam date stored
    as "UPH013" matches timetable cells recorded as "UPH013L", "UPH013T", "UPH013P".
    Empty set on miss (or no timetable).
    """
    doc = await TimetableDoc.find_one(TimetableDoc.code == batch)
    if doc is None:
        return set()
    codes: set[str] = set()
    for klass in doc.classes:
        code = (klass.code or "").strip().upper()
        if code:
            codes.add(code)
            if len(code) >= 5 and code[-1] in ('L', 'T', 'P'):
                codes.add(code[:-1])
        for opt in klass.options or []:
            opt_code = (opt.subject_code or "").strip().upper()
            if opt_code:
                codes.add(opt_code)
                if len(opt_code) >= 5 and opt_code[-1] in ('L', 'T', 'P'):
                    codes.add(opt_code[:-1])
    return codes


async def add_exam_date(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    subject = str(payload.get("subject") or "").strip()
    code = str(payload.get("code") or "").strip().upper()
    date = str(payload.get("date") or "").strip()
    if not subject:
        raise ValueError("subject is required")
    if not code:
        raise ValueError("code is required")
    if not _DATE_RE.match(date):
        raise ValueError("date must be yyyy-mm-dd")

    def _opt(field: str) -> str | None:
        raw = payload.get(field)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(f"{field} must be a string when provided")
        cleaned = raw.strip()
        return cleaned or None

    target_year: int | None = None
    raw_year = payload.get("target_year")
    if raw_year not in (None, "", "all"):
        try:
            target_year = int(raw_year)
        except (TypeError, ValueError) as exc:
            raise ValueError("target_year must be an integer (1..9) or null") from exc
        if not 1 <= target_year <= 9:
            raise ValueError("target_year must be between 1 and 9")

    doc = ExamDateDoc(
        subject=subject,
        code=code,
        date=date,
        slot=_opt("slot"),
        type=_opt("type"),
        room=_opt("room"),
        target_year=target_year,
    )
    await doc.insert()
    return _exam_payload(doc)


async def delete_exam_date(exam_id: str) -> bool:
    from beanie import PydanticObjectId
    try:
        oid = PydanticObjectId(exam_id)
    except Exception:
        return False
    doc = await ExamDateDoc.get(oid)
    if doc is None:
        return False
    await doc.delete()
    return True


async def reset_exam_dates() -> dict[str, int]:
    """Delete every ExamDateDoc without reseeding."""
    deleted = await ExamDateDoc.find_all().delete()
    deleted_count = getattr(deleted, "deleted_count", 0) or 0
    return {"deleted": deleted_count, "seeded": 0}


# ── Calendar overrides ──────────────────────────────────────────────────
_ALLOWED_OVERRIDE_KIND = {"holiday", "follow_day", "mst", "est", "assessment", "frosh"}
_ALLOWED_OVERRIDE_SCOPE = {"global", "year", "branch"}
_YEAR_STR_RE = re.compile(r"^[1-9]$")
_BRANCH_STR_RE = re.compile(r"^[1-9][A-Z]$")


def _override_payload(doc: CalendarOverrideDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "date": doc.date,
        "kind": doc.kind,
        "reason": doc.reason,
        "follows_day": doc.follows_day,
        "scope": doc.scope,
        "scope_values": list(doc.scope_values or []),
    }


async def _seed_calendar_overrides_if_empty() -> None:
    if await CalendarOverrideDoc.find_all().count() > 0:
        return
    path = _ASSETS_DIR / "calendar_overrides.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("calendar-overrides seed skipped: %s", exc)
        return
    if not isinstance(data, list):
        return
    docs: list[CalendarOverrideDoc] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        date = str(raw.get("date") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        if not _DATE_RE.match(date) or kind not in _ALLOWED_OVERRIDE_KIND:
            continue
        follows_day = raw.get("follows_day")
        if kind == "follow_day":
            if not isinstance(follows_day, int) or not (0 <= follows_day <= 4):
                continue
        else:
            follows_day = None
        scope = str(raw.get("scope") or "global").strip().lower()
        if scope not in _ALLOWED_OVERRIDE_SCOPE:
            scope = "global"
        scope_values = raw.get("scope_values") or []
        if not isinstance(scope_values, list):
            scope_values = []
        docs.append(
            CalendarOverrideDoc(
                date=date,
                kind=kind,
                reason=(raw.get("reason") or None) or None,
                follows_day=follows_day,
                scope=scope,
                scope_values=[str(v) for v in scope_values],
            )
        )
    if docs:
        await CalendarOverrideDoc.insert_many(docs)


def _override_matches_batch(doc: CalendarOverrideDoc, year: int | None, branch: str | None) -> bool:
    """Return True if the override applies to a batch with the given year+branch."""
    if doc.scope == "global":
        return True
    values = [str(v).upper() for v in (doc.scope_values or [])]
    if doc.scope == "year":
        if year is None:
            return False
        return str(year) in values
    if doc.scope == "branch":
        if not branch:
            return False
        return branch.upper() in values
    return False


async def list_calendar_overrides(batch: str | None = None) -> list[dict[str, Any]]:
    """Return calendar overrides, filtered by ``batch`` when supplied.

    When ``batch`` is provided we compute the batch's year (1..5) and
    branch prefix (e.g. "2A" from "2A11") and return only overrides whose
    scope matches. When ``batch`` is missing all overrides are returned
    (used by the admin listing).
    """
    await _seed_calendar_overrides_if_empty()

    year: int | None = None
    branch: str | None = None
    if batch:
        try:
            code = _safe_batch(batch)
        except BatchNotFound:
            code = None
        if code:
            meta = _derive_batch_meta(code)
            year = meta.get("year")
            section = meta.get("section")
            if year is not None and section:
                branch = f"{year}{section[:1]}"

    out: list[dict[str, Any]] = []
    async for doc in CalendarOverrideDoc.find_all(sort=[("date", 1)]):
        if batch:
            if not _override_matches_batch(doc, year, branch):
                continue
        out.append(_override_payload(doc))
    return out


def _validate_override_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a cleaned dict ready to be handed to a CalendarOverrideDoc.

    Raises ``ValueError`` with an actionable message on invalid input.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    date = str(payload.get("date") or "").strip()
    if not _DATE_RE.match(date):
        raise ValueError("date must be YYYY-MM-DD")
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in _ALLOWED_OVERRIDE_KIND:
        raise ValueError(
            "kind must be one of: holiday, follow_day, mst, est, assessment, frosh"
        )
    follows_day: int | None = None
    if kind == "follow_day":
        raw_fd = payload.get("follows_day")
        try:
            follows_day = int(raw_fd)
        except (TypeError, ValueError) as exc:
            raise ValueError("follows_day must be an integer 0..4 (Mon..Fri)") from exc
        if not (0 <= follows_day <= 4):
            raise ValueError("follows_day must be 0..4 (Mon..Fri)")
    reason = payload.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            raise ValueError("reason must be a string when provided")
        reason = reason.strip() or None
    scope = str(payload.get("scope") or "global").strip().lower()
    if scope not in _ALLOWED_OVERRIDE_SCOPE:
        raise ValueError("scope must be 'global', 'year' or 'branch'")
    raw_values = payload.get("scope_values") or []
    if not isinstance(raw_values, list):
        raise ValueError("scope_values must be a list of strings")
    scope_values: list[str] = []
    for v in raw_values:
        s = str(v).strip().upper()
        if not s:
            continue
        if scope == "year" and not _YEAR_STR_RE.match(s):
            raise ValueError(f"scope_values[{v!r}] must be a single digit 1..9 for year scope")
        if scope == "branch" and not _BRANCH_STR_RE.match(s):
            raise ValueError(f"scope_values[{v!r}] must look like '2A' for branch scope")
        scope_values.append(s)
    if scope in {"year", "branch"} and not scope_values:
        raise ValueError(f"scope_values is required when scope is '{scope}'")
    if scope == "global":
        scope_values = []
    return {
        "date": date,
        "kind": kind,
        "reason": reason,
        "follows_day": follows_day,
        "scope": scope,
        "scope_values": scope_values,
    }


async def add_calendar_override(payload: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_override_payload(payload)
    doc = CalendarOverrideDoc(**clean)
    await doc.insert()
    return _override_payload(doc)


async def update_calendar_override(override_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    from beanie import PydanticObjectId
    try:
        oid = PydanticObjectId(override_id)
    except Exception:
        return None
    doc = await CalendarOverrideDoc.get(oid)
    if doc is None:
        return None
    clean = _validate_override_payload(payload)
    for key, value in clean.items():
        setattr(doc, key, value)
    await doc.save()
    return _override_payload(doc)


async def delete_calendar_override(override_id: str) -> bool:
    from beanie import PydanticObjectId
    try:
        oid = PydanticObjectId(override_id)
    except Exception:
        return False
    doc = await CalendarOverrideDoc.get(oid)
    if doc is None:
        return False
    await doc.delete()
    return True


async def reset_calendar_overrides() -> dict[str, int]:
    """Delete every CalendarOverrideDoc then re-seed from the bundled JSON."""
    deleted = await CalendarOverrideDoc.find_all().delete()
    deleted_count = getattr(deleted, "deleted_count", 0) or 0
    await _seed_calendar_overrides_if_empty()
    seeded = await CalendarOverrideDoc.find_all().count()
    return {"deleted": deleted_count, "seeded": seeded}


async def delete_calendar_overrides_in_range(
    start: str,
    end: str,
    *,
    scope: str | None = None,
    scope_values: list[str] | None = None,
) -> int:
    """Delete every calendar override whose date falls in [start, end]
    (inclusive) AND whose scope matches, then return how many were removed.

    Used by ``POST /admin/calendar/apply-plan`` to make a re-upload of the
    same PDF idempotent — we wipe the calendar's own date window before
    inserting the fresh plan.

    When ``scope`` is None, matches any scope. When ``scope_values`` is
    None, matches any values (scope-only filter).
    """
    if not (_DATE_RE.match(start) and _DATE_RE.match(end)):
        return 0
    query: dict[str, Any] = {"date": {"$gte": start, "$lte": end}}
    if scope:
        query["scope"] = scope
    if scope_values is not None:
        # Match documents whose scope_values equals the passed list exactly.
        # Order doesn't matter for equality on Mongo; sort both sides.
        query["scope_values"] = sorted(scope_values)
    # Beanie's find() accepts a Motor filter dict via `find({"$and":[...]})`
    # or via find_all with `.aggregate([{"$match": ...}])`. Simplest: use
    # the raw Motor collection.
    cursor = CalendarOverrideDoc.get_motor_collection().find(query)
    ids: list[Any] = []
    async for doc in cursor:
        ids.append(doc["_id"])
    if not ids:
        return 0
    result = await CalendarOverrideDoc.get_motor_collection().delete_many(
        {"_id": {"$in": ids}},
    )
    return int(result.deleted_count or 0)
