"""Per-user endpoints: identity touch + overrides + merged timetable.

Identity model (v1): client mints a UUID and sends it in `X-User-Id` on every
request. No real auth yet; the server just upserts a `UserDoc` row so we can
attach metadata (default batch, last seen) later.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import logging

from server import calendar_storage, storage
from server.auth import require_user_id
from server.db.models import (
    CalendarSyncJobDoc,
    ClassEntry,
    OverrideDoc,
    OverrideEntry,
    PersonalCustomizationDoc,
    PersonalOverrideOperation,
    UserDoc,
)

logger = logging.getLogger(__name__)
from server.personal_timetable import (
    apply_operations,
    merge_draft_operations,
    with_stable_class_ids,
)

router = APIRouter(prefix="/me", tags=["me"])

_SLOT_KEY_RE = re.compile(r"^[A-Za-z]+\|[\w:\.\- ]+$")


# ── Request / response bodies ────────────────────────────────────────────
class SetDefaultBatch(BaseModel):
    batch: str = Field(min_length=1, max_length=32)


class OverrideBody(BaseModel):
    kind: str
    entry: Optional[ClassEntry] = None


class PersonalOperationBody(BaseModel):
    kind: Literal["elective_pick", "edit", "delete", "add"]
    target_id: str = Field(min_length=3, max_length=128)
    entry: Optional[ClassEntry] = None
    base_fingerprint: Optional[str] = Field(default=None, max_length=128)


class ApplyPersonalChangesBody(BaseModel):
    expected_revision: int = Field(default=0, ge=0)
    operations: list[PersonalOperationBody] = Field(min_length=1, max_length=250)


# ── Helpers ──────────────────────────────────────────────────────────────
async def _touch_user(user_id: str) -> UserDoc:
    now = datetime.now(timezone.utc)
    doc = await UserDoc.find_one(UserDoc.user_id == user_id)
    if doc is None:
        doc = UserDoc(user_id=user_id, last_seen_at=now)
        await doc.insert()
    else:
        await doc.set({"last_seen_at": now})
    return doc


async def _load_overrides(user_id: str, batch: str) -> Optional[OverrideDoc]:
    return await OverrideDoc.find_one(
        OverrideDoc.user_id == user_id,
        OverrideDoc.batch == batch,
    )


async def _load_all_legacy_overrides(user_id: str, batch: str) -> list[OverrideDoc]:
    """Load every legacy doc; old concurrent writes created duplicates."""
    return await OverrideDoc.find(
        OverrideDoc.user_id == user_id,
        OverrideDoc.batch == batch,
    ).sort("updated_at").to_list()


def _merged_legacy_entries(docs: list[OverrideDoc]) -> dict[str, OverrideEntry]:
    merged: dict[str, OverrideEntry] = {}
    for doc in sorted(docs, key=lambda item: (item.updated_at, str(item.id))):
        merged.update(doc.entries or {})
    return merged


async def _load_personal_v2(user_id: str, batch: str) -> Optional[PersonalCustomizationDoc]:
    return await PersonalCustomizationDoc.find_one(
        PersonalCustomizationDoc.user_id == user_id,
        PersonalCustomizationDoc.batch == batch,
    )


async def _enqueue_calendar_sync(user_id: str) -> str:
    """Re-sync Google Calendar after a personal timetable change, if connected.

    Returns a short status string that callers surface in their JSON response so
    the browser can see whether a sync fired: "enqueued" (new job created),
    "already_pending" (a job was already waiting), "not_connected" (no enabled
    calendar), or "error". No-op-safe: never raises.
    """
    try:
        conn = await calendar_storage.get_connection(user_id)
        if conn is None or not conn.enabled:
            logger.info("calendar sync skipped for %s: not connected/enabled", user_id)
            return "not_connected"
        existing = await CalendarSyncJobDoc.find_one(
            CalendarSyncJobDoc.user_id == user_id,
            CalendarSyncJobDoc.trigger == "override_changed",
            CalendarSyncJobDoc.status == "pending",
        )
        if existing is not None:
            logger.info("calendar sync already pending for %s", user_id)
            return "already_pending"
        await calendar_storage.enqueue_job(user_id, "override_changed")
        logger.info("calendar sync enqueued for %s after personal edit", user_id)
        return "enqueued"
    except Exception:
        logger.exception("calendar sync enqueue failed for user %s", user_id)
        return "error"


def _normalize_batch(value: str) -> str:
    return "".join(ch for ch in value.strip().upper() if ch.isalnum())


async def _require_batch(user_id: str, batch: Optional[str]) -> str:
    """Resolve the batch to operate on: explicit arg wins, else user.default_batch."""
    user = await UserDoc.find_one(UserDoc.user_id == user_id)
    code = _normalize_batch(batch) if batch else ""
    if not code:
        code = (user.default_batch or "") if user else ""
    if not code:
        raise HTTPException(
            status_code=400,
            detail={"error": "no batch supplied and no default set", "code": "no_batch"},
        )
    if user is not None and not user.default_batch:
        await user.set({"default_batch": code, "last_seen_at": datetime.now(timezone.utc)})
    return code


def _slot_key(day: str, start_time: str) -> str:
    return f"{day}|{start_time}"


def _validate_slot(day: str, slot: str) -> tuple[str, str]:
    day = day.strip()
    slot = slot.strip()
    if not day or not slot:
        raise HTTPException(status_code=400, detail={"error": "day and slot required", "code": "bad_slot"})
    return day, slot


def _merge_legacy(canonical: dict[str, Any], entries: dict[str, OverrideEntry]) -> dict[str, Any]:
    if not entries:
        return canonical
    classes = list(canonical.get("classes", []))
    touched: set[str] = set()
    merged: list[dict[str, Any]] = []
    for klass in classes:
        key = _slot_key(klass.get("day", ""), klass.get("start_time", ""))
        ov = entries.get(key)
        if ov is None:
            merged.append(klass)
            continue
        touched.add(key)
        if ov.kind == "delete":
            continue
        if ov.entry is not None:
            # Keep the canonical id so a later V2 save can target this class.
            merged.append({
                **klass,
                **ov.entry.model_dump(exclude_none=False),
                "class_id": klass.get("class_id"),
            })
        else:
            merged.append(klass)
    # `add`/orphan overrides → append
    for key, ov in entries.items():
        if key in touched:
            continue
        if ov.kind == "delete" or ov.entry is None:
            continue
        orphan = ov.entry.model_dump(exclude_none=False)
        orphan.setdefault("class_id", f"legacy_{str(key).replace('|', '_')}")
        merged.append(orphan)
    return {**canonical, "classes": merged}


# ── Endpoints ────────────────────────────────────────────────────────────
@router.get("")
async def whoami(user_id: str = Depends(require_user_id)) -> dict[str, Any]:
    """Touch the user row and return its public profile."""
    user = await _touch_user(user_id)
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "default_batch": user.default_batch,
    }


@router.post("/batch")
async def set_default_batch(
    body: SetDefaultBatch,
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    code = "".join(ch for ch in body.batch.strip().upper() if ch.isalnum())
    if not code:
        raise HTTPException(status_code=400, detail={"error": "invalid batch", "code": "bad_batch"})
    await _touch_user(user_id)
    user = await UserDoc.find_one(UserDoc.user_id == user_id)
    assert user is not None
    previous_batch = user.default_batch
    await user.set({"default_batch": code, "last_seen_at": datetime.now(timezone.utc)})
    return {
        "user_id": user_id,
        "default_batch": code,
        "deleted_previous_batch_overrides": None,
    }


@router.get("/timetable")
async def get_my_timetable(
    batch: Optional[str] = Query(default=None),
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    user = await _touch_user(user_id)
    code = _normalize_batch(batch) if batch else (user.default_batch or "")
    if not code:
        raise HTTPException(
            status_code=400,
            detail={"error": "no batch supplied and no default set", "code": "no_batch"},
        )
    try:
        canonical = await storage.read_timetable(code)
    except storage.BatchNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": str(exc), "code": "batch_not_found", "batch": exc.batch},
        ) from exc
    canonical_classes = with_stable_class_ids(code, canonical.get("classes", []))
    canonical = {**canonical, "classes": canonical_classes}
    personal = await _load_personal_v2(user_id, code)
    stale: list[str] = []
    if personal is not None:
        personalized_classes, stale = apply_operations(canonical_classes, personal.operations)
        merged = {**canonical, "classes": personalized_classes}
        applied_count = len(personal.operations)
        revision = personal.revision
        source = "v2"
    else:
        legacy_entries = _merged_legacy_entries(await _load_all_legacy_overrides(user_id, code))
        merged = _merge_legacy(canonical, legacy_entries)
        applied_count = len(legacy_entries)
        revision = 0
        source = "legacy" if legacy_entries else "none"
    merged["canonical_classes"] = canonical_classes
    if not merged.get("teacher_codes_visible"):
        storage.redact_teacher_codes(merged)
        canonical_payload = {"classes": merged["canonical_classes"]}
        storage.redact_teacher_codes(canonical_payload)
        merged["canonical_classes"] = canonical_payload["classes"]
    merged["overrides_applied"] = applied_count
    merged["personal_revision"] = revision
    merged["customization_source"] = source
    merged["stale_override_ids"] = stale
    return merged


@router.put("/customizations/{batch}")
async def apply_personal_changes(
    batch: str,
    body: ApplyPersonalChangesBody,
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    """Atomically merge a complete frontend draft into V2 personal state."""
    await _touch_user(user_id)
    code = _normalize_batch(batch)
    if not code:
        raise HTTPException(status_code=400, detail={"error": "invalid batch", "code": "bad_batch"})
    try:
        canonical_payload = await storage.read_timetable(code)
    except storage.BatchNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": str(exc), "code": "batch_not_found"}) from exc
    canonical = with_stable_class_ids(code, canonical_payload.get("classes", []))
    current = await _load_personal_v2(user_id, code)
    if current is None and await OverrideDoc.find(
        OverrideDoc.user_id == user_id,
        OverrideDoc.batch == code,
    ).count() > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Legacy personal timetable must be migrated before it can be changed",
                "code": "migration_required",
            },
        )
    current_revision = current.revision if current else 0
    if body.expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Personal timetable changed on another device; reload before saving",
                "code": "revision_conflict",
                "current_revision": current_revision,
            },
        )
    try:
        merged = merge_draft_operations(
            canonical,
            current.operations if current else {},
            [operation.model_dump(exclude_none=False) for operation in body.operations],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc), "code": "invalid_operation"}) from exc

    now = datetime.now(timezone.utc)
    next_revision = current_revision + 1
    serialized = {
        key: PersonalOverrideOperation.model_validate(value).model_dump(exclude_none=False)
        for key, value in merged.items()
    }
    collection = PersonalCustomizationDoc.get_motor_collection()
    if current is None:
        try:
            created = PersonalCustomizationDoc(
                user_id=user_id,
                batch=code,
                revision=next_revision,
                operations={key: PersonalOverrideOperation.model_validate(value) for key, value in merged.items()},
                created_at=now,
                updated_at=now,
            )
            await created.insert()
        except DuplicateKeyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "Concurrent personal save; reload before retrying", "code": "revision_conflict"},
            ) from exc
    else:
        updated = await collection.find_one_and_update(
            {"_id": current.id, "revision": current_revision},
            {"$set": {"operations": serialized, "updated_at": now}, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise HTTPException(
                status_code=409,
                detail={"error": "Concurrent personal save; reload before retrying", "code": "revision_conflict"},
            )
    calendar_sync = await _enqueue_calendar_sync(user_id)
    return {
        "ok": True,
        "batch": code,
        "revision": next_revision,
        "saved_operations": len(merged),
        "calendar_sync": calendar_sync,
    }


@router.delete("/customizations/{batch}")
async def reset_personal_changes(
    batch: str,
    expected_revision: int = Query(default=0, ge=0),
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    """Atomically clear V2 personal state while preserving revision history."""
    code = _normalize_batch(batch)
    current = await _load_personal_v2(user_id, code)
    if current is None:
        # A V2 reset also suppresses legacy fallback without mutating legacy.
        if expected_revision != 0:
            raise HTTPException(status_code=409, detail={"error": "revision conflict", "code": "revision_conflict"})
        now = datetime.now(timezone.utc)
        try:
            await PersonalCustomizationDoc(
                user_id=user_id, batch=code, revision=1, operations={}, created_at=now, updated_at=now,
            ).insert()
        except DuplicateKeyError as exc:
            raise HTTPException(status_code=409, detail={"error": "revision conflict", "code": "revision_conflict"}) from exc
        calendar_sync = await _enqueue_calendar_sync(user_id)
        return {"ok": True, "batch": code, "revision": 1, "saved_operations": 0, "calendar_sync": calendar_sync}
    if current.revision != expected_revision:
        raise HTTPException(
            status_code=409,
            detail={"error": "Personal timetable changed on another device", "code": "revision_conflict", "current_revision": current.revision},
        )
    now = datetime.now(timezone.utc)
    collection = PersonalCustomizationDoc.get_motor_collection()
    updated = await collection.find_one_and_update(
        {"_id": current.id, "revision": current.revision},
        {"$set": {"operations": {}, "updated_at": now}, "$inc": {"revision": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail={"error": "revision conflict", "code": "revision_conflict"})
    calendar_sync = await _enqueue_calendar_sync(user_id)
    return {"ok": True, "batch": code, "revision": current.revision + 1, "saved_operations": 0, "calendar_sync": calendar_sync}


@router.get("/overrides")
async def list_overrides(
    batch: Optional[str] = Query(default=None),
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    code = await _require_batch(user_id, batch)
    doc = await _load_overrides(user_id, code)
    if doc is None:
        return {"batch": code, "entries": {}}
    return {
        "batch": doc.batch,
        "entries": {
            key: {"kind": ov.kind, "entry": ov.entry.model_dump(exclude_none=False) if ov.entry else None}
            for key, ov in doc.entries.items()
        },
    }


@router.put("/overrides/{day}/{slot}", status_code=status.HTTP_200_OK)
async def upsert_override(
    day: str,
    slot: str,
    body: OverrideBody,
    batch: Optional[str] = Query(default=None),
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    day, slot = _validate_slot(day, slot)
    if body.kind not in {"elective_pick", "edit", "delete", "add"}:
        raise HTTPException(status_code=400, detail={"error": "unknown kind", "code": "bad_kind"})
    if body.kind != "delete" and body.entry is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "entry is required for non-delete overrides", "code": "missing_entry"},
        )

    await _touch_user(user_id)
    code = await _require_batch(user_id, batch)

    key = _slot_key(day, slot)
    if not _SLOT_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail={"error": "invalid slot key", "code": "bad_slot"})

    entry = OverrideEntry(kind=body.kind, entry=body.entry)
    doc = await _load_overrides(user_id, code)
    now = datetime.now(timezone.utc)
    if doc is None:
        doc = OverrideDoc(
            user_id=user_id,
            batch=code,
            entries={key: entry},
        )
        await doc.insert()
    else:
        doc.entries[key] = entry
        doc.updated_at = now
        await doc.save()

    await _enqueue_calendar_sync(user_id)
    return {"key": key, "override": {"kind": entry.kind, "entry": entry.entry.model_dump(exclude_none=False) if entry.entry else None}}


@router.delete("/overrides/{day}/{slot}", status_code=status.HTTP_200_OK)
async def delete_override(
    day: str,
    slot: str,
    batch: Optional[str] = Query(default=None),
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    day, slot = _validate_slot(day, slot)
    code = await _require_batch(user_id, batch)
    key = _slot_key(day, slot)
    doc = await _load_overrides(user_id, code)
    if doc is None or key not in doc.entries:
        return {"deleted": False, "key": key}
    del doc.entries[key]
    doc.updated_at = datetime.now(timezone.utc)
    await doc.save()
    await _enqueue_calendar_sync(user_id)
    return {"deleted": True, "key": key}


@router.delete("/overrides", status_code=status.HTTP_200_OK)
async def delete_overrides(
    batch: Optional[str] = Query(default=None),
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    """Delete all personal overrides for one batch."""
    code = await _require_batch(user_id, batch)
    result = await OverrideDoc.find_one(
        OverrideDoc.user_id == user_id,
        OverrideDoc.batch == code,
    )
    if result is None:
        return {"deleted": False, "batch": code}
    await result.delete()
    await _enqueue_calendar_sync(user_id)
    return {"deleted": True, "batch": code}
