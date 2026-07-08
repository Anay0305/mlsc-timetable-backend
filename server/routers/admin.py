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
from server.scheme_parser import baseline_key_for, parse_scheme_pdf
from server.calendar_parser import parse_calendar_pdf

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
    try:
        await storage.write_current({
            "label": payload["label"],
            "term_end_date": payload.get("term_end_date"),
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail={
            "error": str(e),
            "code": "invalid_semester_label",
        })
    storage.maybe_git_commit(f"admin: update semester label to {payload['label']!r}")
    result: dict[str, object] = {"ok": True, "label": payload["label"]}
    if payload.get("term_end_date"):
        result["term_end_date"] = payload["term_end_date"]
    return result


@router.post("/ingest")
async def post_ingest(
    semester: str = Form(...),
    sheet: str = Form("all"),
    file: UploadFile = File(...),
    force: bool = Form(False),
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail={
            "error": "Upload must be an .xlsx/.xlsm file",
            "code": "invalid_file",
        })

    # Cooldown gate — admin can pass force=true to override.
    gate = await storage.check_ingest_cooldown(force=force)
    if not gate.get("ok"):
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Ingest cooldown active",
                "code": "ingest_cooldown",
                **{k: v for k, v in gate.items() if k != "ok"},
            },
        )

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
        "forced": bool(force),
        **summary,
    }


@router.get("/ingest/cooldown")
async def get_ingest_cooldown() -> dict[str, object]:
    """Surface the current cooldown state to the admin UI so it can show
    "Next ingest available in 3h 42m" and grey out the upload button.
    """
    gate = await storage.check_ingest_cooldown()
    if gate.get("ok"):
        last = await storage.last_ingest_started_at()
        return {
            "ok": True,
            "active": False,
            "last_ingest_at": last.isoformat() if last else None,
        }
    return {"ok": True, "active": True, **{k: v for k, v in gate.items() if k != "ok"}}


@router.get("/ingest/rollback")
async def get_ingest_rollback_meta() -> dict[str, object]:
    """Return metadata for the latest snapshot, if any. The Fix page uses
    this to show the rollback button + countdown to expiry.
    """
    meta = await storage.get_ingest_snapshot_meta()
    if meta is None:
        return {"available": False}
    return {"available": True, **meta}


@router.post("/ingest/rollback")
async def post_ingest_rollback(
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    try:
        result = await storage.restore_ingest_snapshot()
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": str(exc), "code": "no_snapshot"},
        ) from exc
    logger.info("Rollback performed by %s: %r", principal.label, result)
    return result


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


@router.get("/uploads/{attempt_id}")
async def get_upload(attempt_id: str) -> dict[str, object]:
    doc = await storage.get_upload_attempt(attempt_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": f"no upload attempt {attempt_id!r}",
            "code": "not_found",
        })
    return doc


@router.post("/baselines/sync-counts")
async def sync_baseline_counts(
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Derive and back-fill ``counts`` for every baseline whose counts field
    is empty, using the stored course L / T / P columns.

    Safe to call repeatedly — baselines that already have explicit counts
    are not touched. Use this after a scheme-PDF upload on an older install
    where counts were seeded as ``{}`` before this endpoint existed.
    """
    result = await storage.backfill_baseline_counts()
    return {"ok": True, **result}


@router.post("/baselines/{key}")
async def post_baseline(key: str, payload: dict) -> dict[str, object]:
    """Create or replace the baseline for `key` (e.g. ``E1A``).

    Body: ``{"counts": {"Lecture": 12, "Tutorial": 4, "Practical": 3},
             "courses": [ {code, title, category, L, T, P, Cr}, ... ]}``

    ``courses`` is optional. When supplied it fully replaces the existing
    roster for the baseline (use ``POST /admin/scheme/apply`` for the
    merge-per-semester flow driven by a course-scheme PDF).
    """
    counts = payload.get("counts") if isinstance(payload, dict) else None
    if not isinstance(counts, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must include a 'counts' object mapping type → int",
            "code": "invalid_payload",
        })
    courses = payload.get("courses") if isinstance(payload, dict) else None
    scheme_source = payload.get("scheme_source") if isinstance(payload, dict) else None
    try:
        doc = await storage.write_baseline(
            key,
            counts,
            courses=courses,
            scheme_source=scheme_source if isinstance(scheme_source, str) else None,
        )
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


@router.post("/baselines/{key}/check")
async def check_baseline(
    key: str,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Run the doctor for a single baseline group against live timetables.

    Clears any existing open ``BASELINE_MISMATCH`` / ``BASELINE_MISSING``
    rows for the group and writes fresh ones. Returns a compact summary so
    the UI can show the result inline without a full page reload.
    """
    try:
        result = await storage.check_baseline_group(key)
    except storage.DataMissing as exc:
        raise HTTPException(status_code=404, detail={
            "error": str(exc), "code": "not_found",
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error": str(exc), "code": "invalid_key",
        }) from exc
    return {"ok": True, **result}


# ── Course-scheme PDF upload (populates baseline `courses`) ─────────────
_SCHEME_MAX_BYTES = 15 * 1024 * 1024  # 15 MB safety cap


async def _load_scheme_upload(file: UploadFile) -> Path:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail={
            "error": "Upload must be a .pdf file",
            "code": "invalid_file",
        })
    blob = await file.read()
    if len(blob) > _SCHEME_MAX_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": f"PDF exceeds {_SCHEME_MAX_BYTES // (1024 * 1024)} MB limit",
            "code": "file_too_large",
        })
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(blob)
        return Path(tmp.name)


