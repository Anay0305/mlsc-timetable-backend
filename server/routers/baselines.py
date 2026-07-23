"""Public reads for class-count baselines.

A *baseline* is the admin-curated "right" number of classes (broken down by
type — Lecture / Tutorial / Practical / ...) that every batch within a
``{YEAR}{ALPHA}`` group should have.

Document keys carry a semester prefix so EVEN- and ODD-semester baselines
co-exist: ``E1A``, ``O3C``, etc.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from server import storage

router = APIRouter()


@router.get("/baselines")
async def list_baselines(
    q: str | None = None,
    parity: str | None = None,
    year: str | None = None,
    stream: str | None = None,
    limit: int = Query(default=25, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    return {
        "items": await storage.list_baselines(
            q=q, parity=parity, year=year, stream=stream, limit=limit, offset=offset
        ),
        "count": await storage.count_baselines(
            q=q, parity=parity, year=year, stream=stream
        ),
        "limit": limit,
        "offset": offset,
    }


@router.get("/baselines/{key}")
async def get_baseline(key: str) -> dict[str, object]:
    try:
        return await storage.read_baseline(key)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_key"},
        ) from exc
