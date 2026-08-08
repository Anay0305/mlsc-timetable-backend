"""Room and teacher timetables plus free/busy lookup.

Room numbers carry no privacy weight, so the room endpoints are public and
cacheable. Teacher codes do: ``BatchDoc.teacher_codes_visible`` hides them per
batch and :func:`server.storage.redact_teacher_codes` strips them from public
timetables. A teacher directory would route straight around that control, so it
stays admin-only until ``TEACHER_SCHEDULE_ACCESS=public`` says otherwise.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from server import availability as availability_lib
from server import schedule_index
from server.auth import require_admin
from server.config import get_settings

router = APIRouter(prefix="/schedule", tags=["schedule"])

_PUBLIC_CACHE = "public, max-age=300, s-maxage=900, stale-while-revalidate=86400"


def _teacher_is_public() -> bool:
    return get_settings().teacher_schedule_access == "public"


async def _authorize(kind: str, request: Request) -> None:
    """Teacher views need admin credentials unless opened up by config."""
    if kind != "teacher" or _teacher_is_public():
        return
    await require_admin(authorization=request.headers.get("authorization"))


def _cache(response: Response, kind: str) -> None:
    # Only room data is safe to hold at a shared edge; teacher responses are
    # authorized per caller when the directory is closed.
    if kind == "room" or _teacher_is_public():
        response.headers["Cache-Control"] = _PUBLIC_CACHE
    else:
        response.headers["Cache-Control"] = "private, no-store"


def _not_found(exc: availability_lib.UnknownResource) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": str(exc), "code": "unknown_resource", "kind": exc.kind, "name": exc.name},
    )


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": message, "code": "invalid_request"})


@router.get("/meta")
async def schedule_meta(response: Response) -> dict[str, Any]:
    """Slot grid, teaching days and directory sizes for building the UI."""
    index = await schedule_index.get_index()
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return {
        "semester": index.semester_label,
        "days": list(index.days),
        "slots": [{"start_time": start, "end_time": end} for start, end in index.slots],
        "room_count": len(index.by_room),
        "teacher_count": len(index.by_teacher),
        "teacher_access": get_settings().teacher_schedule_access,
        "built_at": index.built_at.isoformat(),
    }


@router.get("/{kind}s")
async def list_resources(
    kind: Literal["room", "teacher"],
    request: Request,
    response: Response,
    q: Optional[str] = Query(default=None, max_length=64),
) -> dict[str, Any]:
    """Directory of every known room or teacher."""
    await _authorize(kind, request)
    index = await schedule_index.get_index()
    items = availability_lib.list_resources(index, kind)
    if q:
        needle = q.strip().upper()
        items = [item for item in items if needle in item["name"].upper()]
    _cache(response, kind)
    return {"kind": kind, "count": len(items), "items": items}


@router.get("/{kind}s/{name}")
async def resource_schedule(
    kind: Literal["room", "teacher"],
    name: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """One room's or teacher's whole week."""
    await _authorize(kind, request)
    index = await schedule_index.get_index()
    try:
        payload = availability_lib.weekly_schedule(
            index, kind, name.strip(), include_teacher=kind == "teacher" or _teacher_is_public()
        )
    except availability_lib.UnknownResource as exc:
        raise _not_found(exc) from exc
    _cache(response, kind)
    return payload


@router.get("/{kind}s/{name}/free")
async def resource_free_windows(
    kind: Literal["room", "teacher"],
    name: str,
    request: Request,
    response: Response,
    day: str = Query(min_length=3, max_length=12),
    day_start: str = Query(default="08:00"),
    day_end: str = Query(default="18:50"),
) -> dict[str, Any]:
    """Contiguous gaps for one resource on one day."""
    await _authorize(kind, request)
    index = await schedule_index.get_index()
    try:
        payload = availability_lib.free_windows(
            index, kind, name.strip(), day=day, day_start=day_start, day_end=day_end
        )
    except availability_lib.UnknownResource as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    _cache(response, kind)
    return payload


@router.get("/availability/{kind}")
async def availability(
    kind: Literal["room", "teacher"],
    request: Request,
    response: Response,
    day: str = Query(min_length=3, max_length=12),
    at: Optional[str] = Query(default=None, description="HH:MM instant"),
    start: Optional[str] = Query(default=None, description="HH:MM window start"),
    end: Optional[str] = Query(default=None, description="HH:MM window end"),
    only: Optional[str] = Query(default=None, description="Comma-separated names to restrict to"),
) -> dict[str, Any]:
    """Who is free and who is busy at a given time."""
    await _authorize(kind, request)
    if at is None and start is None and end is None:
        raise _bad_request("provide either 'at' or both 'start' and 'end'")
    index = await schedule_index.get_index()
    names = [value.strip() for value in (only or "").split(",") if value.strip()] or None
    try:
        payload = availability_lib.availability(
            index,
            kind,
            day=day,
            at=at,
            start=start,
            end=end,
            only=names,
            include_teacher=kind == "teacher" or _teacher_is_public(),
        )
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    _cache(response, kind)
    return payload


admin_router = APIRouter(prefix="/admin/schedule", tags=["admin"])


@admin_router.post("/rebuild-index")
async def rebuild_index(_: Any = Depends(require_admin)) -> dict[str, Any]:
    """Force a rebuild after a bulk import, without waiting for the TTL."""
    schedule_index.invalidate()
    index = await schedule_index.get_index(force=True)
    return {
        "ok": True,
        "occupancies": len(index.occupancies),
        "rooms": len(index.by_room),
        "teachers": len(index.by_teacher),
        "built_at": index.built_at.isoformat(),
    }