# Branches whose year-1 curriculum is their own (no pool rotation). The
# scheme parser writes their year-1 semesters to their own baseline keys
# (E1X / O1X, E1G / O1G, …) just like it does for year 2+.
_INDEPENDENT_BRANCHES = {"X", "G", "J", "R"}
# Pool selector — accepted as "branch" input by the scheme endpoints when
# the admin is uploading the year-1 pool rotation instead of a real branch.
# A single ``POOL`` upload writes BOTH Pool A and Pool B baselines in one
# shot because they share the same course roster — Pool B just sees the
# semester parity swapped (Sem 1 canonical → E1B for Pool B, Sem 2 → O1B).
_POOL_SELECTOR = "POOL"


def _validate_branch(branch: str) -> str:
    """Accepts ``POOL`` (year-1 pool A+B upload) or a single-letter branch
    code A–Z, returning the canonical uppercase form."""
    cleaned = (branch or "").strip().upper()
    if cleaned == _POOL_SELECTOR:
        return cleaned
    if len(cleaned) != 1 or not cleaned.isalpha():
        raise HTTPException(status_code=400, detail={
            "error": "'branch' must be a single letter A–Z or POOL",
            "code": "invalid_branch",
        })
    return cleaned


async def _build_scheme_plan(
    result: dict,
    branch: str,
) -> list[dict]:
    """Turn the parser output into a per-semester apply plan, cross-referenced
    against existing baselines so the UI can show would-overwrite counts.

    Semantics by branch cohort:
      * ``POOL`` → only Sem 1 & 2, writing FOUR baselines: ``O1A`` + ``E1A``
        for Pool A, plus ``E1B`` + ``O1B`` for Pool B (parity-swapped copy
        of the same roster since Pool A and B share the year-1 curriculum,
        just staggered by one semester).
      * Independent branches (``X, G, J, R``) → all Sem 1–8, keys use the
        branch letter directly (``E1X``, ``O1X``, ``E2X``, …).
      * Pool-following branches (every other letter) → only Sem 3–8. The
        year-1 semesters are skipped because those students share the Pool
        A / Pool B baselines uploaded separately.
    """
    is_pool = branch == _POOL_SELECTOR
    is_independent = branch in _INDEPENDENT_BRANCHES

    plan: list[dict] = []
    existing_by_key: dict[str, dict] = {}
    for row in await storage.list_baselines():
        existing_by_key[row["key"]] = row

    # For a POOL upload we emit two baselines per parsed year-1 semester
    # (one for stream A with no swap, one for stream B with parity swap).
    # For every other cohort it's just the one branch letter with no swap.
    if is_pool:
        cohorts = [("A", False), ("B", True)]
    else:
        cohorts = [(branch, False)]

    for sem in result.get("semesters") or []:
        sem_num = int(sem["number"])

        # Cohort-specific semester filtering:
        #   * pool uploads: only sem 1 & 2 (year-1 rotation)
        #   * pool-following branches: skip sem 1 & 2 (owned by pool uploads)
        #   * independent branches: all semesters
        if is_pool:
            if sem_num not in (1, 2):
                continue
        elif not is_independent:
            if sem_num in (1, 2):
                continue

        # Flatten "OR" alternatives (e.g. Sem 8 electives) into a single
        # course roster so the doctor check accepts any code from any option.
        flat_courses: list[dict] = []
        for opt in sem.get("options") or []:
            flat_courses.extend(opt.get("courses") or [])

        for key_branch, pool_swap_year1 in cohorts:
            try:
                key = baseline_key_for(sem_num, key_branch, pool_swap_year1=pool_swap_year1)
            except ValueError:
                continue
            existing = existing_by_key.get(key)
            plan.append({
                "semester": sem_num,
                "keyline": sem["keyline"],
                "baseline_key": key,
                "year": sem["year"],
                "courses": flat_courses,
                "course_count": len(flat_courses),
                "option_count": len(sem.get("options") or []),
                "totals": [opt.get("totals") for opt in sem.get("options") or []],
                "existing_course_count": existing["course_count"] if existing else 0,
                "existing_counts": (existing or {}).get("counts") or {},
                "would_create": existing is None,
            })
    # Sort by branch letter first, then by the *student-facing* semester
    # (odd → sem 1, even → sem 2 within a year). This means Pool A's tabs
    # read `O1A`, `E1A` (Sem 1, Sem 2) and Pool B's read `O1B`, `E1B`
    # (Sem 1, Sem 2 for those students) even though the parser scanned
    # them in a different order.
    def _sort_key(row: dict) -> tuple:
        key = row["baseline_key"]
        parity = key[0]  # 'E' or 'O'
        try:
            year = int(key[1:-1])
        except ValueError:
            year = 0
        branch_letter = key[-1]
        student_sem = 2 * year - (1 if parity == "O" else 0)
        return (branch_letter, student_sem)

    plan.sort(key=_sort_key)
    return plan


