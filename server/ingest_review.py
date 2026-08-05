"""Pure helpers for staged spreadsheet-ingest reviews.

The parser produces a candidate timetable. This module compares it with the
live canonical timetable without touching MongoDB, then resolves the final
class list from explicit admin decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Iterable, Mapping

from server.personal_timetable import with_stable_class_ids


CHANGE_FIELDS = (
    "day",
    "start_time",
    "end_time",
    "subject",
    "code",
    "teacher",
    "type",
    "room",
    "options",
    "alternate_week_start",
)


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=False)
    return deepcopy(dict(value))


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalized_options(entry: Mapping[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for option in entry.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        options.append({
            "subject_code": _text(option.get("subject_code")).upper(),
            # Names backed by Subject Catalog are presentation data and must
            # not make every timetable look changed after a catalog rename.
            "subject_name": _text(option.get("subject_name")) if not option.get("subject_code") else "",
            "type": _text(option.get("type")),
            "place": _text(option.get("place")),
            "teacher": _text(option.get("teacher")).upper(),
        })
    return options


def comparable_entry(value: Any) -> dict[str, Any]:
    raw = _as_dict(value)
    code = _text(raw.get("code")).upper()
    return {
        "day": _text(raw.get("day")).title(),
        "start_time": _text(raw.get("start_time")),
        "end_time": _text(raw.get("end_time")),
        # Catalog-resolved subject names are intentionally ignored when a
        # code exists. Free-text/no-code classes still need subject comparison.
        "subject": "" if code else _text(raw.get("subject")),
        "code": code,
        "teacher": _text(raw.get("teacher")).upper(),
        "type": _text(raw.get("type")).title(),
        "room": _text(raw.get("room")).upper(),
        "options": _normalized_options(raw),
        "alternate_week_start": raw.get("alternate_week_start"),
    }


def payload_hash(classes: Iterable[Any]) -> str:
    payload = [comparable_entry(item) for item in classes]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _move_signature(value: Any) -> tuple[Any, ...]:
    item = comparable_entry(value)
    option_codes = tuple(sorted(option["subject_code"] for option in item["options"]))
    return (item["code"], item["type"], option_codes)


def _slot_signature(value: Any) -> tuple[str, str]:
    item = comparable_entry(value)
    return item["day"], item["start_time"]


def _changed_fields(before: Any, after: Any) -> list[str]:
    left = comparable_entry(before)
    right = comparable_entry(after)
    return [field for field in CHANGE_FIELDS if left.get(field) != right.get(field)]


def _change_id(
    batch: str,
    kind: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    ordinal: int,
) -> str:
    seed = "|".join((
        batch,
        kind,
        str((before or {}).get("class_id") or ""),
        str((after or {}).get("class_id") or ""),
        str(ordinal),
    ))
    return f"chg_{hashlib.sha256(seed.encode()).hexdigest()[:20]}"


def diff_batch(
    batch: str,
    before_classes: Iterable[Any],
    after_classes: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(before_with_ids, after_with_ids, changes)` for one batch."""
    before = with_stable_class_ids(batch, before_classes)
    after = with_stable_class_ids(batch, after_classes)
    before_by_id = {str(item["class_id"]): item for item in before}
    after_by_id = {str(item["class_id"]): item for item in after}

    pairs: list[tuple[str, int, int]] = []
    used_before: set[int] = set()
    used_after: set[int] = set()

    # Stable-coordinate matches cover normal field edits such as room,
    # teacher, duration and alternate-week markers.
    before_idx_by_id = {str(item["class_id"]): idx for idx, item in enumerate(before)}
    for after_idx, item in enumerate(after):
        before_idx = before_idx_by_id.get(str(item["class_id"]))
        if before_idx is None:
            continue
        pairs.append(("modified", before_idx, after_idx))
        used_before.add(before_idx)
        used_after.add(after_idx)

    # Detect only unambiguous moves. Repeated weekly lectures intentionally
    # remain add/remove rows rather than being paired by a risky guess.
    before_moves: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    after_moves: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for idx, item in enumerate(before):
        if idx not in used_before:
            before_moves[_move_signature(item)].append(idx)
    for idx, item in enumerate(after):
        if idx not in used_after:
            after_moves[_move_signature(item)].append(idx)
    for signature, left in before_moves.items():
        right = after_moves.get(signature) or []
        if len(left) != 1 or len(right) != 1 or not signature[0]:
            continue
        before_idx, after_idx = left[0], right[0]
        after[after_idx]["class_id"] = before[before_idx]["class_id"]
        pairs.append(("moved", before_idx, after_idx))
        used_before.add(before_idx)
        used_after.add(after_idx)

    # A single old and new class occupying one slot is a clear replacement.
    # It receives a new identity because the course itself changed.
    before_slots: dict[tuple[str, str], list[int]] = defaultdict(list)
    after_slots: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, item in enumerate(before):
        if idx not in used_before:
            before_slots[_slot_signature(item)].append(idx)
    for idx, item in enumerate(after):
        if idx not in used_after:
            after_slots[_slot_signature(item)].append(idx)
    for slot, left in before_slots.items():
        right = after_slots.get(slot) or []
        if len(left) != 1 or len(right) != 1:
            continue
        before_idx, after_idx = left[0], right[0]
        pairs.append(("replaced", before_idx, after_idx))
        used_before.add(before_idx)
        used_after.add(after_idx)

    changes: list[dict[str, Any]] = []
    ordinals: Counter[str] = Counter()

    def append_change(
        kind: str,
        old: dict[str, Any] | None,
        new: dict[str, Any] | None,
        fields: list[str],
    ) -> None:
        seed = f"{kind}|{(old or {}).get('class_id')}|{(new or {}).get('class_id')}"
        ordinal = ordinals[seed]
        ordinals[seed] += 1
        changes.append({
            "change_id": _change_id(batch, kind, old, new, ordinal),
            "kind": kind,
            "changed_fields": fields,
            "before": deepcopy(old) if old is not None else None,
            "after": deepcopy(new) if new is not None else None,
        })

    for kind, before_idx, after_idx in pairs:
        old, new = before[before_idx], after[after_idx]
        fields = _changed_fields(old, new)
        if fields:
            append_change(kind, old, new, fields)

    for idx, item in enumerate(before):
        if idx not in used_before:
            append_change("removed", item, None, list(CHANGE_FIELDS))
    for idx, item in enumerate(after):
        if idx not in used_after:
            append_change("added", None, item, list(CHANGE_FIELDS))

    day_order = {day: idx for idx, day in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}
    changes.sort(key=lambda row: (
        day_order.get(str((row.get("after") or row.get("before") or {}).get("day")), 99),
        str((row.get("after") or row.get("before") or {}).get("start_time") or ""),
        str((row.get("after") or row.get("before") or {}).get("code") or ""),
        row["kind"],
    ))
    return before, after, changes


