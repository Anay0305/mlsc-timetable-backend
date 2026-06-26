"""Per-user endpoints: identity touch + overrides + merged timetable.

Identity model (v1): client mints a UUID and sends it in `X-User-Id` on every
request. No real auth yet; the server just upserts a `UserDoc` row so we can
attach metadata (default batch, last seen) later.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from server import storage
from server.auth import require_user_id
from server.db.models import (
    ClassEntry,
    OverrideDoc,
    OverrideEntry,
    UserDoc,
)

router = APIRouter(prefix="/me", tags=["me"])

_SLOT_KEY_RE = re.compile(r"^[A-Za-z]+\|[\w:\.\- ]+$")


# ── Request / response bodies ────────────────────────────────────────────
class SetDefaultBatch(BaseModel):
    batch: str = Field(min_length=1, max_length=32)


class OverrideBody(BaseModel):
    kind: str
    entry: Optional[ClassEntry] = None


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


def _normalize_batch(value: str) -> str:
    return "".join(ch for ch in value.strip().upper() if ch.isalnum())


async def _require_batch(user_id: str, batch: Optional[str]) -> str:
    """Resolve the batch to operate on: explicit arg wins, else user.default_batch."""
    code = _normalize_batch(batch) if batch else ""
    if not code:
        user = await UserDoc.find_one(UserDoc.user_id == user_id)
        code = (user.default_batch or "") if user else ""
    if not code:
        raise HTTPException(
            status_code=400,
            detail={"error": "no batch supplied and no default set", "code": "no_batch"},
        )
    return code


def _slot_key(day: str, start_time: str) -> str:
    return f"{day}|{start_time}"


def _validate_slot(day: str, slot: str) -> tuple[str, str]:
    day = day.strip()
    slot = slot.strip()
    if not day or not slot:
        raise HTTPException(status_code=400, detail={"error": "day and slot required", "code": "bad_slot"})
    return day, slot


def _merge(canonical: dict[str, Any], overrides: Optional[OverrideDoc]) -> dict[str, Any]:
    if overrides is None or not overrides.entries:
        return canonical
    classes = list(canonical.get("classes", []))
    touched: set[str] = set()
    merged: list[dict[str, Any]] = []
    for klass in classes:
        key = _slot_key(klass.get("day", ""), klass.get("start_time", ""))
        ov = overrides.entries.get(key)
        if ov is None:
            merged.append(klass)
            continue
        touched.add(key)
        if ov.kind == "delete":
            continue
        if ov.entry is not None:
            merged.append(ov.entry.model_dump(exclude_none=False))
        else:
            merged.append(klass)
    # `add`/orphan overrides → append
    for key, ov in overrides.entries.items():
        if key in touched:
            continue
        if ov.kind == "delete" or ov.entry is None:
            continue
        merged.append(ov.entry.model_dump(exclude_none=False))
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
    await user.set({"default_batch": code, "last_seen_at": datetime.now(timezone.utc)})
    return {"user_id": user_id, "default_batch": code}


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
    overrides = await _load_overrides(user_id, code)
    merged = _merge(canonical, overrides)
    merged["overrides_applied"] = 0 if overrides is None else len(overrides.entries)
    return merged


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
    return {"deleted": True, "key": key}
