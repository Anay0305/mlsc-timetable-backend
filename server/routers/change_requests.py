"""Crowd-sourced change requests.

Public POST to submit a proposal; admin GET/approve/reject to triage. The
public endpoint is rate-limited very tightly because anyone can hit it —
slowapi enforces per-(uid|ip) limits and storage.py has additional
queue-size guards. See /admin/change-requests for the moderation surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from server import storage
from server.auth import require_admin
from server.db.models import ClassEntry, SubjectRequestDoc, SubjectDoc, ChangeRequestDoc
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
    requester_email: Optional[str] = None


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=500)


class SubjectRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requester_batch: str = Field(min_length=2, max_length=16)
    code: str = Field(min_length=2, max_length=24)
    name: str = Field(min_length=2, max_length=200)
    requester_email: Optional[str] = None


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
    requester_email = request.headers.get("X-User-Email") or body.requester_email
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
            requester_email=requester_email,
        )
    except storage.ChangeRequestRefused as exc:
        raise _refusal_to_http(exc) from exc
    except storage.BatchNotFound as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "bad_batch"},
        ) from exc


@router.post("/subject-requests", status_code=201)
@limiter.limit("5/minute;30/hour;100/day")
async def submit_subject_request(
    request: Request,
    response: Response,
    body: SubjectRequestBody,
) -> dict[str, Any]:
    """Submit a missing subject mapping for admin review."""
    code = "".join(ch for ch in body.code.strip().upper() if ch.isalnum())
    name = " ".join(body.name.split())
    if not code or not name:
        raise HTTPException(status_code=400, detail={"error": "code and name are required", "code": "invalid_subject_request"})
    existing = await SubjectRequestDoc.find_one(
        SubjectRequestDoc.code == code,
        SubjectRequestDoc.status == "pending",
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail={"error": "A subject request for this code is already pending", "code": "duplicate"})
    cat_match = await SubjectDoc.find_one(SubjectDoc.code == code)
    if cat_match is not None and cat_match.name.strip().lower() == name.strip().lower():
        raise HTTPException(
            status_code=409,
            detail={"error": f"Subject '{code}' is already mapped in catalog as '{cat_match.name}'", "code": "already_mapped"},
        )
    requester_email = request.headers.get("X-User-Email") or body.requester_email
    doc = SubjectRequestDoc(
        requester_id=request.headers.get("X-User-Id"),
        requester_email=requester_email,
        requester_batch=body.requester_batch.strip().upper(),
        code=code,
        name=name,
    )
    await doc.insert()
    return {"ok": True, "id": str(doc.id), "code": code, "name": name, "status": doc.status}


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
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items = await storage.list_change_requests(status=status, limit=limit, offset=offset)
    return {"items": items, "count": await storage.count_change_requests(status=status), "limit": limit, "offset": offset}


@admin_router.get("/subjects")
async def list_subject_requests(
    status: Optional[Literal["pending", "approved", "rejected"]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    query = SubjectRequestDoc.find_all(sort=[("created_at", -1)])
    if status:
        query = SubjectRequestDoc.find(SubjectRequestDoc.status == status, sort=[("created_at", -1)])
    rows = []
    async for doc in query.limit(limit):
        cat_match = await SubjectDoc.find_one(SubjectDoc.code == doc.code)
        rows.append({
            "id": str(doc.id), "requester_batch": doc.requester_batch,
            "requester_id": doc.requester_id, "requester_email": getattr(doc, "requester_email", None),
            "code": doc.code, "name": doc.name, "status": doc.status,
            "already_mapped": cat_match is not None,
            "existing_catalog_name": cat_match.name if cat_match else None,
            "created_at": doc.created_at.isoformat(),
        })
    return {"items": rows, "count": len(rows)}


@admin_router.post("/subjects/{request_id}/approve")
async def approve_subject_request(
    request_id: str,
    body: DecisionBody | None = None,
    principal=Depends(require_admin),
) -> dict[str, Any]:
    from beanie import PydanticObjectId
    try:
        doc = await SubjectRequestDoc.get(PydanticObjectId(request_id))
    except Exception:
        doc = None
    if doc is None or doc.status != "pending":
        raise HTTPException(status_code=404, detail={"error": "Pending subject request not found", "code": "not_found"})
    row = await storage.upsert_subject(
        code=doc.code, name=doc.name, source="admin", created_by=principal.label,
    )
    await doc.set({
        "status": "approved", "decided_by": principal.label,
        "decision_note": body.note if body else None,
        "decided_at": datetime.now(timezone.utc),
    })

    # Auto-reject pending change requests targeting this same subject code,
    # as the catalog update now handles the subject mapping globally across all batches.
    auto_rejected_count = 0
    pending_crs = await ChangeRequestDoc.find(ChangeRequestDoc.status == "pending").to_list()
    now_utc = datetime.now(timezone.utc)
    for cr in pending_crs:
        cr_code = (cr.entry or {}).get("code") or (cr.existing_entry or {}).get("code")
        if cr_code and "".join(ch for ch in str(cr_code).strip().upper() if ch.isalnum()) == doc.code:
            await cr.set({
                "status": "rejected",
                "decided_by": principal.label,
                "decided_at": now_utc,
                "decision_note": f"Auto-rejected: Subject '{doc.code}' was approved in global catalog ({doc.name})",
            })
            auto_rejected_count += 1

    return {
        "ok": True,
        "subject": row,
        "request_id": request_id,
        "auto_rejected_change_requests": auto_rejected_count,
    }


@admin_router.post("/subjects/{request_id}/reject")
async def reject_subject_request(
    request_id: str,
    body: DecisionBody | None = None,
    principal=Depends(require_admin),
) -> dict[str, Any]:
    from beanie import PydanticObjectId
    try:
        doc = await SubjectRequestDoc.get(PydanticObjectId(request_id))
    except Exception:
        doc = None
    if doc is None or doc.status != "pending":
        raise HTTPException(status_code=404, detail={"error": "Pending subject request not found", "code": "not_found"})
    await doc.set({
        "status": "rejected", "decided_by": principal.label,
        "decision_note": body.note if body else None,
        "decided_at": datetime.now(timezone.utc),
    })
    return {"ok": True, "request_id": request_id, "status": "rejected"}


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