def resolve_reviewed_classes(
    before_classes: Iterable[Any],
    changes: Iterable[Mapping[str, Any]],
    decisions: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Apply explicit decisions to the base class list."""
    result = [_as_dict(item) for item in before_classes]

    def remove_class(class_id: str | None) -> None:
        if not class_id:
            return
        result[:] = [item for item in result if str(item.get("class_id")) != class_id]

    def replace_class(class_id: str | None, replacement: Mapping[str, Any]) -> None:
        if not class_id:
            result.append(deepcopy(dict(replacement)))
            return
        for idx, item in enumerate(result):
            if str(item.get("class_id")) == class_id:
                result[idx] = deepcopy(dict(replacement))
                return
        result.append(deepcopy(dict(replacement)))

    for raw_change in changes:
        change = dict(raw_change)
        change_id = str(change.get("change_id") or "")
        if decisions.get(change_id) != "use_uploaded":
            continue
        kind = change.get("kind")
        old = change.get("before") if isinstance(change.get("before"), Mapping) else None
        new = change.get("after") if isinstance(change.get("after"), Mapping) else None
        old_id = str((old or {}).get("class_id") or "") or None
        if kind in {"modified", "moved"} and new is not None:
            replace_class(old_id, new)
        elif kind == "replaced":
            remove_class(old_id)
            if new is not None:
                result.append(deepcopy(dict(new)))
        elif kind == "removed":
            remove_class(old_id)
        elif kind == "added" and new is not None:
            result.append(deepcopy(dict(new)))

    day_order = {day: idx for idx, day in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}
    result.sort(key=lambda item: (
        day_order.get(str(item.get("day")), 99),
        str(item.get("start_time") or ""),
        str(item.get("code") or ""),
        str(item.get("type") or ""),
        str(item.get("class_id") or ""),
    ))
    return result

