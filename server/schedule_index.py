"""Cross-batch room and teacher schedules.

The stored timetables are batch-shaped: one physical lecture is repeated in
every attending batch's document, so ``UCS503L`` in ``LT102`` on Monday 08:00
appears twenty times. Reading rooms or teachers straight from those documents
would show the same class over and over, so this module folds duplicates into a
single :class:`Occupancy` that carries the attending batches instead.

Elective cells are exploded before folding: each option books its own room and
teacher, so a four-option elective occupies four rooms in that slot even though
a given student only attends one of them.

The index is rebuilt from every timetable document, which is far too expensive
to do per request. It is cached in-process and invalidated by the same writes
that republish public snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from server.config import Settings, get_settings
from server.curriculum_projection import base_course_code, resolve_curriculum_context
from server.room_names import normalize_room

logger = logging.getLogger(__name__)


# Ordered so a weekly grid renders in the usual reading order. Sunday is not a
# teaching day but is tolerated if a workbook ever produces it.
DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_RANK = {day: index for index, day in enumerate(DAY_ORDER)}

# Clash severity. A practical cannot be skipped, a tutorial is harder to skip
# than a lecture. ``Elective`` cells take the strongest severity among their
# options because the student must attend one of them.
SEVERITY_LECTURE = 0
SEVERITY_TUTORIAL = 1
SEVERITY_PRACTICAL = 2

_TYPE_SEVERITY = {
    "lecture": SEVERITY_LECTURE,
    "tutorial": SEVERITY_TUTORIAL,
    "practical": SEVERITY_PRACTICAL,
    "lab": SEVERITY_PRACTICAL,
}
_SEVERITY_NAME = {
    SEVERITY_LECTURE: "Lecture",
    SEVERITY_TUTORIAL: "Tutorial",
    SEVERITY_PRACTICAL: "Practical",
}


def severity_for_type(value: Any) -> int:
    """Map a class type to a clash severity, defaulting to the mildest."""
    return _TYPE_SEVERITY.get(str(value or "").strip().lower(), SEVERITY_LECTURE)


def severity_name(severity: int) -> str:
    return _SEVERITY_NAME.get(severity, "Lecture")


def parse_minute(value: Any) -> int | None:
    """Parse ``"HH:MM"`` into minutes past midnight."""
    text = str(value or "").strip()
    if not text:
        return None
    # Tolerate "9:00 AM" style values in case older rows survive anywhere.
    meridiem = ""
    upper = text.upper()
    for suffix in ("AM", "PM"):
        if upper.endswith(suffix):
            meridiem = suffix
            text = upper[: -len(suffix)].strip()
            break
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_minute(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


@dataclass(frozen=True)
class Occupancy:
    """One physical class, shared by every batch that attends it."""

    day: str
    start_time: str
    end_time: str
    start_minute: int
    end_minute: int
    code: str | None
    subject: str | None
    type: str
    room: str | None          # canonical id, e.g. "L307"
    room_label: str | None    # as written in the workbook, e.g. "AI(L307)"
    teacher: str | None
    batches: tuple[str, ...]
    from_elective: bool
    alternate_week_start: str | None

    @property
    def severity(self) -> int:
        return severity_for_type(self.type)

    def overlaps(self, start_minute: int, end_minute: int) -> bool:
        """Half-open overlap so a class ending at 08:50 frees the 08:50 slot."""
        return self.start_minute < end_minute and start_minute < self.end_minute

    def covers(self, minute: int) -> bool:
        return self.start_minute <= minute < self.end_minute

    def public_dict(self, *, include_teacher: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "day": self.day,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "code": self.code,
            "subject": self.subject,
            "type": self.type,
            "room": self.room,
            "room_label": self.room_label,
            "batches": list(self.batches),
            "from_elective": self.from_elective,
        }
        if self.alternate_week_start:
            payload["alternate_week_start"] = self.alternate_week_start
        if include_teacher:
            payload["teacher"] = self.teacher
        return payload


@dataclass(frozen=True)
class ScheduleIndex:
    """Every occupancy this term, grouped by room, teacher and course."""

    built_at: datetime
    semester_label: str | None
    occupancies: tuple[Occupancy, ...]
    by_room: Mapping[str, tuple[Occupancy, ...]]
    by_teacher: Mapping[str, tuple[Occupancy, ...]]
    by_code: Mapping[str, tuple[Occupancy, ...]]
    by_batch: Mapping[str, tuple[Occupancy, ...]]
    batch_semester: Mapping[str, int]
    slots: tuple[tuple[str, str], ...]
    days: tuple[str, ...]
    # Batches whose stored term label differs from the current one — their last
    # ingest missed them, so their data may describe a previous semester.
    stale_term_batches: frozenset[str]

    @property
    def rooms(self) -> list[str]:
        return sorted(self.by_room)

    @property
    def teachers(self) -> list[str]:
        return sorted(self.by_teacher)


def _sort_key(item: Occupancy) -> tuple[int, int, int, str]:
    return (
        _DAY_RANK.get(item.day, len(DAY_ORDER)),
        item.start_minute,
        item.end_minute,
        item.room or "",
    )


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _record(
    *,
    code: Any,
    subject: Any,
    type_: Any,
    room: Any,
    teacher: Any,
    from_elective: bool,
) -> dict[str, Any]:
    rooms, room_label = normalize_room(room)
    return {
        "code": _clean(code),
        "subject": _clean(subject),
        "type": _clean(type_) or "Unknown",
        "rooms": rooms,
        "room_label": room_label,
        "teacher": _clean(teacher),
        "from_elective": from_elective,
    }


def _expand_entry(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Split one stored class into the concrete rooms/teachers it books.

    An elective cell has no room of its own — each option does — so it yields
    one record per option. Every other cell yields exactly one record.
    """
    options = entry.get("options")
    if isinstance(options, list) and options:
        expanded = [
            _record(
                code=option.get("subject_code"),
                subject=option.get("subject_name"),
                type_=option.get("type"),
                room=option.get("place"),
                teacher=option.get("teacher"),
                from_elective=True,
            )
            for option in options
            if isinstance(option, Mapping)
        ]
        if expanded:
            return expanded
    return [
        _record(
            code=entry.get("code"),
            subject=entry.get("subject"),
            type_=entry.get("type"),
            room=entry.get("room"),
            teacher=entry.get("teacher"),
            from_elective=False,
        )
    ]