@router.post("/scheme/preview")
async def post_scheme_preview(
    branch: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, object]:
    """Parse a course-scheme PDF and preview the baselines that would be
    written. Does not mutate any data.
    """
    branch = _validate_branch(branch)
    tmp_path = await _load_scheme_upload(file)
    try:
        result = parse_scheme_pdf(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    plan = await _build_scheme_plan(result, branch)
    return {
        "ok": True,
        "source": file.filename,
        "branch": branch,
        "semester_count": result.get("semester_count", 0),
        "plan": plan,
        "keyline_convention": result.get("keyline_convention", {}),
    }


@router.post("/scheme/apply")
async def post_scheme_apply(
    branch: str = Form(...),
    merge: bool = Form(False),
    file: UploadFile = File(...),
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Apply the parsed scheme to the baseline collection.

    For each detected semester (filtered by cohort — see ``_build_scheme_plan``)
    upserts a baseline keyed ``<E|O><year><branch>`` and writes its
    ``courses`` roster. Existing per-type ``counts`` on those baselines
    are preserved.

    ``merge=false`` (default) fully replaces the courses list per baseline;
    ``merge=true`` unions by course code with what's already stored (useful
    when a scheme is split across multiple PDFs).
    """
    branch = _validate_branch(branch)
    tmp_path = await _load_scheme_upload(file)
    try:
        result = parse_scheme_pdf(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    plan = await _build_scheme_plan(result, branch)
    written: list[dict] = []
    errors: list[dict] = []
    for entry in plan:
        key = entry["baseline_key"]
        try:
            doc = await storage.upsert_baseline_courses(
                key,
                entry["courses"],
                scheme_source=file.filename,
                merge=bool(merge),
            )
            written.append({
                "baseline_key": key,
                "semester": entry["semester"],
                "course_count": doc.get("course_count", 0),
                "created": entry["would_create"],
            })
        except ValueError as exc:
            errors.append({"baseline_key": key, "error": str(exc)})

    logger.info(
        "Scheme apply by %s: branch=%s wrote=%d errors=%d",
        principal.label, branch, len(written), len(errors),
    )
    return {
        "ok": True,
        "source": file.filename,
        "branch": branch,
        "merge": bool(merge),
        "written": written,
        "errors": errors,
    }


@router.post("/scheme/apply-plan")
async def post_scheme_apply_plan(
    payload: dict,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Apply a hand-edited scheme plan (JSON, no PDF re-parse).

    Body:
      ``{ plan: [{baseline_key, courses:[{code,title,category,L,T,P,Cr}], semester?}, ...],
          merge?: bool, source?: str }``

    Each entry's ``courses`` fully replaces the baseline's roster (or unions
    when ``merge=true``). Existing per-type counts on the baselines are
    preserved. Companion to ``POST /admin/scheme/apply`` which does the
    same thing but re-parses the source PDF.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    plan = payload.get("plan")
    if not isinstance(plan, list) or not plan:
        raise HTTPException(status_code=400, detail={
            "error": "'plan' must be a non-empty list",
            "code": "invalid_payload",
        })
    merge = bool(payload.get("merge"))
    source = payload.get("source")
    if source is not None and not isinstance(source, str):
        source = None

    written: list[dict] = []
    errors: list[dict] = []
    for idx, entry in enumerate(plan):
        if not isinstance(entry, dict):
            errors.append({"index": idx, "error": "entry must be an object"})
            continue
        key = str(entry.get("baseline_key") or "").strip().upper()
        if not key:
            errors.append({"index": idx, "error": "missing baseline_key"})
            continue
        courses = entry.get("courses") or []
        if not isinstance(courses, list):
            errors.append({"baseline_key": key, "error": "'courses' must be a list"})
            continue
        try:
            doc = await storage.upsert_baseline_courses(
                key,
                courses,
                scheme_source=source,
                merge=merge,
            )
            written.append({
                "baseline_key": key,
                "semester": entry.get("semester"),
                "course_count": doc.get("course_count", 0),
            })
        except ValueError as exc:
            errors.append({"baseline_key": key, "error": str(exc)})

    logger.info(
        "Scheme apply-plan by %s: entries=%d wrote=%d errors=%d",
        principal.label, len(plan), len(written), len(errors),
    )
    return {
        "ok": True,
        "source": source,
        "merge": merge,
        "written": written,
        "errors": errors,
    }


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


@router.post("/announcements/reset",
             dependencies=[Depends(require_admin)])
async def reset_announcements() -> dict[str, object]:
    """Delete all announcements and re-seed from the bundled defaults."""
    result = await storage.reset_announcements()
    return {"ok": True, **result}


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


@router.post("/exam-dates/reset",
             dependencies=[Depends(require_admin)])
async def reset_exam_dates() -> dict[str, object]:
    """Delete all exam dates and re-seed from the bundled defaults."""
    result = await storage.reset_exam_dates()
    return {"ok": True, **result}


# ── Calendar overrides ──────────────────────────────────────────────────
@router.post("/calendar-overrides")
async def post_calendar_override(payload: dict) -> dict[str, object]:
    """Create a calendar override.

    Body:
      ``{date, kind, reason?, follows_day?, scope, scope_values}``
    See ``CalendarOverrideDoc`` for the semantics.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    try:
        doc = await storage.add_calendar_override(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_payload"},
        ) from exc
    # Fan out to opted-in users asynchronously (best-effort)
    try:
        from server import calendar_storage
        await calendar_storage.enqueue_jobs_for_override(doc)
    except Exception:
        logger.exception("calendar fan-out failed for new override %s", doc.get("id"))
    return {"ok": True, **doc}


@router.put("/calendar-overrides/{override_id}")
async def put_calendar_override(override_id: str, payload: dict) -> dict[str, object]:
    """Replace an existing calendar override.

    All fields are re-validated as if the row were being created fresh —
    this keeps the update path simple and consistent with the create path.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    try:
        updated = await storage.update_calendar_override(override_id, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_payload"},
        ) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail={
            "error": f"no calendar override {override_id!r}",
            "code": "not_found",
        })
    # Fan out to opted-in users asynchronously (best-effort)
    try:
        from server import calendar_storage
        await calendar_storage.enqueue_jobs_for_override(updated)
    except Exception:
        logger.exception("calendar fan-out failed for updated override %s", override_id)
    return {"ok": True, **updated}


@router.delete("/calendar-overrides/{override_id}")
async def delete_calendar_override(override_id: str) -> dict[str, object]:
    deleted = await storage.delete_calendar_override(override_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"no calendar override {override_id!r}",
            "code": "not_found",
        })
    # Fan out: deleted override means timetable slots are restored;
    # trigger a resync for all opted-in users (best-effort)
    try:
        from server import calendar_storage
        await calendar_storage.enqueue_jobs_for_override({"id": override_id, "scope": "global"})
    except Exception:
        logger.exception("calendar fan-out failed for deleted override %s", override_id)
    return {"ok": True, "id": override_id}


@router.post("/calendar-overrides/reset",
             dependencies=[Depends(require_admin)])
async def reset_calendar_overrides() -> dict[str, object]:
    """Delete all calendar overrides and re-seed from the bundled defaults."""
    result = await storage.reset_calendar_overrides()
    return {"ok": True, **result}


# ── Calendar PDF parser ─────────────────────────────────────────────────
# Mirrors the course-scheme PDF flow: preview parses the PDF into a JSON
# preview (holidays, follow-day mappings, and any ambiguous cells the
# admin should double-check), and apply-plan writes the edited plan to
# the calendar_overrides collection.
_CALENDAR_MAX_BYTES = 8 * 1024 * 1024  # 8 MB safety cap


async def _load_calendar_upload(file: UploadFile) -> Path:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail={
            "error": "Upload must be a .pdf file",
            "code": "invalid_file",
        })
    blob = await file.read()
    if len(blob) > _CALENDAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": f"PDF exceeds {_CALENDAR_MAX_BYTES // (1024 * 1024)} MB limit",
            "code": "file_too_large",
        })
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(blob)
        return Path(tmp.name)


def _apply_scope_to_overrides(
    overrides: list[dict[str, Any]],
    scope: str,
    scope_values: list[str],
) -> list[dict[str, Any]]:
    """Stamp every override with the resolved scope so downstream storage
    validation doesn't need to backfill defaults."""
    out: list[dict[str, Any]] = []
    for o in overrides:
        merged = dict(o)
        merged["scope"] = scope
        merged["scope_values"] = scope_values if scope != "global" else []
        out.append(merged)
    return out


@router.post("/calendar/preview")
async def post_calendar_preview(
    file: UploadFile = File(...),
) -> dict[str, object]:
    """Parse an academic-calendar PDF and preview the calendar overrides
    that would be written. Does not mutate any data.

    Returns:
      { source, year_start, year_end, sem_kind,
        overrides: [ {date, kind, reason?, follows_day?}, ... ],
        warnings: [ {kind, date?, hint, ...}, ... ],
        holiday_legend, non_teaching, lieu_mappings, weeks }
    """
    tmp_path = await _load_calendar_upload(file)
    try:
        parsed = parse_calendar_pdf(tmp_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "invalid_pdf"},
        ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"ok": True, **parsed}


