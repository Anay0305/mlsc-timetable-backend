"""Google Calendar OAuth + sync endpoints.

Routes:
  GET  /api/calendar/status          → connection status
  GET  /api/calendar/oauth/start     → { redirect_url }
  GET  /api/calendar/oauth/callback  → HTML popup close
  POST /api/calendar/enable          → flip enabled=true, enqueue initial sync
  POST /api/calendar/disable         → flip enabled=false
  POST /api/calendar/resync          → enqueue manual full re-sync
  DELETE /api/calendar/disconnect    → revoke token, delete calendar, wipe DB
  DELETE /api/calendar/clear         → delete calendar + event maps, keep connection
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from server import calendar_storage, calendar_sync
from server.auth import require_clerk_user
from server.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_SCOPES = "https://www.googleapis.com/auth/calendar email openid"


# ── Helpers ───────────────────────────────────────────────────────────

def _is_configured() -> bool:
    s = get_settings()
    return bool(s.google_oauth_client_id and s.google_oauth_client_secret and s.calendar_token_key)


def _require_configured() -> None:
    if not _is_configured():
        raise HTTPException(
            status_code=503,
            detail={"error": "Google Calendar integration not configured", "code": "not_configured"},
        )


def _make_state(user_id: str) -> str:
    """Encode user_id + timestamp into a Fernet-encrypted state token."""
    fernet = calendar_storage._get_fernet()
    payload = json.dumps({"user_id": user_id, "ts": time.time()})
    return fernet.encrypt(payload.encode()).decode()


def _decode_state(state: str) -> str:
    """Decode state token → user_id. Raises HTTPException on invalid/expired."""
    try:
        fernet = calendar_storage._get_fernet()
        payload = json.loads(fernet.decrypt(state.encode()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid state", "code": "invalid_state"})
    age = time.time() - float(payload.get("ts", 0))
    if age > 600:  # 10-minute window
        raise HTTPException(status_code=400, detail={"error": "State token expired", "code": "state_expired"})
    return payload["user_id"]


def _popup_html(*, success: bool, message: str = "") -> HTMLResponse:
    """Return HTML that posts a postMessage to the opener and closes the popup."""
    if success:
        script = """
window.opener && window.opener.postMessage({type:'mlsc_calendar_connected'}, '*');
window.close();
"""
        body = "<p>Connected! You can close this window.</p>"
    else:
        payload = json.dumps({"type": "mlsc_calendar_error", "error": message})
        script = f"window.opener && window.opener.postMessage({payload}, '*'); window.close();"
        body = f"<p>Error: {message}. You can close this window.</p>"

    html = f"""<!DOCTYPE html>
