"""Calendar connection storage helpers.

Handles Fernet-encrypted OAuth token storage, CalendarConnectionDoc CRUD,
sync job management, and event map tracking.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from beanie.operators import In

from server.config import get_settings
from server.db.models import (
    CalendarConnectionDoc,
    CalendarEventMapDoc,
    CalendarSyncJobDoc,
)

logger = logging.getLogger(__name__)


# ── Fernet helpers ────────────────────────────────────────────────────

def _get_fernet():
    from cryptography.fernet import Fernet
    key = get_settings().calendar_token_key
    if not key:
        raise RuntimeError("CALENDAR_TOKEN_KEY env var is not set")
    k = key.encode() if isinstance(key, str) else key
    return Fernet(k)


def encrypt_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


# ── CalendarConnectionDoc CRUD ────────────────────────────────────────

def _conn_payload(doc: CalendarConnectionDoc) -> dict[str, Any]:
    return {
        "connected": True,
        "enabled": doc.enabled,
        "google_email": doc.google_email,
        "calendar_id": doc.calendar_id,
        "batch_code": doc.batch_code,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "last_synced_at": doc.last_synced_at.isoformat() if doc.last_synced_at else None,
        "last_error": doc.last_error,
    }


async def get_connection(user_id: str) -> CalendarConnectionDoc | None:
    return await CalendarConnectionDoc.find_one(CalendarConnectionDoc.user_id == user_id)


async def get_status(user_id: str) -> dict[str, Any]:
    conn = await get_connection(user_id)
    if conn is None:
        return {
            "connected": False,
            "enabled": False,
            "google_email": None,
            "calendar_id": None,
            "batch_code": None,
            "last_synced_at": None,
            "last_error": None,
        }
    payload = _conn_payload(conn)
    job = await CalendarSyncJobDoc.find_one(
        CalendarSyncJobDoc.user_id == user_id,
        In("status", ["pending", "running"]),
        sort=[("created_at", -1)],
    )
    payload["sync_state"] = "syncing" if job is not None else "idle"
    payload["sync_job_id"] = str(job.id) if job is not None and job.id else None
    return payload


async def create_or_replace_connection(
    user_id: str,
    *,
    refresh_token_plain: str,
    access_token_plain: str,
    access_expires_at: datetime,
    google_email: str,
) -> CalendarConnectionDoc:
    """Upsert connection.

    On re-connect (existing connection found), only the tokens and google_email
    are updated — ``calendar_id``, ``enabled``, and ``batch_code`` are preserved
    so a second OAuth flow doesn't orphan the existing calendar or reset sync.
    """
    existing = await get_connection(user_id)
    if existing is not None:
        # Preserve calendar state; only rotate tokens + email.
        await existing.set({
            "refresh_token": encrypt_token(refresh_token_plain),
            "access_token": encrypt_token(access_token_plain),
            "access_expires_at": access_expires_at,
            "google_email": google_email,
            "last_error": None,
        })
        return existing

    doc = CalendarConnectionDoc(
        user_id=user_id,
        refresh_token=encrypt_token(refresh_token_plain),
        access_token=encrypt_token(access_token_plain),
        access_expires_at=access_expires_at,
        google_email=google_email,
    )
    await doc.insert()
    return doc


async def set_enabled(user_id: str, *, enabled: bool, batch_code: str | None = None) -> bool:
    conn = await get_connection(user_id)
    if conn is None:
        return False
    updates: dict[str, Any] = {"enabled": enabled}
    if batch_code is not None:
        updates["batch_code"] = batch_code.upper()
    if enabled:
        updates["last_error"] = None
    await conn.set(updates)
    return True


async def update_token_cache(
    user_id: str,
    access_token_plain: str,
    access_expires_at: datetime,
) -> None:
    conn = await get_connection(user_id)
    if conn is None:
        return
    await conn.set({
        "access_token": encrypt_token(access_token_plain),
        "access_expires_at": access_expires_at,
    })


_UNSET = object()


async def update_after_sync(
    user_id: str,
    *,
    last_synced_at: datetime | None = None,
    last_error: Any = _UNSET,
    calendar_id: str | None = None,
) -> None:
    conn = await get_connection(user_id)
    if conn is None:
        return
    updates: dict[str, Any] = {}
    if last_synced_at is not None:
        updates["last_synced_at"] = last_synced_at
    if last_error is not _UNSET:
        updates["last_error"] = last_error
    if calendar_id is not None:
        updates["calendar_id"] = calendar_id
    if updates:
        await conn.set(updates)


async def mark_invalid_grant(user_id: str) -> None:
    conn = await get_connection(user_id)
    if conn is None:
        return
    await conn.set({"enabled": False, "last_error": "invalid_grant"})


async def wipe_calendar_id(user_id: str) -> None:
    """Null out calendar_id after a clear/reset."""
    conn = await get_connection(user_id)
    if conn is None:
        return
    await conn.set({
        "calendar_id": None,
        "last_synced_at": None,
        "last_error": None,
    })


async def delete_connection(user_id: str) -> None:
    conn = await get_connection(user_id)
    if conn is not None:
        await conn.delete()
    await CalendarEventMapDoc.find(CalendarEventMapDoc.user_id == user_id).delete()
    await CalendarSyncJobDoc.find(CalendarSyncJobDoc.user_id == user_id).delete()


# ── Sync jobs ─────────────────────────────────────────────────────────

async def enqueue_job(
    user_id: str,
    trigger: str,
    *,
    override_id: str | None = None,
) -> CalendarSyncJobDoc:
    doc = CalendarSyncJobDoc(
        user_id=user_id,
        trigger=trigger,  # type: ignore[arg-type]
        override_id=override_id,
    )
    await doc.insert()
    return doc


async def enqueue_jobs_for_override(override_doc: dict[str, Any]) -> int:
    """Enqueue sync jobs for all opted-in users affected by an override change.
    Called after admin creates/updates/deletes a CalendarOverrideDoc.
    """
    scope = override_doc.get("scope", "global")
    scope_values = [str(v).upper() for v in (override_doc.get("scope_values") or [])]
    override_id = str(override_doc.get("id") or "")

    count = 0
    async for conn in CalendarConnectionDoc.find(CalendarConnectionDoc.enabled == True):  # noqa: E712
        # Scope filter
        if scope != "global":
            batch = (conn.batch_code or "").upper()
            if not batch:
                continue
            year_str = batch[0] if batch else ""
            branch_str = f"{batch[0]}{batch[1]}" if len(batch) >= 2 else ""
            if scope == "year" and year_str not in scope_values:
                continue
            if scope == "branch" and branch_str not in scope_values:
                continue

        # Dedup: skip if a pending job for this (user, override) already exists
        existing = await CalendarSyncJobDoc.find_one(
            CalendarSyncJobDoc.user_id == conn.user_id,
            CalendarSyncJobDoc.override_id == override_id,
            CalendarSyncJobDoc.status == "pending",
        )
        if existing is not None:
            continue

        await enqueue_job(conn.user_id, "override_changed", override_id=override_id)
        count += 1

    return count


# ── Event map ─────────────────────────────────────────────────────────

async def clear_event_maps(user_id: str) -> int:
    result = await CalendarEventMapDoc.find(CalendarEventMapDoc.user_id == user_id).delete()
    return getattr(result, "deleted_count", 0) or 0
