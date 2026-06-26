"""Admin endpoints: token-protected writes + spreadsheet ingest.

Auth model (v1): single shared bearer token from ADMIN_TOKEN env var. Replace
with real user sessions in Phase 3.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from server import storage
from server.auth import require_admin
from server.ingest import parse_workbook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("/health")
async def admin_health() -> dict[str, object]:
    return {"ok": True, "scope": "admin"}


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
        summary = await parse_workbook(tmp_path, semester_label=semester, sheet=sheet)
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"ok": True, "semester": semester, "sheet": sheet, **summary}


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