@router.post("/calendar/apply-plan")
async def post_calendar_apply_plan(
    payload: dict,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Apply a hand-edited calendar plan (JSON, no PDF re-parse).

    Body:
      { plan: [{date, kind, reason?, follows_day?}, ...],
        scope: "global" | "year" | "branch",
        scope_values: [str, ...],   # required when scope != "global"
        replace_range?: {start: "YYYY-MM-DD", end: "YYYY-MM-DD"},
        source?: str }

    When ``replace_range`` is present, every existing override whose date
    falls in that inclusive range AND matches the same scope+scope_values
    is deleted before the plan is applied — this keeps re-uploads
    idempotent (upload same calendar twice = same result, not doubled).
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={
            "error": "Body must be a JSON object",
            "code": "invalid_payload",
        })
    plan = payload.get("plan")
    if not isinstance(plan, list):
        raise HTTPException(status_code=400, detail={
            "error": "'plan' must be a list",
            "code": "invalid_payload",
        })
    scope = str(payload.get("scope") or "global").strip().lower()
    scope_values_raw = payload.get("scope_values") or []
    if not isinstance(scope_values_raw, list):
        raise HTTPException(status_code=400, detail={
            "error": "'scope_values' must be a list of strings",
            "code": "invalid_payload",
        })
    scope_values = [str(v).strip().upper() for v in scope_values_raw if str(v).strip()]

    # Optional idempotency window: wipe existing overrides in the calendar's
    # date range so a re-upload replaces rather than doubles.
    replace_range = payload.get("replace_range") or None
    deleted_count = 0
    if isinstance(replace_range, dict):
        start = str(replace_range.get("start") or "").strip()
        end = str(replace_range.get("end") or "").strip()
        if start and end:
            deleted_count = await storage.delete_calendar_overrides_in_range(
                start, end, scope=scope, scope_values=scope_values,
            )

    written: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stamped = _apply_scope_to_overrides(plan, scope, scope_values)
    for idx, entry in enumerate(stamped):
        try:
            doc = await storage.add_calendar_override(entry)
            written.append(doc)
        except ValueError as exc:
            errors.append({"index": idx, "error": str(exc), "row": entry})

    logger.info(
        "Calendar apply-plan by %s: entries=%d wrote=%d deleted=%d errors=%d",
        principal.label, len(plan), len(written), deleted_count, len(errors),
    )
    return {
        "ok": True,
        "source": payload.get("source"),
        "scope": scope,
        "scope_values": scope_values,
        "deleted": deleted_count,
        "written": written,
        "errors": errors,
    }


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


