"""Free/busy queries over the cross-batch schedule index.

"Is LT102 free at 11:00 on Wednesday" and "which labs are free right now" are
both filters over :mod:`server.schedule_index`; nothing here touches the
database. A resource is busy when any occupancy overlaps the requested window,
and free otherwise — including resources that have no classes at all that day.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping

from server.schedule_index import (
    DAY_ORDER,
    Occupancy,
    ScheduleIndex,
    format_minute,
    parse_minute,
)

ResourceKind = Literal["room", "teacher"]


class UnknownResource(Exception):
    """Raised when a room or teacher code is not present in the index."""

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(f"unknown {kind}: {name}")
        self.kind = kind
        self.name = name


def _bucket(index: ScheduleIndex, kind: ResourceKind) -> Mapping[str, tuple[Occupancy, ...]]:
    return index.by_teacher if kind == "teacher" else index.by_room


def normalize_day(value: Any) -> str | None:
    """Accept ``mon``/``Monday``/``MONDAY`` and return the canonical name."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    for day in DAY_ORDER:
        if day.lower() == text or day.lower().startswith(text) and len(text) >= 3:
            return day
    return None


def list_resources(index: ScheduleIndex, kind: ResourceKind) -> list[dict[str, Any]]:
    """Every known room or teacher with a cheap summary of its load."""
    bucket = _bucket(index, kind)
    result: list[dict[str, Any]] = []
    for name in sorted(bucket):
        items = bucket[name]
        result.append(
            {
                "name": name,
                "class_count": len(items),
                "days": sorted({item.day for item in items}, key=DAY_ORDER.index),
            }
        )
    return result


def weekly_schedule(
    index: ScheduleIndex,
    kind: ResourceKind,
    name: str,
    *,
    include_teacher: bool = True,
) -> dict[str, Any]:
    """Return one resource's whole week, grouped by day."""
    bucket = _bucket(index, kind)
    items = bucket.get(name)
    if items is None:
        raise UnknownResource(kind, name)

    by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in index.days}
    for item in items:
        by_day.setdefault(item.day, []).append(item.public_dict(include_teacher=include_teacher))

    return {
        "kind": kind,
        "name": name,
        "semester": index.semester_label,
        "days": [
            {"day": day, "classes": by_day.get(day, [])}
            for day in DAY_ORDER
            if day in by_day
        ],
        "class_count": len(items),
    }


def _window(day: str, at: str | None, start: str | None, end: str | None) -> tuple[int, int]:
    """Resolve the query window, defaulting to a single instant."""
    if start is not None or end is not None:
        start_minute = parse_minute(start) if start else None
        end_minute = parse_minute(end) if end else None
        if start_minute is None or end_minute is None:
            raise ValueError("start and end must both be HH:MM")
        if end_minute <= start_minute:
            raise ValueError("end must be after start")
        return start_minute, end_minute
    instant = parse_minute(at)
    if instant is None:
        raise ValueError("at must be HH:MM")
    # A zero-length window would never overlap under half-open comparison, so
    # probe the single minute at the requested instant.
    return instant, instant + 1


def availability(
    index: ScheduleIndex,
    kind: ResourceKind,
    *,
    day: str,
    at: str | None = None,
    start: str | None = None,
    end: str | None = None,
    only: Iterable[str] | None = None,
    include_teacher: bool = True,
) -> dict[str, Any]:
    """Split every room/teacher into free and busy for one time window."""
    canonical_day = normalize_day(day)
    if canonical_day is None:
        raise ValueError(f"unknown day: {day!r}")
    start_minute, end_minute = _window(canonical_day, at, start, end)

    bucket = _bucket(index, kind)
    wanted = {str(value).strip() for value in only or [] if str(value).strip()}
    names = sorted(wanted & set(bucket)) if wanted else sorted(bucket)

    free: list[dict[str, Any]] = []
    busy: list[dict[str, Any]] = []
    for name in names:
        clashes = [
            item
            for item in bucket[name]
            if item.day == canonical_day and item.overlaps(start_minute, end_minute)
        ]
        if clashes:
            busy.append(
                {
                    "name": name,
                    "classes": [
                        item.public_dict(include_teacher=include_teacher) for item in clashes
                    ],
                }
            )
        else:
            free.append({"name": name, "next_class": _next_class(
                bucket[name], canonical_day, end_minute, include_teacher=include_teacher
            )})

    return {
        "kind": kind,
        "day": canonical_day,
        "start_time": format_minute(start_minute),
        "end_time": format_minute(end_minute if end_minute > start_minute + 1 else start_minute),
        "instant": at is not None and start is None and end is None,
        "semester": index.semester_label,
        "free": free,
        "busy": busy,
        "free_count": len(free),
        "busy_count": len(busy),
    }


def _next_class(
    items: Iterable[Occupancy],
    day: str,
    after_minute: int,
    *,
    include_teacher: bool,
) -> dict[str, Any] | None:
    """The soonest class later that day, so "free" carries a usable horizon."""
    upcoming = sorted(
        (item for item in items if item.day == day and item.start_minute >= after_minute),
        key=lambda item: item.start_minute,
    )
    if not upcoming:
        return None
    return upcoming[0].public_dict(include_teacher=include_teacher)


def free_windows(
    index: ScheduleIndex,
    kind: ResourceKind,
    name: str,
    *,
    day: str,
    day_start: str = "08:00",
    day_end: str = "18:50",
) -> dict[str, Any]:
    """Collapse one resource's gaps on a day into contiguous free windows."""
    canonical_day = normalize_day(day)
    if canonical_day is None:
        raise ValueError(f"unknown day: {day!r}")
    bucket = _bucket(index, kind)
    items = bucket.get(name)
    if items is None:
        raise UnknownResource(kind, name)

    lower = parse_minute(day_start)
    upper = parse_minute(day_end)
    if lower is None or upper is None or upper <= lower:
        raise ValueError("day_start and day_end must be HH:MM with end after start")

    busy = sorted(
        (
            (max(item.start_minute, lower), min(item.end_minute, upper))
            for item in items
            if item.day == canonical_day and item.overlaps(lower, upper)
        )
    )

    windows: list[dict[str, str]] = []
    cursor = lower
    for start_minute, end_minute in busy:
        if start_minute > cursor:
            windows.append({"start_time": format_minute(cursor), "end_time": format_minute(start_minute)})
        cursor = max(cursor, end_minute)
    if cursor < upper:
        windows.append({"start_time": format_minute(cursor), "end_time": format_minute(upper)})

    return {
        "kind": kind,
        "name": name,
        "day": canonical_day,
        "free_windows": windows,
        "busy_count": len(busy),
    }
