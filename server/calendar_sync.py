"""Google Calendar sync engine.

Handles token refresh, Google API calls, the full sync algorithm,
the background worker, and override fan-out enqueueing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from server import calendar_storage
from server.config import get_settings
from server.db.models import CalendarConnectionDoc, CalendarSyncJobDoc

logger = logging.getLogger(__name__)

GCAL_BASE = "https://www.googleapis.com/calendar/v3"

# Global reference to the background worker task so lifespan can cancel it.
_worker_task: asyncio.Task | None = None


# ── Exceptions ────────────────────────────────────────────────────────

class InvalidGrantError(Exception):
    """Refresh token has been revoked or has expired."""


class CalendarNotConfiguredError(Exception):
    """Google OAuth credentials are not set in the environment."""


# ── Token management ──────────────────────────────────────────────────

async def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an OAuth authorization code for tokens. Returns the full token dict."""
    settings = get_settings()
    if not settings.google_oauth_client_id:
        raise CalendarNotConfiguredError("GOOGLE_OAUTH_CLIENT_ID not set")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
    resp.raise_for_status()
    return resp.json()


async def _refresh_access_token(refresh_token_plain: str) -> tuple[str, datetime]:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "refresh_token": refresh_token_plain,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
    if resp.status_code in (400, 401):
        try:
            body = resp.json()
        except Exception:
            body = {}
        error = body.get("error") or "oauth_token_refresh_failed"
        description = body.get("error_description") or "Google rejected the stored refresh token"
        raise InvalidGrantError(f"{error}: {description}")
    resp.raise_for_status()
    data = resp.json()
    expires_in = int(data.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return data["access_token"], expires_at


async def get_valid_access_token(conn: CalendarConnectionDoc) -> str:
    """Return a valid access token, refreshing via httpx if expired."""
    access_token = calendar_storage.decrypt_token(conn.access_token)
    expires_at = conn.access_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
        return access_token

    refresh_token = calendar_storage.decrypt_token(conn.refresh_token)
    try:
        new_access, new_expires = await _refresh_access_token(refresh_token)
    except InvalidGrantError:
        await calendar_storage.mark_invalid_grant(conn.user_id)
        raise

    await calendar_storage.update_token_cache(conn.user_id, new_access, new_expires)
    return new_access


async def revoke_token(token: str) -> None:
    """Revoke a Google OAuth token (best-effort, never raises)."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": token},
                timeout=10,
            )
    except Exception:
        pass


# ── Google Calendar API helpers ───────────────────────────────────────

async def _gcal(
    method: str,
    path: str,
    *,
    access_token: str,
    json: dict | None = None,
    params: dict | None = None,
) -> dict | None:
    url = f"{GCAL_BASE}{path}"
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            json=json,
            params=params,
            timeout=20,
        )
    if resp.status_code == 204:
        return None
    if resp.status_code == 404 and method == "DELETE":
        return None
    resp.raise_for_status()
    return resp.json() if resp.content else None


async def create_calendar(access_token: str) -> str:
    result = await _gcal(
        "POST",
        "/calendars",
        access_token=access_token,
        json={
            "summary": "MLSC Timetable",
            "description": "Auto-synced timetable from your MLSC batch schedule.",
            "timeZone": "Asia/Kolkata",
        },
    )
    return result["id"]  # type: ignore[index]


async def delete_calendar(calendar_id: str, access_token: str) -> None:
    try:
        await _gcal("DELETE", f"/calendars/{calendar_id}", access_token=access_token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise


async def _list_all_events(calendar_id: str, access_token: str) -> list[dict]:
    events: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"maxResults": 250, "singleEvents": False}
        if page_token:
            params["pageToken"] = page_token
        result = await _gcal(
            "GET",
            f"/calendars/{calendar_id}/events",
            access_token=access_token,
            params=params,
        )
        if result is None:
            break
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


async def _delete_events_batch(
    calendar_id: str,
    access_token: str,
    event_ids: list[str],
) -> None:
    """Delete events concurrently in batches of 10."""
    for i in range(0, len(event_ids), 10):
        chunk = event_ids[i : i + 10]
        await asyncio.gather(
            *[
                _gcal(
                    "DELETE",
                    f"/calendars/{calendar_id}/events/{eid}",
                    access_token=access_token,
                )
                for eid in chunk
            ],
            return_exceptions=True,
        )


async def _create_events_batch(
    calendar_id: str,
    access_token: str,
    events: list[dict],
) -> list[str]:
    """Create events concurrently in batches of 10. Returns list of created event IDs."""
    event_ids: list[str] = []
    for i in range(0, len(events), 10):
        chunk = events[i : i + 10]
        results = await asyncio.gather(
            *[
                _gcal(
                    "POST",
                    f"/calendars/{calendar_id}/events",
                    access_token=access_token,
                    json={k: v for k, v in ev.items() if not k.startswith("_")},
                )
                for ev in chunk
            ],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, dict) and result.get("id"):
                event_ids.append(result["id"])
    return event_ids


# ── Slot ID & event building ──────────────────────────────────────────

def compute_slot_id(
    batch: str,
    day: str,
    start_time: str,
    code: str | None,
    room: str | None,
) -> str:
    raw = f"{batch}|{day.lower()}|{start_time}|{(code or '').upper()}|{(room or '').upper()}"
    return hashlib.sha1(raw.encode()).hexdigest()


_BYDAY_MAP = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE",
    "thursday": "TH", "friday": "FR", "saturday": "SA", "sunday": "SU",
}
_WEEKDAY_IDX = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
_BYDAY_TO_WEEKDAY = {v: k for k, v in _BYDAY_MAP.items()}


def _day_to_byday(day: str) -> str | None:
    return _BYDAY_MAP.get(day.lower())


def _day_to_weekday_idx(day: str) -> int | None:
    return _WEEKDAY_IDX.get(day.lower())


def _parse_time(time_str: str) -> str:
    """Convert '9:00 AM' or '09:00' to 'HH:MM:SS' (24-hour)."""
    t = time_str.strip().upper()
    try:
        if "AM" in t or "PM" in t:
            dt = datetime.strptime(t, "%I:%M %p")
        elif ":" in t:
            parts = t.split(":")
            dt = datetime.strptime(f"{parts[0].zfill(2)}:{parts[1]}", "%H:%M")
        else:
            return "00:00:00"
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return "00:00:00"


def _next_occurrence_of_weekday(weekday_idx: int) -> date:
    today = date.today()
    days_ahead = weekday_idx - today.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def _first_occurrence_on_or_after(weekday_idx: int, from_date: date) -> date:
    """Return the first date >= from_date that falls on weekday_idx (0=Mon)."""
    days_ahead = weekday_idx - from_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)


# Google Calendar colorId by class type.
_TYPE_COLOR: dict[str, str] = {
    "lecture":   "7",  # Peacock (teal)
    "tutorial":  "2",  # Sage (green)
    "practical": "6",  # Tangerine (orange)
}


def _color_for_type(class_type: str) -> str | None:
    return _TYPE_COLOR.get((class_type or "").lower())


def _build_description(entry: dict) -> str:
    """Build the event description carrying the details we don't want in the
    title — the subject code (so it's still searchable/visible), class type,
    teacher, and any elective options. The title shows the human-readable
    course name; this is the "hidden" companion metadata.
    """
    lines: list[str] = []
    code = (entry.get("code") or "").strip()
    if code:
        lines.append(f"Code: {code}")
    ctype = (entry.get("type") or "").strip()
    if ctype and ctype.lower() != "unknown":
        lines.append(f"Type: {ctype}")
    teacher = (entry.get("teacher") or "").strip()
    if teacher:
        lines.append(f"Teacher: {teacher}")

    opts = entry.get("options")
    if isinstance(opts, list) and opts:
        lines.append("Options:")
        for o in opts:
            if not isinstance(o, dict):
                continue
            oc = (o.get("subject_code") or "").strip()
            on = (o.get("subject_name") or "").strip()
            op = (o.get("place") or "").strip()
            ot = (o.get("teacher") or "").strip()
            label = on or oc or "?"
            extras = [x for x in [oc if (on and oc) else None, op, ot] if x]
            piece = label + (f" ({', '.join(extras)})" if extras else "")
            lines.append(f"  • {piece}")
    return "\n".join(lines)


_SUBJECT_CODE_RE = re.compile(r"^U[A-Z]{2,4}\d{3,4}[LTP]?$", re.I)


def _course_name(entry: dict, catalog: Any = None) -> str:
    """Resolve a timetable entry to a name, never treating a code as a name."""
    code = str(entry.get("code") or "").strip()
    stored = str(entry.get("subject") or "").strip()
    if stored and not _SUBJECT_CODE_RE.fullmatch(stored):
        return stored
    if code and catalog is not None:
        name = catalog.name_for(code)
        if name:
            return name
    try:
        from timetable_parser.core.subject_catalog import SubjectCatalog
        fallback = SubjectCatalog.load_from_file()
        name = fallback.name_for(code)
        if name:
            return name
    except Exception:
        pass
    return code or stored or "Class"


# ── Idempotent-reconcile helpers ──────────────────────────────────────
# A synced event is uniquely identified by its ``mlscSlotId`` (stored in
# extendedProperties). To decide whether an already-present event still
# matches what we would create, we compare a normalized signature of the
# visible fields. Google echoes start/end dateTimes back *with* a timezone
# offset (e.g. ``...T09:00:00+05:30``) whereas we send them without one, so
# we normalize to the first 19 chars (local ``YYYY-MM-DDTHH:MM:SS``).

def _slot_id_of(ev: dict) -> str | None:
    return (
        (ev.get("extendedProperties") or {})
        .get("private", {})
        .get("mlscSlotId")
    )


def _norm_dt(obj: dict | None) -> tuple:
    if not obj:
        return ()
    if obj.get("date"):
        return ("date", obj["date"])
    dt = str(obj.get("dateTime", ""))
    return ("dateTime", dt[:19], obj.get("timeZone", ""))


def _norm_recurrence(rules: list[str] | None) -> tuple:
    """Normalize a recurrence array for comparison.

    Google may echo an RRULE back with its ``KEY=VALUE`` parts reordered or
    re-cased relative to what we sent, so we canonicalize each line: uppercase,
    drop the ``RRULE:``/``EXDATE`` type prefix into a tag, and sort the parts.
    This keeps unchanged events from being needlessly recreated every sync.
    """
    out: list[tuple] = []
    for raw in rules or []:
        line = str(raw).strip().upper()
        if not line:
            continue
        tag, _, rest = line.partition(":")
        # EXDATE lines carry a TZID param in the tag half (EXDATE;TZID=...).
        parts = tuple(sorted(p for p in rest.replace(";", ",").split(",") if p))
        out.append((tag, parts))
    return tuple(sorted(out))


def _event_signature(ev: dict) -> tuple:
    """Order-independent signature of the fields that define an event's
    visible content. Works for both our desired events and Google's echoed
    events because both use the same schema.
    """
    return (
        ev.get("summary", "") or "",
        ev.get("description", "") or "",
        ev.get("colorId", "") or "",
        _norm_dt(ev.get("start")),
        _norm_dt(ev.get("end")),
        _norm_recurrence(ev.get("recurrence")),
    )


def _event_duplicate_key(ev: dict) -> tuple:
    """Identify visually duplicated event instances, including legacy events
    that predate ``mlscSlotId`` or were created during a concurrent sync."""
    return (
        ev.get("summary", "") or "",
        _norm_dt(ev.get("start")),
        _norm_dt(ev.get("end")),
    )


def _build_base_event(
    batch: str,
    entry: dict,
    term_end_date: str,
    exdates_by_byday: dict[str, list[str]],
    *,
    term_start_date: str | None = None,
    catalog: Any = None,
    alternate_week_start: int | None = None,
) -> dict | None:
    day = entry.get("day", "")
    byday = _day_to_byday(day)
    weekday_idx = _day_to_weekday_idx(day)
    if not byday or weekday_idx is None or weekday_idx > 4:
        return None  # Skip Saturday/Sunday base events

    start_time_str = entry.get("start_time", "")
    end_time_str = entry.get("end_time", "")
    start_hms = _parse_time(start_time_str)
    end_hms = _parse_time(end_time_str)

    # Use term start date so the recurring event covers the full semester,
    # not just "next week onwards".
    if term_start_date:
        try:
            anchor = _first_occurrence_on_or_after(weekday_idx, date.fromisoformat(term_start_date))
        except ValueError:
            anchor = _next_occurrence_of_weekday(weekday_idx)
    else:
        anchor = _next_occurrence_of_weekday(weekday_idx)
    event_date_str = anchor.strftime("%Y-%m-%d")
    until = term_end_date.replace("-", "") + "T000000Z"

    is_elective_group = len(entry.get("options") or []) > 1 and not entry.get("electiveChoice")
    code = entry.get("code") or ""
    # Prefer the human-readable name: stored ``subject`` → catalog lookup by
    # code → the raw code as a last resort. Names are stripped on write and
    # resolved on read elsewhere, so without the catalog fill this path would
    # otherwise show bare codes.
    subject = "Elective" if is_elective_group else _course_name(entry, catalog)
    room = entry.get("room") or ""
    class_type = "" if is_elective_group else entry.get("type", "")

    summary_parts = [subject]
    if room:
        summary_parts.append(f"({room})")
    summary = " ".join(summary_parts)
    description = _build_description({**entry, "code": code})

    recurrence = [f"RRULE:FREQ=WEEKLY;BYDAY={byday};UNTIL={until}"]
    for exdate in exdates_by_byday.get(byday, []):
        time_compact = start_hms.replace(":", "")
        recurrence.append(
            f"EXDATE;TZID=Asia/Kolkata:{exdate.replace('-', '')}T{time_compact}"
        )
    if alternate_week_start in (1, 2) and term_start_date:
        try:
            term_date = date.fromisoformat(term_start_date)
            first = _first_occurrence_on_or_after(weekday_idx, term_date)
            cursor = first
            week_number = 1
            while cursor <= date.fromisoformat(term_end_date):
                if week_number % 2 != alternate_week_start % 2:
                    time_compact = start_hms.replace(":", "")
                    recurrence.append(
                        f"EXDATE;TZID=Asia/Kolkata:{cursor.strftime('%Y%m%d')}T{time_compact}"
                    )
                cursor += timedelta(days=7)
                week_number += 1
        except ValueError:
            pass

    slot_id = compute_slot_id(batch, day, start_time_str, code, room)
    event: dict = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": f"{event_date_str}T{start_hms}", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": f"{event_date_str}T{end_hms}", "timeZone": "Asia/Kolkata"},
        "recurrence": recurrence,
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {
            "private": {
                "mlscSlotId": slot_id,
                "mlscKind": "base",
                "mlscBatchCode": batch,
            }
        },
        "_slot_id": slot_id,
    }
    color = _color_for_type(class_type)
    if color:
        event["colorId"] = color
    return event


def _build_oneoff_event(
    batch: str,
    override_date: str,
    source_entry: dict,
    override_id: str,
    *,
    catalog: Any = None,
) -> dict:
    """Build a one-off event for a follow_day override (substitute schedule)."""
    start_time_str = source_entry.get("start_time", "")
    end_time_str = source_entry.get("end_time", "")
    start_hms = _parse_time(start_time_str)
    end_hms = _parse_time(end_time_str)

    code = source_entry.get("code") or ""
    subject = _course_name(source_entry, catalog)
    room = source_entry.get("room") or ""
    class_type = source_entry.get("type", "")

    summary_parts = [subject]
    if room:
        summary_parts.append(f"({room})")
    summary = " ".join(summary_parts)
    description = _build_description({**source_entry, "code": code})

    slot_id = compute_slot_id(
        batch, f"shift:{override_date}", start_time_str, code, room
    )
    event: dict = {
        "summary": f"[Rescheduled] {summary}",
        "description": description,
        "start": {"dateTime": f"{override_date}T{start_hms}", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": f"{override_date}T{end_hms}", "timeZone": "Asia/Kolkata"},
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {
            "private": {
                "mlscSlotId": slot_id,
                "mlscKind": "shift",
                "mlscBatchCode": batch,
                "mlscOverrideId": override_id,
            }
        },
        "_slot_id": slot_id,
    }
    color = _color_for_type(class_type)
    if color:
        event["colorId"] = color
    return event


def _build_allday_event(
    batch: str,
    override: dict,
) -> dict:
    """Build an all-day informational event for MST/EST/Assessment periods."""
    kind = override.get("kind", "")
    reason = override.get("reason") or kind.upper()
    ov_date = override["date"]
    ov_id = str(override.get("id", ""))
    slot_id = compute_slot_id(batch, f"{kind}:{ov_date}", "", ov_id, "")
    return {
        "summary": reason,
        "start": {"date": ov_date},
        "end": {"date": ov_date},
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {
            "private": {
                "mlscSlotId": slot_id,
                "mlscKind": kind,
                "mlscBatchCode": batch,
            }
        },
        "_slot_id": slot_id,
    }


# ── Core sync algorithm ───────────────────────────────────────────────

async def _ensure_calendar(conn: CalendarConnectionDoc, access_token: str) -> str:
    """Return calendar_id, reusing an existing 'MLSC Timetable' calendar if found.

    Lookup priority:
    1. Stored ``calendar_id`` — verify it still exists on Google.
    2. Scan the user's calendar list for a calendar named 'MLSC Timetable'.
    3. Create a fresh calendar.
    """
    if conn.calendar_id:
        try:
            result = await _gcal(
                "GET",
                f"/calendars/{conn.calendar_id}",
                access_token=access_token,
            )
            if result:
                return conn.calendar_id
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        # 404 — calendar was deleted externally; fall through

    # Scan the user's calendar list to reuse any existing 'MLSC Timetable'
    # calendar (prevents duplicates if the user reconnects or the stored ID
    # was wiped without the underlying Google calendar being deleted).
    try:
        cal_list = await _gcal("GET", "/users/me/calendarList", access_token=access_token)
        for cal in (cal_list or {}).get("items", []):
            if cal.get("summary") == "MLSC Timetable":
                cal_id = cal["id"]
                await calendar_storage.update_after_sync(conn.user_id, calendar_id=cal_id)
                return cal_id
    except Exception:
        pass  # Best-effort scan; fall through to create

    cal_id = await create_calendar(access_token)
    await calendar_storage.update_after_sync(conn.user_id, calendar_id=cal_id)
    return cal_id


import re as _re
_LABEL_YEAR_RE = _re.compile(r"(\d{2,4})-(\d{2,4})")

def _semester_fallback_date(label: str) -> str:
    """Derive a sensible RRULE UNTIL from the semester label.

    ``"ODD 25-26"``  → odd sem ends Dec → ``2025-12-31``
    ``"EVEN 25-26"`` → even sem ends May → ``2026-05-31``
    Falls back to current year Dec 31 if the label can't be parsed.
    """
    label = (label or "").upper()
    is_even = label.startswith("EVEN")
    m = _LABEL_YEAR_RE.search(label)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y1 < 100:
            y1 += 2000
        if y2 < 100:
            y2 += 2000
        sem_year = y2 if is_even else y1
        return f"{sem_year}-05-31" if is_even else f"{sem_year}-12-31"
    today = date.today()
    return f"{today.year}-05-31" if is_even else f"{today.year}-12-31"


def _semester_start_fallback(label: str) -> str:
    """Derive a sensible RRULE DTSTART from the semester label.

    ``"ODD 25-26"``  → odd sem starts ~Aug → ``2025-08-01``
    ``"EVEN 25-26"`` → even sem starts ~Jan → ``2026-01-01``
    Falls back to a date six months ago if the label can't be parsed.
    """
    label = (label or "").upper()
    is_even = label.startswith("EVEN")
    m = _LABEL_YEAR_RE.search(label)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y1 < 100:
            y1 += 2000
        if y2 < 100:
            y2 += 2000
        sem_year = y2 if is_even else y1
        return f"{sem_year}-01-01" if is_even else f"{sem_year}-08-01"
    today = date.today()
    fallback = today - timedelta(days=180)
    return fallback.strftime("%Y-%m-%d")


async def full_sync_user(user_id: str, *, force: bool = False) -> None:
    """Full re-sync: wipe all events in the MLSC calendar, recreate from timetable + overrides.

    When ``force=True`` (manual trigger), runs even if auto-sync is disabled.
    """
    from server.db.models import OverrideDoc, TimetableDoc
    from server import storage as main_storage

    conn = await calendar_storage.get_connection(user_id)
    if conn is None:
        return
    if not conn.enabled and not force:
        return

    settings = get_settings()
    # Resolve term end date: DB year-keyed dict → env var → semester-aware fallback.
    # Resolve term start date: DB year-keyed dict → semester-aware fallback.
    try:
        current_doc = await main_storage.read_current()
        term_end_dates = current_doc.get("term_end_dates") or {}
        term_start_dates = current_doc.get("term_start_dates") or {}
        # Batch code like "1B14" → year "1"; PG codes that start with letters → "1"
        batch_year = str((conn.batch_code or "1")[0]) if (conn.batch_code or "1")[0].isdigit() else "1"
        # Smart fallback: parse semester label → derive the actual semester year + parity.
        #   "ODD 25-26"  → odd,  semester year 2025 → fallback 2025-12-31 / 2025-08-01
        #   "EVEN 25-26" → even, semester year 2026 → fallback 2026-05-31 / 2026-01-01
        label = (current_doc.get("label") or "").upper()
        sem_fallback_end = _semester_fallback_date(label)
        sem_fallback_start = _semester_start_fallback(label)
        term_end = (
            term_end_dates.get(batch_year)
            or settings.calendar_term_end_date
            or sem_fallback_end
        )
        term_start = term_start_dates.get(batch_year) or sem_fallback_start
    except Exception:
        term_end = settings.calendar_term_end_date or f"{date.today().year}-12-31"
        term_start = None

    try:
        access_token = await get_valid_access_token(conn)
    except InvalidGrantError:
        return

    calendar_id = await _ensure_calendar(conn, access_token)

    batch = (conn.batch_code or "").upper()
    if not batch:
        logger.warning("calendar_sync: user %s has no batch_code set, skipping", user_id)
        return

    tt = await TimetableDoc.find_one(TimetableDoc.code == batch)
    if tt is None:
        logger.warning("calendar_sync: no timetable for batch %s", batch)
        return

    classes = [
        c.model_dump() if hasattr(c, "model_dump") else dict(c)
        for c in (tt.classes or [])
    ]
    user_overrides = await OverrideDoc.find_one(
        OverrideDoc.user_id == user_id,
        OverrideDoc.batch == batch,
    )
    if user_overrides is not None:
        by_slot = user_overrides.entries or {}
        merged_classes: list[dict] = []
        for entry in classes:
            override = by_slot.get(f"{entry.get('day', '')}|{entry.get('start_time', '')}")
            if override is not None:
                if override.kind == "delete" or override.entry is None:
                    continue
                merged_classes.append(override.entry.model_dump(exclude_none=False))
            else:
                merged_classes.append(entry)
        for key, override in by_slot.items():
            if override.kind == "add" and override.entry is not None:
                merged_classes.append(override.entry.model_dump(exclude_none=False))
        classes = merged_classes

    # Subject names are stripped on write and resolved on read; the calendar
    # path must resolve them too so events show course names, not bare codes.
    from timetable_parser.core.subject_catalog import ensure_catalog
    try:
        catalog = await ensure_catalog()
    except Exception:
        catalog = None
    if catalog is not None and not catalog.subjects:
        try:
            from timetable_parser.core.subject_catalog import SubjectCatalog
            catalog = SubjectCatalog.load_from_file()
        except Exception:
            pass

    # Get all calendar overrides scoped to this batch
    overrides = await main_storage.list_calendar_overrides(batch=batch)

    # ── Pass 1: Collect all EXDATEs per BYDAY ──────────────────────────
    # Sources: holiday overrides + follow_day overrides on weekdays
    exdates_by_byday: dict[str, list[str]] = {}  # "MO" -> ["2026-08-15"]
    follow_day_overrides: list[dict] = []
    allday_overrides: list[dict] = []
    holiday_dates: set[str] = set()

    for ov in overrides:
        kind = ov.get("kind", "")
        ov_date = ov.get("date", "")
        if kind == "holiday":
            holiday_dates.add(ov_date)
            try:
                d = date.fromisoformat(ov_date)
                wd = d.weekday()
                if wd <= 4:  # Mon-Fri
                    byday = ["MO", "TU", "WE", "TH", "FR"][wd]
                    exdates_by_byday.setdefault(byday, []).append(ov_date)
            except ValueError:
                pass
        elif kind == "follow_day":
            follow_day_overrides.append(ov)
            # If the override date falls on a Mon-Fri, cancel that day's own schedule
            try:
                d = date.fromisoformat(ov_date)
                wd = d.weekday()
                if wd <= 4:
                    byday = ["MO", "TU", "WE", "TH", "FR"][wd]
                    exdates_by_byday.setdefault(byday, []).append(ov_date)
            except ValueError:
                pass
        elif kind in ("mst", "est", "assessment", "frosh"):
            allday_overrides.append(ov)
            # Skip regular classes on these days too
            try:
                d = date.fromisoformat(ov_date)
                wd = d.weekday()
                if wd <= 4:  # Mon-Fri only
                    byday = ["MO", "TU", "WE", "TH", "FR"][wd]
                    exdates_by_byday.setdefault(byday, []).append(ov_date)
            except ValueError:
                pass

    # ── Pass 2: Build base recurring events ────────────────────────────
    events_to_create: list[dict] = []
    for entry in classes:
        event = _build_base_event(
            batch, entry, term_end, exdates_by_byday,
            term_start_date=term_start, catalog=catalog,
            alternate_week_start=entry.get("alternate_week_start"),
        )
        if event is not None:
            events_to_create.append(event)

    # ── Pass 3: Build follow-day one-off events ────────────────────────
    classes_by_weekday: dict[int, list[dict]] = {}
    for entry in classes:
        day = entry.get("day", "")
        idx = _day_to_weekday_idx(day)
        if idx is not None:
            classes_by_weekday.setdefault(idx, []).append(entry)

    for ov in follow_day_overrides:
        ov_date = ov.get("date", "")
        if ov_date in holiday_dates:
            # A holiday always wins over a compensating/follow-day rule.
            continue
        follows_day = ov.get("follows_day")
        ov_id = str(ov.get("id", ""))
        if not isinstance(follows_day, int) or not (0 <= follows_day <= 4):
            continue
        for src_entry in classes_by_weekday.get(follows_day, []):
            events_to_create.append(
                _build_oneoff_event(batch, ov_date, src_entry, ov_id, catalog=catalog)
            )

    # ── Pass 4: All-day exam-period events ────────────────────────────
    for ov in allday_overrides:
        events_to_create.append(_build_allday_event(batch, ov))

    # ── Execute: reconcile (keep matching, delete stale, create missing) ──
    # A plain wipe-then-recreate double-books the calendar on re-sync:
    # deletions are best-effort and Google doesn't reliably remove a recurring
    # series before the recreate lands, so the old tiles linger next to the new
    # ones. Instead we diff by the stable ``mlscSlotId`` each event carries and
    # only touch what actually changed.
    try:
        existing_events = await _list_all_events(calendar_id, access_token)

        # Index existing events by slot_id. If Google somehow holds two events
        # for the same slot (from a prior buggy wipe-and-recreate), keep the
        # first and mark the rest for deletion so re-sync self-heals dupes.
        existing_by_slot: dict[str, dict] = {}
        stale_ids: list[str] = []
        existing_by_visual_key: dict[tuple, str] = {}
        for ev in existing_events:
            eid = ev.get("id")
            if not eid:
                continue
            slot = _slot_id_of(ev)
            if not slot:
                # Not one of ours (or legacy without the marker) — remove it so
                # the MLSC calendar only ever contains events we manage.
                stale_ids.append(eid)
                continue
            visual_key = _event_duplicate_key(ev)
            if visual_key in existing_by_visual_key:
                stale_ids.append(eid)
                continue
            existing_by_visual_key[visual_key] = eid
            if slot in existing_by_slot:
                stale_ids.append(eid)  # duplicate of a slot we already kept
            else:
                existing_by_slot[slot] = ev

        desired_by_slot: dict[str, dict] = {}
        for ev in events_to_create:
            slot = ev.get("_slot_id")
            if slot:
                desired_by_slot[slot] = ev

        # Delete events whose slot is no longer desired.
        for slot, ev in existing_by_slot.items():
            if slot not in desired_by_slot:
                stale_ids.append(ev["id"])

        # Create events whose slot is new, or whose content changed. When
        # content changed we delete the old one and recreate (simplest way to
        # fully replace recurrence/EXDATEs without patch edge-cases).
        to_create: list[dict] = []
        for slot, ev in desired_by_slot.items():
            existing = existing_by_slot.get(slot)
            if existing is None:
                to_create.append(ev)
            elif _event_signature(existing) != _event_signature(ev):
                stale_ids.append(existing["id"])
                to_create.append(ev)
            # else: unchanged — leave the Google event exactly as-is.

        if stale_ids:
            await _delete_events_batch(calendar_id, access_token, stale_ids)
        if to_create:
            await _create_events_batch(calendar_id, access_token, to_create)

        await calendar_storage.update_after_sync(
            user_id,
            last_synced_at=datetime.now(timezone.utc),
            last_error=None,
        )
        logger.info(
            "calendar_sync: reconciled batch %s for user %s "
            "(desired=%d, created=%d, deleted=%d, unchanged=%d)",
            batch,
            user_id,
            len(desired_by_slot),
            len(to_create),
            len(stale_ids),
            len(desired_by_slot) - len(to_create),
        )
    except Exception as exc:
        logger.exception("calendar_sync: sync failed for user %s", user_id)
        await calendar_storage.update_after_sync(
            user_id, last_error=str(exc)[:200]
        )
        raise


# ── Background worker ─────────────────────────────────────────────────

_RETRY_DELAYS = [30, 300, 1800, 14400]  # 30s, 5m, 30m, 4h
_MAX_ATTEMPTS = 5


async def _run_sync_job(job_doc: dict) -> None:
    """Execute one sync job dict (from MongoDB raw doc)."""
    user_id = job_doc.get("user_id", "")
    job_id = str(job_doc.get("_id", ""))

    coll = CalendarSyncJobDoc.get_motor_collection()

    await coll.update_one(
        {"_id": job_doc["_id"]},
        {"$set": {"status": "running", "updated_at": datetime.now(timezone.utc)}},
    )

    try:
        # Manual triggers bypass the enabled check so users can sync on demand
        # even when auto-sync is paused.
        force = job_doc.get("trigger") in ("manual", "initial")
        await full_sync_user(user_id, force=force)
        await coll.update_one(
            {"_id": job_doc["_id"]},
            {"$set": {"status": "done", "updated_at": datetime.now(timezone.utc)}},
        )
    except Exception as exc:
        attempts = int(job_doc.get("attempts", 0)) + 1
        if attempts >= _MAX_ATTEMPTS:
            await coll.update_one(
                {"_id": job_doc["_id"]},
                {
                    "$set": {
                        "status": "failed",
                        "attempts": attempts,
                        "last_error": str(exc)[:300],
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            logger.error(
                "calendar_sync: job %s for user %s permanently failed after %d attempts",
                job_id,
                user_id,
                attempts,
            )
        else:
            delay = _RETRY_DELAYS[min(attempts - 1, len(_RETRY_DELAYS) - 1)]
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            await coll.update_one(
                {"_id": job_doc["_id"]},
                {
                    "$set": {
                        "status": "pending",
                        "attempts": attempts,
                        "last_error": str(exc)[:300],
                        "updated_at": retry_at,  # poll checks updated_at <= now
                    }
                },
            )
            logger.warning(
                "calendar_sync: job %s for user %s failed (attempt %d/%d), retrying in %ds",
                job_id, user_id, attempts, _MAX_ATTEMPTS, delay,
            )


async def calendar_worker() -> None:
    """Background worker: polls pending CalendarSyncJobDoc rows and executes them.
    Uses find_one_and_update for per-user locking (SKIP LOCKED semantics).
    Started once in app lifespan.
    """
    coll = CalendarSyncJobDoc.get_motor_collection()
    running_users: set[str] = set()

    logger.info("calendar_sync: worker started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            query = {
                "status": "pending",
                "user_id": {"$nin": list(running_users)},
                "updated_at": {"$lte": now},
            }
            job = await coll.find_one_and_update(
                query,
                {"$set": {"status": "running", "updated_at": now}},
                sort=[("created_at", 1)],
                return_document=True,
            )
            if job is None:
                await asyncio.sleep(10)
                continue

            user_id = job.get("user_id", "")
            running_users.add(user_id)
            try:
                await _run_sync_job(job)
            finally:
                running_users.discard(user_id)

        except asyncio.CancelledError:
            logger.info("calendar_sync: worker cancelled")
            break
        except Exception:
            logger.exception("calendar_sync: worker error")
            await asyncio.sleep(10)


def start_worker() -> asyncio.Task:
    """Create and return the worker task. Call from app lifespan."""
    global _worker_task
    loop = asyncio.get_event_loop()
    _worker_task = loop.create_task(calendar_worker())
    return _worker_task


def stop_worker() -> None:
    """Cancel the worker task. Call from app lifespan shutdown."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
    _worker_task = None
