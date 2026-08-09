"""Decide what to change in Google — without touching it.

Planning is separated from applying so the risky part is a pure function. The
planner takes what should exist (the projection), what we last recorded
(the local mirror) and, when needed, what Google currently holds, and returns
the operations that make them agree.

Three properties matter and each is a rule here.

**Nothing duplicated.** Every operation is keyed by ``slot_id``. A desired
event either matches something already there or is created exactly once; two
remote events claiming the same slot means one is a leftover and gets removed.

**Nothing destroyed.** Only events we created — the ones carrying our
``mlscSlotId`` — are ever deleted. An event a student added to the calendar
themselves is left alone, and reported so it is visible rather than silently
dropped.

**Nothing rebuilt needlessly.** A content change is a patch, not a
delete-and-recreate: recreating a recurring series discards the reminders the
user set on it and fires a second round of notifications. On the first run
after deploy the mirror is empty, so events already in Google are *adopted* by
slot id instead of recreated — which is what keeps this rewrite invisible to
the 35 people already syncing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from server.gcal.projection import DesiredEvent

# Where we stamp our identity on a Google event.
SLOT_PROPERTY = "mlscSlotId"
FINGERPRINT_PROPERTY = "mlscFingerprint"


@dataclass(frozen=True)
class RemoteEvent:
    """An event as Google reports it, reduced to what reconciliation needs."""

    event_id: str
    slot_id: str | None = None
    fingerprint: str | None = None

    @classmethod
    def from_google(cls, payload: Mapping[str, Any]) -> "RemoteEvent":
        private = ((payload.get("extendedProperties") or {}).get("private") or {})
        return cls(
            event_id=str(payload.get("id") or ""),
            slot_id=private.get(SLOT_PROPERTY) or None,
            fingerprint=private.get(FINGERPRINT_PROPERTY) or None,
        )

    @property
    def is_ours(self) -> bool:
        return bool(self.slot_id)


@dataclass(frozen=True)
class MirrorRow:
    """What we last recorded about one slot."""

    slot_id: str
    event_id: str
    fingerprint: str


@dataclass
class Plan:
    create: list[DesiredEvent] = field(default_factory=list)
    patch: list[tuple[str, DesiredEvent]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    adopt: list[tuple[str, DesiredEvent]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not (self.create or self.patch or self.delete or self.adopt)

    @property
    def writes(self) -> int:
        """Calls that actually change Google. Adoption is local bookkeeping."""
        return len(self.create) + len(self.patch) + len(self.delete)

    def summary(self) -> dict[str, int]:
        return {
            "create": len(self.create),
            "patch": len(self.patch),
            "delete": len(self.delete),
            "adopt": len(self.adopt),
            "unchanged": len(self.unchanged),
            "foreign": len(self.foreign),
        }


def plan_sync(
    desired: Sequence[DesiredEvent],
    mirror: Iterable[MirrorRow] = (),
    remote: Iterable[RemoteEvent] | None = None,
    *,
    delete_foreign: bool = False,
) -> Plan:
    """Work out the operations that make Google match ``desired``.

    ``remote`` is optional. Pass it on the first sync for a user (empty mirror)
    or when a drift check is due; otherwise the mirror alone is enough and no
    listing call is needed. When it is omitted, the mirror is trusted.
    """
    result = Plan()

    by_slot_desired: dict[str, DesiredEvent] = {}
    for event in desired:
        # The projection already collapses duplicates; this is a second guard so
        # a caller passing raw events can never produce two creates for one slot.
        by_slot_desired.setdefault(event.slot_id, event)

    mirror_by_slot = {row.slot_id: row for row in mirror}

    if remote is None:
        # Trust the mirror. Anything recorded but no longer wanted is removed;
        # anything wanted but unrecorded is created.
        for slot, event in by_slot_desired.items():
            row = mirror_by_slot.get(slot)
            if row is None:
                result.create.append(event)
            elif row.fingerprint != event.fingerprint:
                result.patch.append((row.event_id, event))
            else:
                result.unchanged.append(slot)
        for slot, row in mirror_by_slot.items():
            if slot not in by_slot_desired:
                result.delete.append(row.event_id)
        return result

    # Google is authoritative for what exists. Index it, and treat a second
    # event claiming a slot as a leftover to remove rather than a second truth.
    remote_by_slot: dict[str, RemoteEvent] = {}
    for event in remote:
        if not event.event_id:
            continue
        if not event.is_ours:
            result.foreign.append(event.event_id)
            if delete_foreign:
                result.delete.append(event.event_id)
            continue
        existing = remote_by_slot.get(event.slot_id or "")
        if existing is None:
            remote_by_slot[event.slot_id or ""] = event
        else:
            result.delete.append(event.event_id)

    for slot, event in by_slot_desired.items():
        found = remote_by_slot.get(slot)
        if found is None and event.legacy_slot_id:
            # Events created before the rewrite carry the old identity. Match on
            # it so they are adopted rather than deleted and recreated, which
            # would empty and refill every existing user's calendar.
            found = remote_by_slot.pop(event.legacy_slot_id, None)
            if found is not None:
                # Re-stamp with the new id, and with it the new fingerprint.
                result.patch.append((found.event_id, event))
                continue
        if found is None:
            result.create.append(event)
            continue
        row = mirror_by_slot.get(slot)
        # Prefer Google's own stamp; fall back to what we recorded. Only when
        # neither is available do we assume a patch is needed.
        seen = found.fingerprint or (row.fingerprint if row else None)
        if seen == event.fingerprint:
            if row is None or row.event_id != found.event_id:
                result.adopt.append((found.event_id, event))
            else:
                result.unchanged.append(slot)
        else:
            result.patch.append((found.event_id, event))

    for slot, found in remote_by_slot.items():
        if slot not in by_slot_desired:
            result.delete.append(found.event_id)

    return result


def to_google_event(event: DesiredEvent, *, timezone: str) -> dict[str, Any]:
    """Render a desired event into Google's request body."""
    body: dict[str, Any] = {
        "summary": event.summary,
        "description": event.description,
        "extendedProperties": {
            "private": {
                SLOT_PROPERTY: event.slot_id,
                FINGERPRINT_PROPERTY: event.fingerprint,
            }
        },
    }
    if event.kind == "all_day":
        from datetime import date, timedelta

        day = date.fromisoformat(event.start_date)
        body["start"] = {"date": event.start_date}
        body["end"] = {"date": (day + timedelta(days=1)).isoformat()}
        body["transparency"] = "transparent"
    else:
        body["start"] = {
            "dateTime": f"{event.start_date}T{event.start_time}",
            "timeZone": timezone,
        }
        body["end"] = {
            "dateTime": f"{event.start_date}T{event.end_time}",
            "timeZone": timezone,
        }
    if event.recurrence:
        body["recurrence"] = list(event.recurrence)
    if event.color_id:
        body["colorId"] = event.color_id
    return body
