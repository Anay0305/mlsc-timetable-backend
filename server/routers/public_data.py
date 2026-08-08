"""Public canonical snapshot reads and authenticated rebuild controls."""

from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from server.auth import AdminPrincipal, require_admin
from server.public_snapshots import (
    MANIFEST_KEY,
    publish_all,
    publish_batch,
    read_object,
    validate_snapshot_path,
)

router = APIRouter(prefix="/public", tags=["public snapshots"])
admin_router = APIRouter(prefix="/admin/public-snapshots", tags=["admin"])


class RebuildBody(BaseModel):
    batch: Optional[str] = None


def _json_response(request: Request, body: bytes, *, immutable: bool) -> Response:
    etag = hashlib.sha256(body).hexdigest()
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})
    cache_control = (
        "public, max-age=31536000, immutable"
        if immutable
        else "public, max-age=30, s-maxage=30, stale-while-revalidate=300"
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": cache_control, "ETag": f'"{etag}"'},
    )


@router.get("/v1/manifest.json")
async def manifest(request: Request) -> Response:
    body = await read_object(MANIFEST_KEY)
    if body is None:
        raise HTTPException(status_code=404, detail={"error": "No public manifest", "code": "not_found"})
    return _json_response(request, body, immutable=False)


@router.get("/v1/timetables/{batch}/{filename}")
async def timetable_snapshot(batch: str, filename: str, request: Request) -> Response:
    try:
        _, key = validate_snapshot_path(batch, filename)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": str(exc), "code": "not_found"}) from exc
    body = await read_object(key)
    if body is None:
        raise HTTPException(status_code=404, detail={"error": "Snapshot not found", "code": "not_found"})
    return _json_response(request, body, immutable=True)


@admin_router.post("/rebuild")
async def rebuild_snapshots(
    body: RebuildBody,
    _: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    if body.batch:
        entry = await publish_batch(body.batch)
        return {"ok": True, "published": 1, "batch": body.batch.upper(), "entry": entry}
    result = await publish_all()
    return {"ok": not bool(result.get("failures")), **result}
