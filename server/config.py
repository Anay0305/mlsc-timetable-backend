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
    git_auto_commit: bool
    mongodb_url: str
    mongodb_db: str
    json_mirror: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", _REPO_ROOT / "data")).resolve()
    raw_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    cors_origins = tuple(origin.strip() for origin in raw_origins.split(",") if origin.strip())
    admin_token = os.environ.get("ADMIN_TOKEN") or None
    git_auto_commit = _truthy(os.environ.get("GIT_AUTO_COMMIT", "0"))
    mongodb_url = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
    mongodb_db = os.environ.get("MONGODB_DB", "mlsc_timetable")
    json_mirror = _truthy(os.environ.get("JSON_MIRROR", "0"))
    return Settings(
        data_dir=data_dir,
        cors_origins=cors_origins,
        admin_token=admin_token,
        git_auto_commit=git_auto_commit,
        mongodb_url=mongodb_url,
        mongodb_db=mongodb_db,
        json_mirror=json_mirror,
    )


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}
