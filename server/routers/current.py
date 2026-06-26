"""GET /current — current semester label."""

from __future__ import annotations

from fastapi import APIRouter

from server import storage

router = APIRouter()


@router.get("/current")
async def get_current() -> dict[str, object]:
    return await storage.read_current()
