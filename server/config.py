"""Runtime configuration sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Load the backend-local `.env` for direct `uvicorn server.app:app` runs.
# Existing process environment variables still win, which keeps deployment
# configuration and `uvicorn --env-file` behavior unchanged.
load_dotenv(_REPO_ROOT / ".env")


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
    mongodb_max_pool_size: int
    mongodb_min_pool_size: int
    mongodb_max_connecting: int
    mongodb_wait_queue_timeout_ms: int
    mongodb_server_selection_timeout_ms: int
    mongodb_connect_timeout_ms: int
    mongodb_socket_timeout_ms: int
    mongodb_max_idle_time_ms: int
    json_mirror: bool
    ingest_cooldown_hours: float
    ingest_snapshot_ttl_hours: float
    # Google Calendar integration
    google_oauth_client_id: str | None
    google_oauth_client_secret: str | None
    google_oauth_redirect_uri: str | None
    calendar_token_key: str | None  # Fernet key for encrypting OAuth tokens
    calendar_term_end_date: str | None  # yyyy-mm-dd e.g. "2026-04-30"
    # Published canonical timetable snapshots. ``local`` is intended for
    # development/single-host deployments; ``r2`` publishes through the
    # S3-compatible Cloudflare R2 API; ``disabled`` keeps dynamic reads only.
    public_snapshot_backend: str
    public_snapshot_dir: Path
    public_snapshot_base_url: str | None
    r2_endpoint_url: str | None
    r2_bucket: str | None
    r2_access_key_id: str | None
    r2_secret_access_key: str | None
    # Room/teacher schedule views. Room numbers are not sensitive, but teacher
    # codes are gated per batch by ``BatchDoc.teacher_codes_visible``; a public
    # teacher directory would bypass that control, so it stays admin-only until
    # explicitly opened.
    teacher_schedule_access: str  # "admin" | "public"
    schedule_index_ttl_seconds: float
    # Improvement (course re-take) planning limits.
    improvement_max_lecture_clashes: int
    improvement_max_tutorial_clashes: int
    improvement_max_practical_clashes: int
    improvement_pool_first_year_semesters: bool
    improvement_max_plan_options: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", _REPO_ROOT / "data")).resolve()
    raw_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    cors_origins = tuple(origin.strip() for origin in raw_origins.split(",") if origin.strip())
    admin_token = (os.environ.get("ADMIN_TOKEN") or "").strip() or None
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
    mongodb_max_pool_size = _positive_int("MONGODB_MAX_POOL_SIZE", 40)
    mongodb_min_pool_size = _non_negative_int("MONGODB_MIN_POOL_SIZE", 5)
    mongodb_max_connecting = _positive_int("MONGODB_MAX_CONNECTING", 4)
    mongodb_wait_queue_timeout_ms = _positive_int("MONGODB_WAIT_QUEUE_TIMEOUT_MS", 1000)
    mongodb_server_selection_timeout_ms = _positive_int(
        "MONGODB_SERVER_SELECTION_TIMEOUT_MS", 3000
    )
    mongodb_connect_timeout_ms = _positive_int("MONGODB_CONNECT_TIMEOUT_MS", 3000)
    mongodb_socket_timeout_ms = _positive_int("MONGODB_SOCKET_TIMEOUT_MS", 5000)
    mongodb_max_idle_time_ms = _positive_int("MONGODB_MAX_IDLE_TIME_MS", 60000)
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
    public_snapshot_backend = (os.environ.get("PUBLIC_SNAPSHOT_BACKEND") or "local").strip().lower()
    if public_snapshot_backend not in {"disabled", "local", "r2"}:
        public_snapshot_backend = "local"
    public_snapshot_dir = Path(
        os.environ.get("PUBLIC_SNAPSHOT_DIR", data_dir / "public")
    ).resolve()
    public_snapshot_base_url = (
        os.environ.get("PUBLIC_SNAPSHOT_BASE_URL") or ""
    ).strip().rstrip("/") or None
    r2_endpoint_url = (os.environ.get("R2_ENDPOINT_URL") or "").strip().rstrip("/") or None
    r2_bucket = (os.environ.get("R2_BUCKET") or "").strip() or None
    r2_access_key_id = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip() or None
    r2_secret_access_key = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip() or None
    teacher_schedule_access = (
        os.environ.get("TEACHER_SCHEDULE_ACCESS") or "admin"
    ).strip().lower()
    if teacher_schedule_access not in {"admin", "public"}:
        teacher_schedule_access = "admin"
    try:
        schedule_index_ttl_seconds = float(os.environ.get("SCHEDULE_INDEX_TTL_SECONDS", "300"))
    except ValueError:
        schedule_index_ttl_seconds = 300.0
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
        mongodb_max_pool_size=mongodb_max_pool_size,
        mongodb_min_pool_size=min(mongodb_min_pool_size, mongodb_max_pool_size),
        mongodb_max_connecting=mongodb_max_connecting,
        mongodb_wait_queue_timeout_ms=mongodb_wait_queue_timeout_ms,
        mongodb_server_selection_timeout_ms=mongodb_server_selection_timeout_ms,
        mongodb_connect_timeout_ms=mongodb_connect_timeout_ms,
        mongodb_socket_timeout_ms=mongodb_socket_timeout_ms,
        mongodb_max_idle_time_ms=mongodb_max_idle_time_ms,
        json_mirror=json_mirror,
        ingest_cooldown_hours=ingest_cooldown_hours,
        ingest_snapshot_ttl_hours=ingest_snapshot_ttl_hours,
        google_oauth_client_id=google_oauth_client_id,
        google_oauth_client_secret=google_oauth_client_secret,
        google_oauth_redirect_uri=google_oauth_redirect_uri,
        calendar_token_key=calendar_token_key,
        calendar_term_end_date=calendar_term_end_date,
        public_snapshot_backend=public_snapshot_backend,
        public_snapshot_dir=public_snapshot_dir,
        public_snapshot_base_url=public_snapshot_base_url,
        r2_endpoint_url=r2_endpoint_url,
        r2_bucket=r2_bucket,
        r2_access_key_id=r2_access_key_id,
        r2_secret_access_key=r2_secret_access_key,
        teacher_schedule_access=teacher_schedule_access,
        schedule_index_ttl_seconds=schedule_index_ttl_seconds,
        improvement_max_lecture_clashes=_non_negative_int("IMPROVEMENT_MAX_LECTURE_CLASHES", 1),
        improvement_max_tutorial_clashes=_non_negative_int("IMPROVEMENT_MAX_TUTORIAL_CLASHES", 1),
        improvement_max_practical_clashes=_non_negative_int("IMPROVEMENT_MAX_PRACTICAL_CLASHES", 0),
        improvement_pool_first_year_semesters=_truthy(
            os.environ.get("IMPROVEMENT_POOL_FIRST_YEAR_SEMESTERS", "1")
        ),
        improvement_max_plan_options=_positive_int("IMPROVEMENT_MAX_PLAN_OPTIONS", 20),
    )


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _non_negative_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default
