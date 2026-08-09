"""What the user's Google Calendar should contain — computed, not pushed.

This module is pure: no database, no network, no clock. Given a batch's
classes, the user's personal operations already applied, the campus calendar
overrides and the term dates, it returns the exact set of events that *should*
exist. The reconciler then makes Google match.

Keeping it pure is what makes the two hard requirements testable:

**Nothing false.** An event is emitted only for a class the student actually
attends. The grid decides that with one filter::

    .filter((entry) => !entry.electiveDismissed && !isUnresolvedLibraryElective(entry))

so this module applies the same rule. An elective the student has not chosen
yet produces **no event at all** — putting "Elective" in someone's calendar
claims a class they may never attend. An elective they resolved by picking a
course offered in a different slot is marked dismissed, which means that slot
is genuinely free for them, so it produces no event either.

**Nothing duplicated.** Every event carries a ``slot_id`` derived from the
things that identify it (batch, day, time, course, kind) and a ``fingerprint``
over everything that is displayed. The reconciler keys on ``slot_id``, so
re-running a sync converges instead of accumulating.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Literal, Mapping, Sequence

TIMEZONE = "Asia/Kolkata"

WEEKDAY_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Google's palette. Practicals read as the heaviest commitment, so they get the
# strongest colour; tutorials sit between them and lectures.
COLOR_BY_TYPE = {"lecture": "9", "tutorial": "5", "practical": "11"}

# Campus overrides that cancel the day's normal teaching and show as a banner.
BLOCKING_KINDS = frozenset({"mst", "est", "assessment", "frosh"})

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$", re.I)


class ProjectionError(ValueError):
    """Raised when inputs cannot produce a meaningful calendar."""


# ── Small pure helpers ───────────────────────────────────────────────────
def parse_time(value: Any) -> str | None:
    """``"9:40"`` or ``"09:40 AM"`` → ``"09:40:00"``. None when unusable."""
    match = _TIME_RE.match(str(value or ""))
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    if meridiem:
        upper = meridiem.upper()
        if upper == "PM" and hour != 12:
            hour += 12
        elif upper == "AM" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}:00"


def minutes_of(value: Any) -> int | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    hour, minute, _ = parsed.split(":")
    return int(hour) * 60 + int(minute)


def day_index(value: Any) -> int | None:
    return _DAY_INDEX.get(str(value or "").strip().lower())


def first_on_or_after(weekday: int, start: date) -> date:
    """First ``weekday`` (0=Mon) falling on or after ``start``."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ── The visibility rule ──────────────────────────────────────────────────
def is_visible_to_student(entry: Mapping[str, Any]) -> bool:
    """Does the student's grid render this cell as a class they attend?

    Mirrors the frontend filter. Two ways an elective cell is *not* a class:

    * no choice made yet — the grid shows a picker, not a class;
    * a choice was made elsewhere in the group and this slot has no matching
      option, so the grid marks it dismissed and the slot is free.
    """
    if entry.get("electiveDismissed") is True or entry.get("elective_dismissed") is True:
        return False
    options = entry.get("options") or []
    chosen = entry.get("electiveChoice") or entry.get("elective_choice")
    if len(options) > 1 and not chosen:
        return False
    # A Library-classified elective awaiting selection carries the group id but
    # no choice; the grid hides it the same way.
    if (entry.get("elective_group_id") or entry.get("electiveGroupId")) and not chosen:
        if entry.get("requires_selection") or entry.get("requiresSelection"):
            return False
    return True


def visible_classes(classes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in classes if is_visible_to_student(entry)]


# ── Merging consecutive periods ──────────────────────────────────────────
def _merge_key(entry: Mapping[str, Any]) -> tuple:
    return (
        _clean(entry.get("day")),
        _clean(entry.get("code")).upper(),
        _clean(entry.get("type")).lower(),
        _clean(entry.get("room")).upper(),
        entry.get("alternate_week_start"),
    )


