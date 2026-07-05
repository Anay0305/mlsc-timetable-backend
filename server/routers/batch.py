"""GET /batch — list of all known batch codes."""

from __future__ import annotations

from fastapi import APIRouter

from server import storage

router = APIRouter()


@router.get("/batch")
async def get_batches() -> list[str]:
    return await storage.read_batch_list()
