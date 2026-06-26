"""Adapter from parser ClassBlocks to the frontend HTTP contract shape.

Contract decisions (Phase 0):
- snake_case JSON.
- Multi-period blocks split into one entry per slot.
- All seven parsed days (Mon..Sat) included; frontend ignores unknown days.
- type values are Title-case: "Lecture" | "Tutorial" | "Practical" | "Unknown".
"""

from __future__ import annotations

from collections.abc import Iterable

from timetable_parser.core.elective_parser import is_place_like, should_skip_metadata
from timetable_parser.core.models import ClassBlock, ElectiveOption
from timetable_parser.core.sheet_geometry import end_time as compute_end_time

DAY_TITLE_CASE = {
    "MONDAY": "Monday",
    "TUESDAY": "Tuesday",
    "WEDNESDAY": "Wednesday",
    "THURSDAY": "Thursday",
    "FRIDAY": "Friday",
    "SATURDAY": "Saturday",
    "SUNDAY": "Sunday",
}

TYPE_TITLE_CASE = {
    "LECTURE": "Lecture",
    "TUTORIAL": "Tutorial",
    "PRACTICAL": "Practical",
    "UNKNOWN": "Unknown",
}


def class_blocks_to_api(
    blocks_by_batch_day: dict[str, dict[str, list[ClassBlock]]],
    semester_label: str,
) -> dict[str, dict[str, object]]:
    """Return {batch_code: <timetable payload>} for every batch in the parser output."""
    return {
        batch: _timetable_payload(batch, day_blocks, semester_label)
        for batch, day_blocks in blocks_by_batch_day.items()
    }


def batch_list(blocks_by_batch_day: dict[str, dict[str, list[ClassBlock]]]) -> list[str]:
    return sorted(blocks_by_batch_day.keys())


def semester_payload(semester_label: str) -> dict[str, str]:
    return {"label": semester_label}


def _timetable_payload(
    batch: str,
    day_blocks: dict[str, list[ClassBlock]],
    semester_label: str,
) -> dict[str, object]:
    classes: list[dict[str, object]] = []
    for day_upper, blocks in day_blocks.items():
        day = DAY_TITLE_CASE.get(day_upper.upper(), day_upper.title())
        for block in blocks:
            classes.extend(_split_block(block, day))
    classes.sort(key=lambda entry: (_DAY_ORDER.get(entry["day"], 99), entry["start_time"]))
    return {
        "batch": batch,
        "semester": semester_payload(semester_label),
        "classes": classes,
    }


def _split_block(block: ClassBlock, day: str) -> Iterable[dict[str, object]]:
    """Expand a ClassBlock into one entry per slot it occupies."""
    type_title = TYPE_TITLE_CASE.get((block.type or "UNKNOWN").upper(), "Unknown")
    subject = _resolve_subject(block)
    room = _resolve_room(block)
    options = [_option_payload(option) for option in block.options]

    for period_index in range(block.periods):
        start = compute_end_time(block.start_time, period_index)
        end = compute_end_time(block.start_time, period_index + 1)
        yield {
            "day": day,
            "start_time": start,
            "end_time": end,
            "subject": subject,
            "code": block.subject_code,
            "type": type_title,
            "room": room,
            "options": options,
        }


def _resolve_subject(block: ClassBlock) -> str | None:
    if block.subject_name:
        return block.subject_name
    if block.options:
        first = block.options[0]
        return first.subject_name or first.subject_code
    return block.subject_code


def _resolve_room(block: ClassBlock) -> str | None:
    if block.options:
        for option in block.options:
            if option.place:
                return option.place
        return None
    return _first_place_like(block.raw, block.subject_name)


def _first_place_like(raw: list[str], subject_name: str | None) -> str | None:
    skip = set()
    if subject_name:
        skip.add(subject_name.strip().upper())
    for value in raw:
        if should_skip_metadata(value):
            continue
        normalized = value.strip().upper()
        if normalized in skip:
            continue
        if is_place_like(value.strip()):
            return value.strip()
    return None


def _option_payload(option: ElectiveOption) -> dict[str, object]:
    return {
        "subject_code": option.subject_code,
        "subject_name": option.subject_name,
        "type": TYPE_TITLE_CASE.get((option.type or "UNKNOWN").upper(), "Unknown"),
        "place": option.place,
        "teacher": option.teacher,
    }


_DAY_ORDER = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}
