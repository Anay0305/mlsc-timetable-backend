"""Admin authentication.

Two credential paths are accepted; either one grants access:

1. ``Authorization: Bearer <ADMIN_TOKEN>`` — single shared token from env.
   Used by CLI scripts and CI.
2. ``Authorization: Bearer <Clerk JWT>`` — verified against Clerk's JWKS;
   the verified email claim must be in the admin allowlist
   (env-set ``ADMIN_EMAILS`` ∪ ``AdminEmailDoc`` collection).
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from server.config import get_settings

logger = logging.getLogger(__name__)


# Kept for /me/* routes which still use opaque per-browser ids.
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")


@dataclass(frozen=True)
class AdminPrincipal:
    """Who is making an admin request."""

    kind: str  # "user" | "token" | "cli"
    email: str | None = None  # set when kind == "user"

    @property
    def label(self) -> str:
        return self.email or self.kind


async def require_admin(
    authorization: str | None = Header(default=None),
) -> AdminPrincipal:
    """FastAPI dependency: authorize an admin request.

    Order: static admin-token bearer → Clerk JWT (verified email allowlist)
    → 401. If neither auth method is configured at all, returns 503
    ``admin_disabled``.
    """
    # Local import to avoid auth → storage → db cycle at module load.
    from server import clerk_jwt, storage

    settings = get_settings()

    token: str | None = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()

    # 1) Static admin token (CLI, scripts, curl)
    if token and settings.admin_token and hmac.compare_digest(token, settings.admin_token):
        return AdminPrincipal(kind="token")

    # 2) Clerk JWT → verified email
    if token and clerk_jwt.is_clerk_configured():
        try:
            claims = clerk_jwt.verify_clerk_jwt(token)
        except clerk_jwt.ClerkJWTError as exc:
            logger.info("clerk jwt rejected: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": str(exc), "code": "invalid_token"},
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        email = clerk_jwt.email_from_claims(claims)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "Clerk token has no email claim; add a 'mlsc-admin' JWT template with {\"email\": \"{{user.primary_email_address}}\"}",
                    "code": "missing_email_claim",
                },
            )
        try:
            allowed = await storage.is_admin_email(email)
        except Exception:
            logger.exception("admin allowlist lookup failed")
            allowed = False
        if allowed:
            return AdminPrincipal(kind="user", email=email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "Your email is not in the admin allowlist",
                "code": "not_admin",
                "email": email,
            },
        )

    # 3) Nothing worked — figure out the best error.
    if not settings.admin_token and not clerk_jwt.is_clerk_configured():
        try:
            db_admins = await storage.count_admin_emails()
        except Exception:
            db_admins = 0
        if db_admins == 0:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "Admin disabled (no ADMIN_TOKEN, no CLERK_ISSUER, no allowlisted emails)",
                    "code": "admin_disabled",
                },
            )

    if token:
        # Bearer present but didn't match the static token and Clerk isn't
        # configured (or the token wasn't a Clerk JWT) — generic invalid.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid bearer token", "code": "invalid_token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "Missing admin credentials (need Authorization: Bearer <token|clerk-jwt>)",
            "code": "missing_credentials",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── User identity (v1: client-managed opaque id via X-User-Id) ──────────────
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
    if not USER_ID_PATTERN.match(x_user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "X-User-Id must be 4–64 chars of [A-Za-z0-9_-]",
                "code": "invalid_user_id",
            },
        )
    return x_user_id
