"""Crowd-sourced change requests.

Public POST to submit a proposal; admin GET/approve/reject to triage. The
public endpoint is rate-limited very tightly because anyone can hit it —
slowapi enforces per-(uid|ip) limits and storage.py has additional
queue-size guards. See /admin/change-requests for the moderation surface.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from server import storage
from server.auth import require_admin
from server.db.models import ClassEntry
from server.rate_limit import limiter

router = APIRouter(tags=["change-requests"])


class ChangeRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requester_batch: str = Field(min_length=2, max_length=16)
    scope: Literal["batch", "class"]
    kind: Literal["add", "edit", "delete"]
    day: str = Field(min_length=3, max_length=12)
    start_time: str = Field(min_length=1, max_length=16)
    entry: Optional[ClassEntry] = None


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=500)


def _refusal_to_http(exc: storage.ChangeRequestRefused) -> HTTPException:
    """Map storage refusal codes onto stable HTTP status codes."""
    status_map = {
        "duplicate": 409,
        "quota_user": 429,
        "quota_batch": 429,
        "quota_global": 429,
        "not_found": 404,
        "not_pending": 409,
        "empty_scope": 409,
        "empty_targets": 409,
        "scope_requires_lecture": 422,
    }
    status_code = status_map.get(exc.code, 400)
    return HTTPException(
        status_code=status_code,
        detail={"error": str(exc), "code": exc.code},
    )


# ── Public submit ────────────────────────────────────────────────────────
# Very strict: anonymous public endpoint, anyone with a network connection
# can hit it. Rate is per-(uid|ip).
@router.post("/change-requests", status_code=201)
@limiter.limit("5/minute;30/hour;100/day")
async def submit_change_request(
    request: Request,
    response: Response,
    body: ChangeRequestBody,
) -> dict[str, Any]:
    requester_id = request.headers.get("X-User-Id")
    entry_payload: dict[str, Any] | None = None
    if body.entry is not None:
        entry_payload = body.entry.model_dump(exclude_none=False)
    try:
        return await storage.create_change_request(
            requester_batch=body.requester_batch,
            scope=body.scope,
            kind=body.kind,
            day=body.day,
            start_time=body.start_time,
            entry=entry_payload,
            requester_id=requester_id,
        )
    except storage.ChangeRequestRefused as exc:
        raise _refusal_to_http(exc) from exc
    except storage.BatchNotFound as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "bad_batch"},
        ) from exc


# ── Admin moderation ─────────────────────────────────────────────────────
admin_router = APIRouter(
    prefix="/admin/change-requests",
    dependencies=[Depends(require_admin)],
    tags=["change-requests", "admin"],
)


@admin_router.get("")
async def list_admin_change_requests(
    status: Optional[Literal["pending", "approved", "rejected"]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    items = await storage.list_change_requests(status=status, limit=limit)
    return {"items": items, "count": len(items)}


@admin_router.post("/{request_id}/approve")
async def approve(request_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    note = body.note if body else None
    try:
        return await storage.approve_change_request(
            request_id, decision_note=note,
        )
    except storage.ChangeRequestRefused as exc:
        raise _refusal_to_http(exc) from exc


@admin_router.post("/{request_id}/reject")
async def reject(request_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    note = body.note if body else None
    try:
        return await storage.reject_change_request(
            request_id, decision_note=note,
        )
    except storage.ChangeRequestRefused as exc:
        raise _refusal_to_http(exc) from exc
