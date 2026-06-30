"""Admin endpoints: token-protected writes + spreadsheet ingest.

Auth model (v1): single shared bearer token from ADMIN_TOKEN env var. Replace
with real user sessions in Phase 3.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from server import storage
from server.auth import AdminPrincipal, require_admin
from server.ingest import parse_workbook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class AdminEmailBody(BaseModel):
    email: str
    display_name: Optional[str] = None


@router.get("/health")
async def admin_health() -> dict[str, object]:
    return {"ok": True, "scope": "admin"}


@router.get("/whoami")
async def whoami(principal: AdminPrincipal = Depends(require_admin)) -> dict[str, object]:
    """Confirm caller is admin; used by the panel to gate the UI."""
    return {
        "ok": True,
        "is_admin": True,
        "kind": principal.kind,
        "email": principal.email,
    }


@router.get("/users")
async def list_admin_users() -> dict[str, object]:
    items = await storage.list_admin_emails()
    return {"items": items, "count": len(items)}


@router.post("/users")
async def add_admin_user(
    body: AdminEmailBody,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    try:
        doc = await storage.add_admin_email(
            body.email,
            display_name=body.display_name,
            added_by=principal.label,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_email"},
        ) from exc
    return {"ok": True, **doc}


@router.delete("/users/{email}")
async def delete_admin_user(email: str) -> dict[str, object]:
    try:
        deleted = await storage.delete_admin_email(email)
    except ValueError as exc:
        # Either malformed email or env-managed entry — both are 400s with code.
        code = "env_managed" if "env-managed" in str(exc) else "invalid_email"
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": code},
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"no admin email {email!r}",
            "code": "not_found",
        })
    return {"ok": True, "email": email.strip().lower()}


@router.put("/timetable/{batch}")
async def put_timetable(batch: str, payload: dict) -> dict[str, object]:
    _validate_timetable_payload(payload)
    await storage.write_timetable(batch, payload)
    storage.maybe_git_commit(f"admin: replace timetable for {batch}")
    return {"ok": True, "batch": batch}


@router.put("/current")
async def put_current(payload: dict) -> dict[str, object]:
    if "label" not in payload or not isinstance(payload["label"], str):
        raise HTTPException(status_code=400, detail={
            "error": "Missing 'label' string",
            "code": "invalid_payload",
        })
    await storage.write_current({"label": payload["label"]})
    storage.maybe_git_commit(f"admin: update semester label to {payload['label']!r}")
    return {"ok": True, "label": payload["label"]}


@router.post("/ingest")
async def post_ingest(
    semester: str = Form(...),
    sheet: str = Form("all"),
    file: UploadFile = File(...),
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail={
            "error": "Upload must be an .xlsx/.xlsm file",
            "code": "invalid_file",
        })

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        summary = await parse_workbook(
            tmp_path,
            semester_label=semester,
            sheet=sheet,
            actor_kind=principal.kind,
            actor_email=principal.email,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    return {
        "ok": True,
        "semester": semester,
        "sheet": sheet,
        "filename": file.filename,
        **summary,
    }


# ── Dashboard observability ──────────────────────────────────────────────
@router.get("/stats")
async def get_admin_stats() -> dict[str, object]:
    """Aggregate counters used by the dashboard hero cards + donut."""
    return await storage.compute_admin_stats()


@router.get("/uploads")
async def list_uploads(
    limit: int = Query(default=50, ge=1, le=500),
    status: Optional[str] = Query(default=None),
) -> dict[str, object]:
    try:
        items = await storage.list_upload_attempts(limit=limit, status=status)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_status"},
        ) from exc
    return {"items": items, "count": len(items)}


@router.get("/uploads/latest")
async def latest_upload() -> dict[str, object]:
    doc = await storage.get_latest_upload_attempt()
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": "no uploads recorded yet",
            "code": "not_found",
        })
    return doc


@router.get("/uploads/{attempt_id}")
async def get_upload(attempt_id: str) -> dict[str, object]:
    doc = await storage.get_upload_attempt(attempt_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": f"no upload attempt {attempt_id!r}",
            "code": "not_found",
        })
    return doc


@router.post("/baselines/{key}")
async def post_baseline(key: str, payload: dict) -> dict[str, object]:
    """Create or replace the baseline for `key` (e.g. ``E1A``).

    Body: ``{"counts": {"Lecture": 12, "Tutorial": 4, "Practical": 3}}``
    """
    counts = payload.get("counts") if isinstance(payload, dict) else None
    if not isinstance(counts, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must include a 'counts' object mapping type → int",
            "code": "invalid_payload",
        })
    try:
        doc = await storage.write_baseline(key, counts)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_baseline"},
        ) from exc
    return {"ok": True, **doc}


@router.delete("/baselines/{key}")
async def delete_baseline(key: str) -> dict[str, object]:
    try:
        deleted = await storage.delete_baseline(key)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_key"},
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"no baseline for {key}",
            "code": "not_found",
        })
    return {"ok": True, "key": key.upper()}


@router.post("/contributors")
async def post_contributor(payload: dict) -> dict[str, object]:
    """Add (or upsert) a contributor by GitHub username.

    Body: ``{"username": "octocat", "display_name": "The Octocat"}`` —
    ``display_name`` is optional. The avatar is always fetched live from the
    GitHub REST API by the public ``GET /contributors`` endpoint.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    username = payload.get("username")
    if not isinstance(username, str) or not username.strip():
        raise HTTPException(status_code=400, detail={
            "error": "'username' must be a non-empty string",
            "code": "invalid_payload",
        })
    display_name = payload.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise HTTPException(status_code=400, detail={
            "error": "'display_name' must be a string when provided",
            "code": "invalid_payload",
        })
    try:
        doc = await storage.add_contributor(username, display_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_username"},
        ) from exc
    return {"ok": True, **doc}


