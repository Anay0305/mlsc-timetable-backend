"""Pure helpers for stable, operation-based personal timetable state.

V2 customizations target a deterministic ``class_id`` instead of a day/time
slot. This allows multiple classes in one slot and makes moves and deletes
unambiguous. The functions here are deliberately database-free so API and
migration tests exercise the exact same behavior.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


PERSONAL_ID_PREFIX = "p_"
CANONICAL_ID_PREFIX = "c_"


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=False)
    return deepcopy(dict(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def class_fingerprint(entry: Any) -> str:
    """Fingerprint conflict-relevant official fields.

    Teacher values and catalog display names are intentionally excluded: they
    may be redacted or resolved differently per response and must never change
    a class identity or make every saved operation stale.
    """
    raw = _as_dict(entry)
    options = [
        {
            "subject_code": option.get("subject_code") or "",
            "type": option.get("type") or "",
            "place": option.get("place") or "",
        }
        for option in raw.get("options") or []
        if isinstance(option, dict)
    ]
    payload = {
        "day": raw.get("day") or "",
        "start_time": raw.get("start_time") or "",
        "end_time": raw.get("end_time") or "",
        "code": raw.get("code") or "",
        "type": raw.get("type") or "",
        "room": raw.get("room") or "",
        "options": options,
        "alternate_week_start": raw.get("alternate_week_start"),
    }
    encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def class_identity_fingerprint(entry: Any) -> str:
    """Fingerprint only stable coordinates used to mint ``class_id``."""
    raw = _as_dict(entry)
    option_codes = sorted(
        str(option.get("subject_code") or "")
        for option in raw.get("options") or []
        if isinstance(option, dict)
    )
    payload = {
        "day": raw.get("day") or "",
        "start_time": raw.get("start_time") or "",
        "code": raw.get("code") or "",
        "type": raw.get("type") or "",
        "option_codes": option_codes,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def with_stable_class_ids(batch: str, classes: Iterable[Any]) -> list[dict[str, Any]]:
    """Return class dictionaries with deterministic, collision-safe ids."""
    code = "".join(ch for ch in str(batch).strip().upper() if ch.isalnum())
    occurrences: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for value in classes:
        entry = _as_dict(value)
        existing = str(entry.get("class_id") or "").strip()
        fingerprint = class_identity_fingerprint(entry)
        occurrence = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        if existing and existing not in used:
            class_id = existing
        else:
            seed = f"{code}|{fingerprint}|{occurrence}"
            class_id = f"{CANONICAL_ID_PREFIX}{hashlib.sha256(seed.encode()).hexdigest()[:24]}"
        entry["class_id"] = class_id
        used.add(class_id)
        result.append(entry)
    return result


def _operation_dict(value: Any) -> dict[str, Any]:
    raw = _as_dict(value)
    if raw.get("entry") is not None:
        raw["entry"] = _as_dict(raw["entry"])
    return raw


def apply_operations(
    canonical_classes: Iterable[Any],
    operations: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply V2 operations and return ``(classes, stale_target_ids)``."""
    canonical = [_as_dict(entry) for entry in canonical_classes]
    pending = {str(key): _operation_dict(value) for key, value in (operations or {}).items()}
    result: list[dict[str, Any]] = []
    stale: list[str] = []

    for official in canonical:
        target_id = str(official.get("class_id") or "")
        operation = pending.pop(target_id, None)
        if operation is None:
            result.append(official)
            continue
        expected = operation.get("base_fingerprint")
        if expected and expected != class_fingerprint(official):
            stale.append(target_id)
            result.append(official)
            continue
        kind = operation.get("kind")
        if kind == "delete":
            continue
        entry = operation.get("entry")
        if kind in {"edit", "elective_pick"} and isinstance(entry, dict):
            personalized = {**official, **entry, "class_id": target_id}
            result.append(personalized)
            continue
        result.append(official)

    for target_id, operation in pending.items():
        if operation.get("kind") != "add" or not isinstance(operation.get("entry"), dict):
            stale.append(target_id)
            continue
        entry = {**operation["entry"], "class_id": target_id}
        result.append(entry)

    return result, stale


def _same_visible_entry(left: Any, right: Any) -> bool:
    a = _as_dict(left)
    b = _as_dict(right)
    ignored = {"class_id", "id", "pairId", "pair_id"}
    return _jsonable({k: v for k, v in a.items() if k not in ignored}) == _jsonable(
        {k: v for k, v in b.items() if k not in ignored}
    )


