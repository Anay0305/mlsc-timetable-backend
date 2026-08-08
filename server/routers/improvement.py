"""Improvement (course re-take) planning endpoints.

Students repeating an earlier course currently open every junior batch's
timetable by hand to find one whose classes they can actually attend. These
routes do that search: ``/improvement/courses`` lists what is reachable from
their semester, and ``/improvement/plan`` ranks the batches per course and
proposes combined timetables.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from server import improvement as improvement_lib
from server import schedule_index, storage
from server.config import get_settings
from server.curriculum_projection import resolve_curriculum_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/improvement", tags=["improvement"])


class PlanBody(BaseModel):
    batch: str = Field(min_length=2, max_length=32)
    codes: list[str] = Field(min_length=1, max_length=8)


def _bad_request(message: str, code: str = "invalid_request") -> HTTPException:
    return HTTPException(status_code=400, detail={"error": message, "code": code})


def _teacher_is_public() -> bool:
    return get_settings().teacher_schedule_access == "public"


async def _student_context(batch: str) -> tuple[str, int, list[Any]]:
    """Resolve the student's batch, semester and current classes."""
    code = "".join(ch for ch in str(batch or "").strip().upper() if ch.isalnum())
    if not code:
        raise _bad_request("batch is required")

    try:
        payload = await storage.read_timetable(code, include_unavailable=True)
    except storage.BatchNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Unknown batch {code}", "code": "unknown_batch"},
        ) from exc

    label = ""
    semester = payload.get("semester")
    if isinstance(semester, dict):
        label = str(semester.get("label") or "")
    try:
        context = resolve_curriculum_context(code, label)
    except ValueError as exc:
        raise _bad_request(
            f"Cannot resolve which semester {code} is in; the term label is {label!r}",
            code="unresolved_semester",
        ) from exc
    return code, context.semester, list(payload.get("classes") or [])


async def _apply_personal(user_id: str | None, batch: str, classes: list[Any]) -> list[Any]:
    """Fold the caller's saved elective picks in, when they are signed in.

    A resolved elective narrows a slot from "worst case among four options" to
    the one class they actually attend, which turns rejected offerings back
    into viable ones.
    """
    if not user_id:
        return classes
    try:
        from server.db.models import PersonalCustomizationDoc
        from server.personal_timetable import apply_operations

        doc = await PersonalCustomizationDoc.find_one(
            PersonalCustomizationDoc.user_id == user_id,
            PersonalCustomizationDoc.batch == batch,
        )
        if doc is None or not doc.operations:
            return classes
        merged, _ = apply_operations(classes, doc.operations)
        return merged
    except Exception:
        logger.exception("improvement: personal customization merge failed")
        return classes


async def _optional_user_id(request: Request) -> str | None:
    """Best-effort identity. Never rejects — improvement works for guests."""
    from server import clerk_jwt

    authorization = request.headers.get("authorization") or ""
    if authorization.startswith("Bearer ") and clerk_jwt.is_clerk_configured():
        try:
            claims = clerk_jwt.verify_clerk_jwt(authorization.removeprefix("Bearer ").strip())
            sub = claims.get("sub")
            if sub:
                return str(sub)
        except clerk_jwt.ClerkJWTError:
            return None
    return None


@router.get("/courses")
async def list_courses(
    response: Response,
    batch: str = Query(min_length=2, max_length=32),
) -> dict[str, Any]:
    """Courses a student in ``batch`` is allowed to sit for improvement."""
    code, semester, _ = await _student_context(batch)
    settings = get_settings()
    index = await schedule_index.get_index(settings)
    courses = improvement_lib.available_courses(
        index,
        student_batch=code,
        student_semester=semester,
        pool_first_year=settings.improvement_pool_first_year_semesters,
    )
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=3600"
    return {
        "batch": code,
        "semester": semester,
        "semester_label": index.semester_label,
        "first_year_semesters_pooled": settings.improvement_pool_first_year_semesters,
        "count": len(courses),
        "courses": courses,
    }


@router.post("/plan")
async def plan(body: PlanBody, request: Request, response: Response) -> dict[str, Any]:
    """Rank junior batches per course and propose combined timetables."""
    code, semester, classes = await _student_context(body.batch)
    if semester <= 1:
        raise _bad_request(
            "There is no earlier semester to take an improvement course from",
            code="no_earlier_semester",
        )

    user_id = await _optional_user_id(request)
    classes = await _apply_personal(user_id, code, classes)

    settings = get_settings()
    index = await schedule_index.get_index(settings)
    try:
        result = improvement_lib.plan_improvements(
            index,
            student_batch=code,
            student_semester=semester,
            student_classes=classes,
            codes=body.codes,
            settings=settings,
            include_teacher=_teacher_is_public(),
        )
    except improvement_lib.ImprovementError as exc:
        raise _bad_request(str(exc)) from exc

    result["personalized"] = bool(user_id)
    # Plans depend on the caller's own saved electives, so never share them.
    response.headers["Cache-Control"] = "private, no-store"
    return result
