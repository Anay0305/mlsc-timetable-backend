"""Admin authentication — single shared bearer token (v1)."""

from __future__ import annotations

import hmac
import re

from fastapi import Header, HTTPException, status

from server.config import get_settings


def require_admin(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Admin disabled (ADMIN_TOKEN not configured)", "code": "admin_disabled"},
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Missing bearer token", "code": "missing_token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid bearer token", "code": "invalid_token"},
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── User identity (v1: client-managed opaque id via X-User-Id) ──────────────
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")


def require_user_id(x_user_id: str | None = Header(default=None, alias="X-User-Id")) -> str:
    """Return the caller's opaque user id from the `X-User-Id` header.

    Until real auth lands the client just persists a UUID in localStorage and
    sends it on every request. The dependency raises 400 if the header is
    missing or malformed (no auto-mint here — the caller controls identity).
    """
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Missing X-User-Id header", "code": "missing_user_id"},
        )
    if not _USER_ID_PATTERN.match(x_user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "X-User-Id must be 4–64 chars of [A-Za-z0-9_-]",
                "code": "invalid_user_id",
            },
        )
    return x_user_id

