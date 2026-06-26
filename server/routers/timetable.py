"""GET /timetable/{batch} — per-batch schedule."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage

router = APIRouter()


@router.get("/timetable/{batch}")
async def get_timetable(batch: str) -> dict[str, object]:
    try:
        return await storage.read_timetable(batch)
    except storage.BatchNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "Batch not found", "code": "batch_not_found", "batch": exc.batch},
        )
