"""Public reads for announcements and exam dates.

Both endpoints are Mongo-backed (``AnnouncementDoc`` / ``ExamDateDoc``). On
the first read against an empty collection the storage layer seeds the
canonical JSON from ``assets/`` so the public sidebar feeds keep working
during the JSON → Mongo cutover with no manual reimport.

``GET /exam-dates`` accepts an optional ``?batch=<code>`` query: when set,
results are filtered to exams whose ``target_year`` matches the batch's
year (or is null = "all years") AND whose subject code appears in that
batch's canonical timetable (incl. elective options).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query

from server import storage

router = APIRouter()


@router.get("/announcements")
async def get_announcements() -> list[dict[str, Any]]:
    """Latest-first list of announcements."""
    return await storage.list_announcements()


@router.get("/exam-dates")
async def get_exam_dates(
    batch: Optional[str] = Query(
        default=None,
        description="Optional batch code; filters exams to that batch's year + subjects.",
    ),
) -> list[dict[str, Any]]:
    """Upcoming exam dates, earliest first. Filtered by `batch` when supplied."""
    return await storage.list_exam_dates(batch=batch)
