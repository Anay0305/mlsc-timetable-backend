"""OAuth scope handling for the Google Calendar connection.

Google's consent screen lets a user tick each requested permission
individually. Three production users unticked Calendar, so the token exchange
came back with only ``openid`` and ``userinfo.email``. The connection was
stored as if it were complete, sync was enabled, and every attempt to create
their calendar returned ``403``. One of them pressed re-sync nine times in four
minutes; the app never told him anything was wrong.

The token response carries the scopes actually granted. Checking it here turns
a permanent, silent 403 into a message the user can act on.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
OPENID_SCOPE = "openid"

# What we ask for. Only the calendar scope is load-bearing; the other two just
# let us label the connection with the account it belongs to.
REQUESTED_SCOPES = (CALENDAR_SCOPE, EMAIL_SCOPE, OPENID_SCOPE)
REQUIRED_SCOPES = frozenset({CALENDAR_SCOPE})

SCOPE_PARAM = " ".join(REQUESTED_SCOPES)


def granted_scopes(token_data: Mapping[str, Any] | None) -> frozenset[str]:
    """Scopes Google says it granted, from a token or refresh response."""
    raw = (token_data or {}).get("scope") or ""
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts: Iterable[str] = raw
    else:
        parts = str(raw).split()
    return frozenset(part.strip() for part in parts if part and part.strip())


def missing_scopes(granted: Iterable[str]) -> frozenset[str]:
    """Required scopes the user withheld. Empty means the connection works."""
    return frozenset(REQUIRED_SCOPES) - frozenset(granted or ())


def has_calendar_access(granted: Iterable[str]) -> bool:
    return not missing_scopes(granted)


def consent_error_message(missing: Iterable[str]) -> str:
    """What to show the user, in terms of the checkbox they need to tick."""
    if CALENDAR_SCOPE in set(missing or ()):
        return (
            "Calendar permission was not granted. Google shows a separate tick box "
            "for each permission — please connect again and leave "
            "“See, edit, share and permanently delete all the calendars” ticked."
        )
    return "Some required permissions were not granted. Please connect again."
