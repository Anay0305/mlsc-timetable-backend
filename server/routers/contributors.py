"""Public read for community contributors.

The database stores only GitHub usernames; this endpoint enriches each one
with a live avatar URL fetched from the GitHub REST API. Responses are cached
in-process for ``CONTRIBUTORS_CACHE_TTL`` seconds (default 1 hour) so we stay
well below the unauthenticated rate limit of 60 requests / hour / IP.

Set ``GITHUB_TOKEN`` in the environment to raise the limit to 5000 req/hr.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter

from server import storage

logger = logging.getLogger(__name__)

router = APIRouter()

_GITHUB_API = "https://api.github.com"
_CACHE_TTL = int(os.environ.get("CONTRIBUTORS_CACHE_TTL", "3600"))
_HTTP_TIMEOUT = 6.0

# username -> (expires_at_epoch, payload-or-None)
_avatar_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = asyncio.Lock()


def _auth_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mlsc-timetable-backend",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_user(client: httpx.AsyncClient, username: str) -> dict[str, Any] | None:
    """Return the enriched payload for a username, or None if GitHub 404s."""
    try:
        resp = await client.get(f"{_GITHUB_API}/users/{username}", headers=_auth_headers())
    except httpx.HTTPError as exc:
        logger.warning("github user fetch failed for %s: %s", username, exc)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning(
            "github user fetch %s -> %s (%s)", username, resp.status_code, resp.text[:160]
        )
        return None
    data = resp.json()
    return {
        "id": data.get("id"),
        "login": data.get("login") or username,
        "avatar_url": data.get("avatar_url"),
        "html_url": data.get("html_url") or f"https://github.com/{username}",
        "name": data.get("name"),
    }


async def _resolve(username: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Cache-or-fetch wrapper."""
    now = time.time()
    cached = _avatar_cache.get(username)
    if cached and cached[0] > now:
        return cached[1]
    payload = await _fetch_user(client, username)
    async with _cache_lock:
        _avatar_cache[username] = (now + _CACHE_TTL, payload)
    return payload


@router.get("/contributors")
async def get_contributors() -> list[dict[str, Any]]:
    """Return ``[{id, login, avatar_url, html_url, name}, ...]`` sorted by login."""
    usernames = await storage.list_contributor_usernames()
    if not usernames:
        return []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        results = await asyncio.gather(*(_resolve(u, client) for u in usernames))
    return [r for r in results if r is not None]
