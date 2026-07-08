"""Runtime configuration sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cors_origins: tuple[str, ...]
    admin_token: str | None
    admin_emails: frozenset[str]
    clerk_issuer: str | None
    clerk_jwks_url: str | None
    git_auto_commit: bool
    mongodb_url: str
    mongodb_db: str
    json_mirror: bool
    ingest_cooldown_hours: float
    ingest_snapshot_ttl_hours: float
    # Google Calendar integration
    google_oauth_client_id: str | None
    google_oauth_client_secret: str | None
    google_oauth_redirect_uri: str | None
    calendar_token_key: str | None  # Fernet key for encrypting OAuth tokens
    calendar_term_end_date: str | None  # yyyy-mm-dd e.g. "2026-04-30"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", _REPO_ROOT / "data")).resolve()
    raw_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    cors_origins = tuple(origin.strip() for origin in raw_origins.split(",") if origin.strip())
    admin_token = os.environ.get("ADMIN_TOKEN", "ANAY")
    raw_admin_emails = os.environ.get("ADMIN_EMAILS", "")
    admin_emails = frozenset(
        email.strip().lower() for email in raw_admin_emails.split(",") if email.strip()
    )
    clerk_issuer = (os.environ.get("CLERK_ISSUER") or "").strip() or None
    if clerk_issuer:
        clerk_issuer = clerk_issuer.rstrip("/")
    clerk_jwks_url = (os.environ.get("CLERK_JWKS_URL") or "").strip() or None
    if not clerk_jwks_url and clerk_issuer:
        clerk_jwks_url = f"{clerk_issuer}/.well-known/jwks.json"
    git_auto_commit = _truthy(os.environ.get("GIT_AUTO_COMMIT", "0"))
    mongodb_url = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
    mongodb_db = os.environ.get("MONGODB_DB", "mlsc_timetable")
    json_mirror = _truthy(os.environ.get("JSON_MIRROR", "0"))
    try:
        ingest_cooldown_hours = float(os.environ.get("INGEST_COOLDOWN_HOURS", "24"))
    except ValueError:
        ingest_cooldown_hours = 24.0
    try:
        ingest_snapshot_ttl_hours = float(os.environ.get("INGEST_SNAPSHOT_TTL_HOURS", "24"))
    except ValueError:
        ingest_snapshot_ttl_hours = 24.0
    google_oauth_client_id = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip() or None
    google_oauth_client_secret = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip() or None
    google_oauth_redirect_uri = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip() or None
    calendar_token_key = (os.environ.get("CALENDAR_TOKEN_KEY") or "").strip() or None
    calendar_term_end_date = (os.environ.get("CALENDAR_TERM_END_DATE") or "").strip() or None
    return Settings(
        data_dir=data_dir,
        cors_origins=cors_origins,
        admin_token=admin_token,
        admin_emails=admin_emails,
        clerk_issuer=clerk_issuer,
        clerk_jwks_url=clerk_jwks_url,
        git_auto_commit=git_auto_commit,
        mongodb_url=mongodb_url,
        mongodb_db=mongodb_db,
        json_mirror=json_mirror,
        ingest_cooldown_hours=ingest_cooldown_hours,
        ingest_snapshot_ttl_hours=ingest_snapshot_ttl_hours,
        google_oauth_client_id=google_oauth_client_id,
        google_oauth_client_secret=google_oauth_client_secret,
        google_oauth_redirect_uri=google_oauth_redirect_uri,
        calendar_token_key=calendar_token_key,
        calendar_term_end_date=calendar_term_end_date,
    )


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}