# ── Parsing errors / Fix tab ─────────────────────────────────────────────
@router.get("/errors")
async def list_errors(
    status: Optional[str] = Query(default=None),
    upload_id: Optional[str] = Query(default=None),
    error_type: Optional[str] = Query(default=None),
    batch_code: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, object]:
    items = await storage.list_parsing_errors(
        status=status,
        upload_id=upload_id,
        error_type=error_type,
        batch_code=batch_code,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.get("/errors/summary")
async def errors_summary(
    upload_id: Optional[str] = Query(default=None),
) -> dict[str, object]:
    return await storage.parsing_errors_summary(upload_id=upload_id)


class _ErrorActionBody(BaseModel):
    note: Optional[str] = None


@router.post("/errors/{error_id}/resolve")
async def resolve_error(
    error_id: str,
    body: Optional[_ErrorActionBody] = None,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    doc = await storage.update_parsing_error_status(
        error_id=error_id,
        new_status="resolved",
        resolved_by=principal.label,
        note=body.note if body else None,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": f"no error {error_id!r}",
            "code": "not_found",
        })
    return {"ok": True, **doc}


@router.post("/errors/{error_id}/ignore")
async def ignore_error(
    error_id: str,
    body: Optional[_ErrorActionBody] = None,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    doc = await storage.update_parsing_error_status(
        error_id=error_id,
        new_status="ignored",
        resolved_by=principal.label,
        note=body.note if body else None,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": f"no error {error_id!r}",
            "code": "not_found",
        })
    return {"ok": True, **doc}


@router.post("/errors/{error_id}/reopen")
async def reopen_error(error_id: str) -> dict[str, object]:
    doc = await storage.update_parsing_error_status(
        error_id=error_id, new_status="open"
    )
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": f"no error {error_id!r}",
            "code": "not_found",
        })
    return {"ok": True, **doc}