<html><head><title>Google Calendar</title></head>
<body>
<script>{script}</script>
{body}
</body></html>"""
    return HTMLResponse(html)


# ── Routes ────────────────────────────────────────────────────────────

@router.get("/configured")
async def get_configured() -> dict:
    """Public endpoint — returns whether Google Calendar integration is configured.
    No auth required so the frontend can decide whether to show the button
    before the user is signed in.
    """
    return {"configured": _is_configured()}


@router.get("/status")
async def get_status(user_id: str = Depends(require_clerk_user)) -> dict:
    """Return connection + sync status for the authenticated user."""
    status = await calendar_storage.get_status(user_id)
    status["configured"] = _is_configured()
    return status


@router.get("/oauth/start")
async def oauth_start(user_id: str = Depends(require_clerk_user)) -> dict:
    """Return the Google OAuth URL for the frontend to open in a popup."""
    _require_configured()
    settings = get_settings()
    state = _make_state(user_id)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return {"redirect_url": f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
) -> HTMLResponse:
    """Google redirects here after user consents. Exchanges code, stores tokens, closes popup."""
    if error:
        return _popup_html(success=False, message=error)

    if not code or not state:
        return _popup_html(success=False, message="Missing code or state")

    try:
        user_id = _decode_state(state)
    except HTTPException as exc:
        return _popup_html(success=False, message=str(exc.detail))

    try:
        token_data = await calendar_sync.exchange_code(code)
    except Exception as exc:
        logger.exception("calendar oauth_callback: token exchange failed")
        return _popup_html(success=False, message="Token exchange failed")

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return _popup_html(
            success=False,
            message="No refresh token returned — please revoke app access in Google and try again",
        )

    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Extract email: try id_token JWT payload first (no extra API call),
    # fall back to userinfo endpoint, then "unknown".
    google_email = ""
    id_token = token_data.get("id_token", "")
    if id_token:
        try:
            import base64, json as _json
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
            google_email = claims.get("email", "")
        except Exception:
            pass
    if not google_email:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                info = await client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10,
                )
                if info.status_code == 200:
                    google_email = info.json().get("email", "")
        except Exception:
            pass
    if not google_email:
        google_email = "unknown"

    await calendar_storage.create_or_replace_connection(
        user_id,
        refresh_token_plain=refresh_token,
        access_token_plain=access_token,
        access_expires_at=expires_at,
        google_email=google_email,
    )

    return _popup_html(success=True)


class EnableBody(BaseModel):
    batch: str


@router.post("/enable")
async def enable_sync(
    body: EnableBody,
    user_id: str = Depends(require_clerk_user),
) -> dict:
    """Enable calendar sync for the user. Enqueues initial sync."""
    _require_configured()
    batch = body.batch.strip().upper()
    if not batch:
        raise HTTPException(status_code=400, detail={"error": "batch is required", "code": "missing_batch"})

    ok = await calendar_storage.set_enabled(user_id, enabled=True, batch_code=batch)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"error": "Google Calendar not connected. Connect first.", "code": "not_connected"},
        )

    # Dedup: skip if a pending/running sync job already exists for this user.
    from server.db.models import CalendarSyncJobDoc
    existing_job = await CalendarSyncJobDoc.find_one(
        CalendarSyncJobDoc.user_id == user_id,
        CalendarSyncJobDoc.status.in_(["pending", "running"]),
    )
    if existing_job is None:
        await calendar_storage.enqueue_job(user_id, "initial")
    return {"ok": True}


@router.post("/disable")
async def disable_sync(user_id: str = Depends(require_clerk_user)) -> dict:
    """Disable sync. Keeps calendar + events as-is; just stops future updates."""
    ok = await calendar_storage.set_enabled(user_id, enabled=False)
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "Not connected", "code": "not_connected"})
    return {"ok": True}


@router.post("/resync")
async def resync(
    batch: Optional[str] = None,
    user_id: str = Depends(require_clerk_user),
) -> dict:
    """Enqueue a manual full re-sync. Optionally update the batch first."""
    _require_configured()
    conn = await calendar_storage.get_connection(user_id)
    if conn is None:
        raise HTTPException(status_code=404, detail={"error": "Not connected", "code": "not_connected"})

    if batch:
        await calendar_storage.set_enabled(user_id, enabled=conn.enabled, batch_code=batch.upper())

    await calendar_storage.enqueue_job(user_id, "manual")
    return {"ok": True}


@router.delete("/disconnect")
async def disconnect(
    clear: bool = Query(default=True),
    user_id: str = Depends(require_clerk_user),
) -> dict:
    """Revoke Google token and wipe all DB rows.

    If ``clear=true`` (default), also deletes the dedicated MLSC calendar and
    all its events from Google. Pass ``clear=false`` to only revoke the token
    and remove the connection, leaving the calendar intact.
    """
    conn = await calendar_storage.get_connection(user_id)
    if conn is None:
        raise HTTPException(status_code=404, detail={"error": "Not connected", "code": "not_connected"})

    # Try to revoke the refresh token
    try:
        refresh_plain = calendar_storage.decrypt_token(conn.refresh_token)
        await calendar_sync.revoke_token(refresh_plain)
    except Exception:
        pass  # Best-effort

    # Optionally delete the Google calendar
    if clear and conn.calendar_id:
        try:
            access_token = await calendar_sync.get_valid_access_token(conn)
            await calendar_sync.delete_calendar(conn.calendar_id, access_token)
        except Exception:
            pass  # Best-effort: calendar may already be gone

    await calendar_storage.delete_connection(user_id)
    return {"ok": True}


@router.delete("/clear")
async def clear_events(user_id: str = Depends(require_clerk_user)) -> dict:
    """Delete all events we created in the user's MLSC calendar and reset sync state.

    The Google connection is kept — calling enable/resync will recreate
    a fresh calendar and repopulate it from the current timetable.
    """
    conn = await calendar_storage.get_connection(user_id)
    if conn is None:
        raise HTTPException(status_code=404, detail={"error": "Not connected", "code": "not_connected"})

    cleared_calendar = False
    if conn.calendar_id:
        try:
            access_token = await calendar_sync.get_valid_access_token(conn)
            await calendar_sync.delete_calendar(conn.calendar_id, access_token)
            cleared_calendar = True
        except Exception:
            pass  # Calendar may have already been deleted by the user

    event_count = await calendar_storage.clear_event_maps(user_id)
    await calendar_storage.wipe_calendar_id(user_id)

    return {"ok": True, "calendar_deleted": cleared_calendar, "event_maps_cleared": event_count}
