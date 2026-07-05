"""Clerk JWT verification.

Verifies session JWTs minted by Clerk against the JWKS published at
``<issuer>/.well-known/jwks.json``. Designed for the admin allowlist flow:
frontend calls ``session.getToken({ template: "mlsc-admin" })`` (or the
default session token if it already includes the email claim), backend
verifies + extracts the verified email.

`PyJWKClient` caches signing keys in-process automatically.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from server.config import get_settings

logger = logging.getLogger(__name__)


class ClerkJWTError(ValueError):
    """JWT failed verification (bad sig, expired, wrong issuer, missing claim)."""


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient | None:
    settings = get_settings()
    url = settings.clerk_jwks_url
    if not url:
        return None
    # PyJWKClient caches keys with a default 5-min lifespan and refetches on miss.
    return PyJWKClient(url, cache_keys=True, lifespan=3600)


def is_clerk_configured() -> bool:
    settings = get_settings()
    return bool(settings.clerk_jwks_url and settings.clerk_issuer)


def verify_clerk_jwt(token: str) -> dict[str, Any]:
    """Validate signature + issuer + expiry; return the decoded claims.

    Raises ``ClerkJWTError`` on any failure (the caller turns this into a 401).
    """
    settings = get_settings()
    if not is_clerk_configured():
        raise ClerkJWTError("Clerk auth not configured on backend")

    client = _jwks_client()
    if client is None:
        raise ClerkJWTError("JWKS client unavailable")

    try:
        signing_key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            # Clerk tokens have no `aud` by default; allow that.
            options={"verify_aud": False, "require": ["exp", "iss"]},
            leeway=30,  # tolerate small clock skew
        )
    except PyJWTError as exc:
        raise ClerkJWTError(f"invalid Clerk token: {exc}") from exc
    return claims


def email_from_claims(claims: dict[str, Any]) -> str | None:
    """Pull the verified email out of the Clerk JWT claims.

    Supports both custom JWT templates (``email`` claim) and the default
    session token shape where the email lives under
    ``primary_email_address``.
    """
    for key in ("email", "primary_email_address", "email_address"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    # Some templates put it in a nested user payload.
    user = claims.get("user")
    if isinstance(user, dict):
        for key in ("email", "primary_email_address"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return None