def build_index(
    documents: Iterable[tuple[str, str | None, Sequence[Any]]],
    *,
    semester_label: str | None = None,
    catalog: Any = None,
) -> ScheduleIndex:
    """Fold ``(batch_code, semester_label, classes)`` triples into an index.

    Pure and database-free so tests can drive it with literal timetables.
    """
    # key -> (record fields, set of batches)
    folded: dict[tuple, dict[str, Any]] = {}
    batch_semester: dict[str, int] = {}
    stale_term_batches: set[str] = set()
    slots: set[tuple[int, int]] = set()
    days: set[str] = set()

    for batch_code, doc_label, classes in documents:
        batch = str(batch_code or "").strip().upper()
        if not batch:
            continue
        # Semester parity must come from the term everyone is actually in.
        # Individual documents keep whatever label their last ingest wrote, so
        # a batch missed by the latest upload still says "EVEN 25-26" and would
        # otherwise resolve to an even semester in the middle of an odd term.
        if doc_label and semester_label and doc_label != semester_label:
            stale_term_batches.add(batch)
        label = semester_label or doc_label
        try:
            batch_semester[batch] = resolve_curriculum_context(batch, label or "").semester
        except ValueError:
            # Batches whose code or term label cannot be resolved still belong
            # in room/teacher views; they are simply not offered for improvement.
            pass

        for raw in classes or []:
            entry = raw.model_dump(exclude_none=False) if hasattr(raw, "model_dump") else dict(raw)
            day = _clean(entry.get("day"))
            start_minute = parse_minute(entry.get("start_time"))
            end_minute = parse_minute(entry.get("end_time"))
            if not day or start_minute is None or end_minute is None:
                continue
            if end_minute <= start_minute:
                continue
            days.add(day)
            slots.add((start_minute, end_minute))
            alternate = _clean(entry.get("alternate_week_start"))

            for record in _expand_entry(entry):
                if not record["rooms"] and not record["teacher"] and not record["code"]:
                    # Nothing to index this against at all.
                    continue
                # A class with a course code but no room or teacher still runs:
                # 95 rows across 45 batches look like this. It cannot appear in
                # a room or teacher view — those buckets skip it below — but it
                # is a real commitment, so improvement planning must see it.
                subject = record["subject"]
                if not subject and record["code"] and catalog is not None:
                    subject = catalog.name_for(record["code"]) or None
                # A class listed as "B204/F314" books both rooms.
                for room in record["rooms"] or [None]:
                    key = (
                        day,
                        start_minute,
                        end_minute,
                        room or "",
                        record["teacher"] or "",
                        record["code"] or "",
                        record["type"],
                        alternate or "",
                    )
                    slot = folded.get(key)
                    if slot is None:
                        folded[key] = {
                            "day": day,
                            "start_minute": start_minute,
                            "end_minute": end_minute,
                            "code": record["code"],
                            "subject": subject,
                            "type": record["type"],
                            "room": room,
                            "room_label": record["room_label"],
                            "teacher": record["teacher"],
                            "from_elective": record["from_elective"],
                            "alternate_week_start": alternate,
                            "batches": {batch},
                        }
                    else:
                        slot["batches"].add(batch)
                        if not slot["subject"] and subject:
                            slot["subject"] = subject
                        # A cell folded from both a plain class and an elective
                        # option is still a real booking; keep the stricter label.
                        slot["from_elective"] = slot["from_elective"] and record["from_elective"]

    occupancies = tuple(
        sorted(
            (
                Occupancy(
                    day=item["day"],
                    start_time=format_minute(item["start_minute"]),
                    end_time=format_minute(item["end_minute"]),
                    start_minute=item["start_minute"],
                    end_minute=item["end_minute"],
                    code=item["code"],
                    subject=item["subject"],
                    type=item["type"],
                    room=item["room"],
                    room_label=item["room_label"],
                    teacher=item["teacher"],
                    batches=tuple(sorted(item["batches"])),
                    from_elective=item["from_elective"],
                    alternate_week_start=item["alternate_week_start"],
                )
                for item in folded.values()
            ),
            key=_sort_key,
        )
    )

    by_room: dict[str, list[Occupancy]] = {}
    by_teacher: dict[str, list[Occupancy]] = {}
    by_code: dict[str, list[Occupancy]] = {}
    by_batch: dict[str, list[Occupancy]] = {}
    for item in occupancies:
        if item.room:
            by_room.setdefault(item.room, []).append(item)
        if item.teacher:
            by_teacher.setdefault(item.teacher, []).append(item)
        if item.code:
            by_code.setdefault(base_course_code(item.code), []).append(item)
        for batch in item.batches:
            by_batch.setdefault(batch, []).append(item)

    ordered_slots = tuple(
        (format_minute(start), format_minute(end)) for start, end in sorted(slots)
    )
    ordered_days = tuple(day for day in DAY_ORDER if day in days)

    return ScheduleIndex(
        built_at=datetime.now(timezone.utc),
        semester_label=semester_label,
        occupancies=occupancies,
        by_room={key: tuple(value) for key, value in by_room.items()},
        by_teacher={key: tuple(value) for key, value in by_teacher.items()},
        by_code={key: tuple(value) for key, value in by_code.items()},
        by_batch={key: tuple(value) for key, value in by_batch.items()},
        batch_semester=batch_semester,
        slots=ordered_slots,
        days=ordered_days,
        stale_term_batches=frozenset(stale_term_batches),
    )