def merge_draft_operations(
    canonical_classes: Iterable[Any],
    current_operations: Mapping[str, Any] | None,
    incoming_operations: Iterable[Any],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Fold one atomic frontend draft into the saved V2 operation set."""
    canonical = {_as_dict(entry).get("class_id"): _as_dict(entry) for entry in canonical_classes}
    merged = {str(key): _operation_dict(value) for key, value in (current_operations or {}).items()}
    timestamp = now or datetime.now(timezone.utc)

    for value in incoming_operations:
        incoming = _operation_dict(value)
        target_id = str(incoming.get("target_id") or "").strip()
        kind = incoming.get("kind")
        if not target_id:
            raise ValueError("target_id is required")
        if kind not in {"add", "edit", "delete", "elective_pick"}:
            raise ValueError(f"unsupported operation kind: {kind}")
        entry = incoming.get("entry")
        current = merged.get(target_id)

        # Editing or deleting a personally-added class updates/removes its add
        # operation instead of creating a second operation against it.
        if current and current.get("kind") == "add":
            if kind == "delete":
                merged.pop(target_id, None)
                continue
            if kind in {"edit", "elective_pick"} and isinstance(entry, dict):
                merged[target_id] = {
                    **current,
                    "entry": {**(current.get("entry") or {}), **entry, "class_id": target_id},
                    "updated_at": timestamp,
                }
                continue

        if kind == "add":
            if target_id in canonical:
                raise ValueError("add target_id collides with a canonical class")
            if not isinstance(entry, dict):
                raise ValueError("add requires an entry")
            merged[target_id] = {
                "kind": "add",
                "target_id": target_id,
                "entry": {**entry, "class_id": target_id},
                "base_fingerprint": None,
                "updated_at": timestamp,
            }
            continue

        official = canonical.get(target_id)
        if official is None:
            raise ValueError(f"unknown target_id: {target_id}")
        base_fingerprint = (current or {}).get("base_fingerprint") or class_fingerprint(official)

        if kind in {"edit", "elective_pick"}:
            if not isinstance(entry, dict):
                raise ValueError(f"{kind} requires an entry")
            normalized_entry = {**entry, "class_id": target_id}
            if _same_visible_entry(official, normalized_entry):
                merged.pop(target_id, None)
                continue
            merged[target_id] = {
                "kind": kind,
                "target_id": target_id,
                "entry": normalized_entry,
                "base_fingerprint": base_fingerprint,
                "updated_at": timestamp,
            }
            continue

        merged[target_id] = {
            "kind": "delete",
            "target_id": target_id,
            "entry": None,
            "base_fingerprint": base_fingerprint,
            "updated_at": timestamp,
        }

    return merged


def _personal_legacy_id(user_id: str, batch: str, slot: str, ordinal: int = 0) -> str:
    seed = f"{user_id}|{batch}|{slot}|{ordinal}"
    return f"{PERSONAL_ID_PREFIX}{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


def convert_legacy_entries(
    *,
    user_id: str,
    batch: str,
    canonical_classes: Iterable[Any],
    entries: Mapping[str, Any],
    migrated_at: datetime | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Convert a merged legacy slot map into V2 target-id operations.

    Ambiguous mappings are retained and reported. Orphan legacy edits become
    personal adds so the old rendered class is not silently discarded.
    """
    canonical = with_stable_class_ids(batch, canonical_classes)
    by_slot: dict[str, list[dict[str, Any]]] = {}
    for item in canonical:
        slot = f"{item.get('day', '')}|{item.get('start_time', '')}"
        by_slot.setdefault(slot, []).append(item)

    now = migrated_at or datetime.now(timezone.utc)
    operations: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []

    for ordinal, (slot, raw_value) in enumerate(sorted(entries.items())):
        value = _operation_dict(raw_value)
        kind = value.get("kind")
        entry = value.get("entry") if isinstance(value.get("entry"), dict) else None
        migration = deepcopy(value.get("migration") or {})
        migration.update({"legacy_slot": slot, "legacy_kind": kind})

        if kind == "add":
            target_id = _personal_legacy_id(user_id, batch, slot, ordinal)
            operations[target_id] = {
                "kind": "add",
                "target_id": target_id,
                "entry": {**(entry or {}), "class_id": target_id},
                "base_fingerprint": None,
                "updated_at": now,
                "migration": migration,
            }
            continue

        candidates = list(by_slot.get(slot, []))
        selected: dict[str, Any] | None = None
        if len(candidates) == 1:
            selected = candidates[0]
        elif len(candidates) > 1 and entry:
            entry_option_codes = sorted(
                str(option.get("subject_code") or "")
                for option in entry.get("options") or []
                if isinstance(option, dict)
            )
            if entry_option_codes:
                option_matches = [candidate for candidate in candidates if sorted(
                    str(option.get("subject_code") or "")
                    for option in candidate.get("options") or []
                    if isinstance(option, dict)
                ) == entry_option_codes]
                if len(option_matches) == 1:
                    selected = option_matches[0]
            if selected is None:
                field_matches = [candidate for candidate in candidates if all(
                    not entry.get(field) or candidate.get(field) == entry.get(field)
                    for field in ("code", "type")
                )]
                if len(field_matches) == 1:
                    selected = field_matches[0]

        if selected is None and candidates:
            selected = candidates[0]
            conflicts.append({
                "type": "ambiguous_target",
                "slot": slot,
                "candidate_ids": [candidate.get("class_id") for candidate in candidates],
                "selected_id": selected.get("class_id"),
            })

        if selected is None:
            if kind == "delete":
                conflicts.append({"type": "orphan_delete", "slot": slot})
                continue
            target_id = _personal_legacy_id(user_id, batch, slot, ordinal)
            operations[target_id] = {
                "kind": "add",
                "target_id": target_id,
                "entry": {**(entry or {}), "class_id": target_id},
                "base_fingerprint": None,
                "updated_at": now,
                "migration": {**migration, "converted_from_orphan": True},
            }
            conflicts.append({"type": "orphan_converted_to_add", "slot": slot, "target_id": target_id})
            continue

        target_id = str(selected["class_id"])
        operations[target_id] = {
            "kind": kind,
            "target_id": target_id,
            "entry": None if kind == "delete" else {**(entry or selected), "class_id": target_id},
            "base_fingerprint": class_fingerprint(selected),
            "updated_at": now,
            "migration": migration,
        }

    return operations, conflicts