class _BulkErrorBody(BaseModel):
    ids: list[str]
    action: str  # "resolve" | "ignore" | "reopen"


@router.post("/errors/bulk")
async def bulk_errors(
    body: _BulkErrorBody,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    action_to_status = {"resolve": "resolved", "ignore": "ignored", "reopen": "open"}
    target = action_to_status.get(body.action)
    if target is None:
        raise HTTPException(status_code=400, detail={
            "error": f"invalid action {body.action!r}; expected one of {list(action_to_status)}",
            "code": "invalid_action",
        })
    count = await storage.bulk_update_parsing_errors(
        error_ids=body.ids or [],
        new_status=target,
        resolved_by=principal.label,
    )
    return {"ok": True, "updated": count, "status": target}


@router.post("/errors/backfill-baselines")
async def backfill_baseline_errors(
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Re-run the doctor against live timetables + baselines and replace all
    orphan ``BASELINE_MISMATCH`` / ``BASELINE_MISSING`` rows. Use after editing
    baselines to refresh the Fix page without re-ingesting the spreadsheet.
    """
    return await storage.backfill_baseline_errors()


@router.get("/timetables/{batch}")
async def get_timetable_for_admin(batch: str) -> dict[str, object]:
    """Read a batch's timetable in raw form — used by the Fix grid editor."""
    try:
        data = await storage.read_timetable(batch)
    except storage.BatchNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "error": str(exc), "code": "not_found",
        }) from exc
    return data


@router.patch("/timetables/{batch}")
async def patch_timetable(batch: str, payload: dict) -> dict[str, object]:
    """Partial update of a batch's timetable.

    Body shape:
      ``{"classes": [...]}``  full replacement of the class list, validated
      against the same schema used by PUT. Future ops (single-cell add/remove
      via index) can go here as a discriminator field.
    """
    _validate_timetable_payload(payload)
    try:
        current = await storage.read_timetable(batch)
    except storage.BatchNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "error": str(exc), "code": "not_found",
        }) from exc

    merged = {
        "batch": current.get("batch", batch),
        "semester": current.get("semester") or {},
        "classes": payload.get("classes", []),
    }
    if "label" in payload.get("semester", {}) and isinstance(payload["semester"]["label"], str):
        merged["semester"] = {"label": payload["semester"]["label"]}
    await storage.write_timetable(batch, merged)
    storage.maybe_git_commit(f"admin: patch timetable for {batch}")
    return {"ok": True, "batch": batch, "classes": len(merged["classes"])}