# ── Cached access ────────────────────────────────────────────────────────
@dataclass
class _Cache:
    index: ScheduleIndex | None = None
    built_monotonic: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_cache = _Cache()


def invalidate() -> None:
    """Drop the cached index after a write that changes any timetable."""
    _cache.index = None
    _cache.built_monotonic = 0.0


async def _load_documents() -> tuple[list[tuple[str, str | None, list[Any]]], str | None]:
    from server import storage
    from server.db.models import TimetableDoc

    documents: list[tuple[str, str | None, list[Any]]] = []
    async for doc in TimetableDoc.find_all():
        documents.append((doc.code, doc.semester, list(doc.classes or [])))

    label: str | None = None
    try:
        current = await storage.read_current()
        label = str(current.get("label") or "") or None
    except Exception:
        label = None
    return documents, label


async def get_index(settings: Settings | None = None, *, force: bool = False) -> ScheduleIndex:
    """Return the cached index, rebuilding when stale or invalidated."""
    settings = settings or get_settings()
    ttl = max(0.0, float(settings.schedule_index_ttl_seconds))
    now = time.monotonic()
    cached = _cache.index
    if not force and cached is not None and (now - _cache.built_monotonic) < ttl:
        return cached

    async with _cache.lock:
        # Another waiter may have rebuilt while this one queued on the lock.
        now = time.monotonic()
        cached = _cache.index
        if not force and cached is not None and (now - _cache.built_monotonic) < ttl:
            return cached

        started = time.monotonic()
        documents, label = await _load_documents()
        try:
            from timetable_parser.core.subject_catalog import ensure_catalog

            catalog = await ensure_catalog()
        except Exception:
            logger.exception("schedule index: subject catalog unavailable")
            catalog = None
        index = build_index(documents, semester_label=label, catalog=catalog)
        _cache.index = index
        _cache.built_monotonic = time.monotonic()
        logger.info(
            "schedule index built: %d occupancies, %d rooms, %d teachers in %.0f ms",
            len(index.occupancies),
            len(index.by_room),
            len(index.by_teacher),
            (time.monotonic() - started) * 1000,
        )
        return index
