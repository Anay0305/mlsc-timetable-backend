"""GET /site-status — public maintenance / takedown state.

Deliberately unauthenticated and never 503s: every visitor polls this before
rendering, so it must answer even on a fresh database.
"""

from __future__ import annotations

from fastapi import APIRouter

from server import storage

router = APIRouter()


@router.get("/site-status")
async def get_site_status() -> dict[str, object]:
    return await storage.read_site_status()