@router.delete("/contributors/{username}")
async def delete_contributor(username: str) -> dict[str, object]:
    try:
        deleted = await storage.delete_contributor(username)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_username"},
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"no contributor for {username}",
            "code": "not_found",
        })
    return {"ok": True, "username": username.strip().lstrip("@")}


# ── Announcements ────────────────────────────────────────────────────────
@router.post("/announcements")
async def post_announcement(payload: dict) -> dict[str, object]:
    """Create a new announcement.

    Body: ``{title, body, severity?, posted_at?, link?}``. ``severity`` is
    one of ``info`` / ``warn`` / ``critical`` (default ``info``).
    ``posted_at`` is ISO-8601 (defaults to *now*).
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    try:
        doc = await storage.add_announcement(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_payload"},
        ) from exc
    return {"ok": True, **doc}


@router.delete("/announcements/{announcement_id}")
async def delete_announcement(announcement_id: str) -> dict[str, object]:
    deleted = await storage.delete_announcement(announcement_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"no announcement {announcement_id!r}",
            "code": "not_found",
        })
    return {"ok": True, "id": announcement_id}


# ── Exam dates ───────────────────────────────────────────────────────────
@router.post("/exam-dates")
async def post_exam_date(payload: dict) -> dict[str, object]:
    """Create a new exam-date entry.

    Body: ``{subject, code, date, slot?, type?, room?}`` where ``date`` is
    ``yyyy-mm-dd``.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    try:
        doc = await storage.add_exam_date(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_payload"},
        ) from exc
    return {"ok": True, **doc}


@router.delete("/exam-dates/{exam_id}")
async def delete_exam_date(exam_id: str) -> dict[str, object]:
    deleted = await storage.delete_exam_date(exam_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"no exam date {exam_id!r}",
            "code": "not_found",
        })
    return {"ok": True, "id": exam_id}


def _validate_timetable_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    classes = payload.get("classes")
    if not isinstance(classes, list):
        raise HTTPException(status_code=400, detail={
            "error": "'classes' must be a list",
            "code": "invalid_payload",
        })
    for index, entry in enumerate(classes):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail={
                "error": f"classes[{index}] must be an object",
                "code": "invalid_payload",
            })
        for required in ("day", "start_time", "end_time", "type"):
            if required not in entry:
                raise HTTPException(status_code=400, detail={
                    "error": f"classes[{index}] missing '{required}'",
                    "code": "invalid_payload",
                })