def merge_adjacent(classes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Join back-to-back periods of the same class into one event.

    A two-hour lab is two 50-minute rows in the grid; as a calendar entry it
    should read as one block. Only truly contiguous rows merge — a gap means
    two separate sittings and stays two events.
    """
    buckets: dict[tuple, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for raw in classes:
        entry = dict(raw)
        if minutes_of(entry.get("start_time")) is None or minutes_of(entry.get("end_time")) is None:
            passthrough.append(entry)
            continue
        buckets.setdefault(_merge_key(entry), []).append(entry)

    merged: list[dict[str, Any]] = []
    for group in buckets.values():
        group.sort(key=lambda item: minutes_of(item.get("start_time")) or 0)
        current = dict(group[0])
        for nxt in group[1:]:
            if minutes_of(current.get("end_time")) == minutes_of(nxt.get("start_time")):
                current["end_time"] = nxt.get("end_time")
            else:
                merged.append(current)
                current = dict(nxt)
        merged.append(current)
    return merged + passthrough


# ── The output ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DesiredEvent:
    slot_id: str
    kind: Literal["class", "follow_day", "all_day"]
    summary: str
    description: str
    start_date: str                      # yyyy-mm-dd of the first occurrence
    start_time: str | None               # HH:MM:SS, None for all-day
    end_time: str | None
    recurrence: tuple[str, ...] = ()
    color_id: str | None = None
    source: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def fingerprint(self) -> str:
        """Hash of everything the user can see, so drift is detectable."""
        return _digest({
            "kind": self.kind,
            "summary": self.summary,
            "description": self.description,
            "start_date": self.start_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "recurrence": sorted(self.recurrence),
            "color_id": self.color_id,
        })


def _slot_id(*parts: Any) -> str:
    return "s_" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


def course_title(entry: Mapping[str, Any], catalog: Any = None) -> str:
    """Prefer a human name; fall back to the code rather than inventing one."""
    subject = _clean(entry.get("subject"))
    code = _clean(entry.get("code"))
    if subject and subject.upper() != code.upper():
        return subject
    if code and catalog is not None:
        resolved = catalog.name_for(code)
        if resolved:
            return str(resolved)
    return subject or code or "Class"


def _describe(entry: Mapping[str, Any], batch: str, catalog: Any = None) -> str:
    lines = []
    code = _clean(entry.get("code"))
    if code:
        lines.append(f"Course: {code}")
    kind = _clean(entry.get("type"))
    if kind:
        lines.append(f"Type: {kind}")
    room = _clean(entry.get("room"))
    if room:
        lines.append(f"Room: {room}")
    teacher = _clean(entry.get("teacher"))
    if teacher:
        lines.append(f"Faculty: {teacher}")
    lines.append(f"Batch: {batch}")
    lines.append("Synced from MLSC Timetable.")
    return "\n".join(lines)


def _summary(entry: Mapping[str, Any], catalog: Any = None) -> str:
    title = course_title(entry, catalog)
    room = _clean(entry.get("room"))
    return f"{title} ({room})" if room else title


def project(
    *,
    batch: str,
    classes: Iterable[Mapping[str, Any]],
    overrides: Iterable[Mapping[str, Any]] = (),
    term_start: str,
    term_end: str,
    catalog: Any = None,
) -> list[DesiredEvent]:
    """The complete set of events the user's calendar should hold.

    ``classes`` must already have the user's personal operations applied — this
    function decides visibility, not personalisation.

    ``overrides`` must already be scoped to the batch. Every override in
    production carries ``scope: "year"``, so handing this the unfiltered list
    would put another year's exam weeks in the student's calendar. Use
    ``storage.list_calendar_overrides(batch=...)``, which does the filtering.
    """
    code = _clean(batch).upper()
    if not code:
        raise ProjectionError("batch is required")
    try:
        start_date = date.fromisoformat(term_start)
        end_date = date.fromisoformat(term_end)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("term_start and term_end must be yyyy-mm-dd") from exc
    if end_date < start_date:
        raise ProjectionError("term_end precedes term_start")

    attended = merge_adjacent(visible_classes(classes))

    # ── Campus overrides: what cancels teaching, and what replaces it ────
    holidays: set[str] = set()
    follow_days: list[Mapping[str, Any]] = []
    banners: list[Mapping[str, Any]] = []
    cancelled: set[str] = set()
    for override in overrides:
        kind = _clean(override.get("kind")).lower()
        on = _clean(override.get("date"))
        try:
            date.fromisoformat(on)
        except ValueError:
            continue
        if kind == "holiday":
            holidays.add(on)
            cancelled.add(on)
        elif kind == "follow_day":
            follow_days.append(override)
            cancelled.add(on)
        elif kind in BLOCKING_KINDS:
            banners.append(override)
            cancelled.add(on)

    until = end_date.strftime("%Y%m%d") + "T235959Z"
    events: list[DesiredEvent] = []

    # ── Weekly classes ──────────────────────────────────────────────────
    for entry in attended:
        weekday = day_index(entry.get("day"))
        start_hms = parse_time(entry.get("start_time"))
        end_hms = parse_time(entry.get("end_time"))
        if weekday is None or start_hms is None or end_hms is None:
            continue
        if weekday > 5:                      # Sunday is never a teaching day
            continue

        anchor = first_on_or_after(weekday, start_date)
        if anchor > end_date:
            continue

        alternate = entry.get("alternate_week_start")
        interval = 2 if alternate in (1, 2) else 1
        if interval == 2 and alternate == 2:
            anchor = anchor + timedelta(days=7)
            if anchor > end_date:
                continue

        rule = f"RRULE:FREQ=WEEKLY;BYDAY={WEEKDAY_BYDAY[weekday]};UNTIL={until}"
        if interval == 2:
            rule = rule.replace("FREQ=WEEKLY;", "FREQ=WEEKLY;INTERVAL=2;")
        recurrence = [rule]

        # Skip the dates where teaching is cancelled, but only those this
        # class would actually have fallen on.
        compact = start_hms.replace(":", "")
        excluded = sorted(
            on for on in cancelled
            if date.fromisoformat(on).weekday() == weekday
            and start_date <= date.fromisoformat(on) <= end_date
        )
        for on in excluded:
            recurrence.append(f"EXDATE;TZID={TIMEZONE}:{on.replace('-', '')}T{compact}")

        slot = _slot_id(code, "class", entry.get("day"), start_hms,
                        _clean(entry.get("code")).upper(), _clean(entry.get("type")).lower())
        events.append(DesiredEvent(
            slot_id=slot,
            kind="class",
            summary=_summary(entry, catalog),
            description=_describe(entry, code, catalog),
            start_date=anchor.isoformat(),
            start_time=start_hms,
            end_time=end_hms,
            recurrence=tuple(recurrence),
            color_id=COLOR_BY_TYPE.get(_clean(entry.get("type")).lower()),
            source=entry,
        ))

    # ── Follow-day one-offs ─────────────────────────────────────────────
    by_weekday: dict[int, list[dict[str, Any]]] = {}
    for entry in attended:
        weekday = day_index(entry.get("day"))
        if weekday is not None:
            by_weekday.setdefault(weekday, []).append(entry)

    for override in follow_days:
        on = _clean(override.get("date"))
        if on in holidays:                   # a holiday outranks a follow-day
            continue
        follows = override.get("follows_day")
        if not isinstance(follows, int) or not 0 <= follows <= 5:
            continue
        if not (start_date <= date.fromisoformat(on) <= end_date):
            continue
        for entry in by_weekday.get(follows, []):
            start_hms = parse_time(entry.get("start_time"))
            end_hms = parse_time(entry.get("end_time"))
            if start_hms is None or end_hms is None:
                continue
            slot = _slot_id(code, "follow", on, start_hms,
                            _clean(entry.get("code")).upper(), _clean(entry.get("type")).lower())
            events.append(DesiredEvent(
                slot_id=slot,
                kind="follow_day",
                summary=_summary(entry, catalog),
                description=_describe(entry, code, catalog)
                            + f"\n\nRescheduled: {on} follows the usual "
                              f"{list(_DAY_INDEX)[follows].capitalize()} timetable.",
                start_date=on,
                start_time=start_hms,
                end_time=end_hms,
                color_id=COLOR_BY_TYPE.get(_clean(entry.get("type")).lower()),
                source=entry,
            ))

    # ── All-day banners for exam and assessment periods ─────────────────
    for override in banners:
        on = _clean(override.get("date"))
        if not (start_date <= date.fromisoformat(on) <= end_date):
            continue
        kind = _clean(override.get("kind")).upper()
        reason = _clean(override.get("reason"))
        events.append(DesiredEvent(
            slot_id=_slot_id(code, "allday", on, kind),
            kind="all_day",
            summary=reason or kind,
            description=f"{kind} period.\nNo regular classes.\nSynced from MLSC Timetable.",
            start_date=on,
            start_time=None,
            end_time=None,
            source=dict(override),
        ))

    # Two rows can legitimately reduce to the same identity (a duplicated cell
    # in the workbook). Collapse them here so the reconciler never sees a
    # conflict it would resolve by creating twice.
    unique: dict[str, DesiredEvent] = {}
    for event in events:
        unique.setdefault(event.slot_id, event)
    return sorted(unique.values(), key=lambda e: (e.start_date, e.start_time or "", e.summary))