# ── Subject catalog (DB-backed replacement for assets/subjects.json) ────

class _SubjectBody(BaseModel):
    code: str
    name: str
    aliases: list[str] | None = None
    note: str | None = None


class _SubjectPatchBody(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    note: str | None = None


class _SubjectBulkBody(BaseModel):
    items: list[dict]
    resolve_errors: bool = True  # auto-bulk-resolve matching SUBJECT_NOT_IN_CATALOG rows


@router.get("/subjects")
async def list_subjects(
    q: str | None = None,
    source: str | None = None,
    limit: int = 500,
) -> dict[str, object]:
    rows = await storage.list_subjects(q=q, source=source, limit=limit)
    return {"items": rows, "count": len(rows)}


@router.post("/subjects")
async def create_subject(
    body: _SubjectBody,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    try:
        row = await storage.upsert_subject(
            code=body.code,
            name=body.name,
            aliases=body.aliases,
            source="admin",
            created_by=principal.label,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error": str(exc), "code": "invalid_payload",
        }) from exc
    # Auto-clear any open SUBJECT_NOT_IN_CATALOG errors for the same code —
    # the whole point of adding a row is to make those parsing errors go
    # away. Best-effort; we don't fail the create if this part errors.
    resolved = 0
    try:
        norm_code = row["code"]
        candidates = await storage.list_parsing_errors(
            status="open", error_type="SUBJECT_NOT_IN_CATALOG", limit=10000,
        )
        ids = [
            r["id"] for r in candidates
            if (
                (r.get("code") or "").upper().startswith(norm_code)
                or norm_code in str(r.get("context") or {}).upper()
                or (r.get("message") and norm_code in r["message"].upper())
            )
        ]
        if ids:
            resolved = await storage.bulk_update_parsing_errors(
                error_ids=ids, new_status="resolved", resolved_by=principal.label,
            )
    except Exception:
        # logging happens inside storage; don't block the response
        resolved = 0
    return {"ok": True, "subject": row, "errors_resolved": resolved}


@router.patch("/subjects/{code}")
async def patch_subject(
    code: str,
    body: _SubjectPatchBody,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    if body.name is None and body.aliases is None and body.note is None:
        raise HTTPException(status_code=400, detail={
            "error": "no fields to update",
            "code": "empty_patch",
        })
    # Fetch existing row to keep name/aliases when only one field is patched.
    rows = await storage.list_subjects(q=code, limit=10)
    existing = next((r for r in rows if r["code"].upper() == code.strip().upper()
                    or r["code"].upper() == storage._normalize_subject_code(code)), None)
    if existing is None:
        raise HTTPException(status_code=404, detail={
            "error": f"subject {code!r} not found",
            "code": "not_found",
        })
    try:
        row = await storage.upsert_subject(
            code=existing["code"],
            name=body.name if body.name is not None else existing["name"],
            aliases=body.aliases if body.aliases is not None else existing.get("aliases"),
            source="admin",
            created_by=principal.label,
            note=body.note if body.note is not None else existing.get("note"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error": str(exc), "code": "invalid_payload",
        }) from exc
    return {"ok": True, "subject": row}


@router.delete("/subjects/{code}")
async def delete_subject(
    code: str,
    force: bool = False,
) -> dict[str, object]:
    try:
        ok = await storage.delete_subject(code, force=force)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={
            "error": str(exc), "code": "seed_protected",
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error": str(exc), "code": "invalid_code",
        }) from exc
    if not ok:
        raise HTTPException(status_code=404, detail={
            "error": f"subject {code!r} not found",
            "code": "not_found",
        })
    return {"ok": True, "code": code}


@router.post("/subjects/bulk")
async def bulk_subjects(
    body: _SubjectBulkBody,
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    summary = await storage.bulk_upsert_subjects(body.items, created_by=principal.label)
    return {"ok": True, **summary}


@router.post("/subjects/backfill-timetables")
async def backfill_timetables_against_catalog(
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """One-shot: re-run the catalog-strip rule over every TimetableDoc so
    older data drops redundant subject names. Safe to call any time.
    """
    summary = await storage.normalize_all_timetables()
    return {"ok": True, **summary}
