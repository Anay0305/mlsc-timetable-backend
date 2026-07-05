"""Public reads for class-count baselines.

A *baseline* is the admin-curated "right" number of classes (broken down by
type — Lecture / Tutorial / Practical / ...) that every batch within a
``{YEAR}{ALPHA}`` group should have.

Document keys carry a semester prefix so EVEN- and ODD-semester baselines
co-exist: ``E1A``, ``O3C``, etc.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage

router = APIRouter()


@router.get("/baselines")
async def list_baselines() -> list[dict[str, object]]:
    return await storage.list_baselines()


@router.get("/baselines/{key}")
async def get_baseline(key: str) -> dict[str, object]:
    try:
        return await storage.read_baseline(key)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_key"},
        ) from exc
