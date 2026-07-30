"""Analytics endpoints: public logging and admin queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.auth import AdminPrincipal, require_admin
from server.db.models import (
    CalendarConnectionDoc,
    DownloadEventDoc,
    PersonalCustomizationDoc,
    UserDoc,
)

router = APIRouter()
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _empty_daily_user_trend(start_date: datetime, days: int = 30) -> dict[str, int]:
    return {
        (start_date + timedelta(days=offset)).strftime("%Y-%m-%d"): 0
        for offset in range(days)
    }


async def _get_user_analytics(now: datetime) -> dict[str, object]:
    """Aggregate privacy-safe analytics for unique timetable identities."""
    active_24h_at = now - timedelta(hours=24)
    active_7d_at = now - timedelta(days=7)
    active_30d_at = now - timedelta(days=30)
    registration_start = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=29)
    )

    total = await UserDoc.find({}).count()
    active_24h = await UserDoc.find(
        {"last_seen_at": {"$gte": active_24h_at}}
    ).count()
    active_7d = await UserDoc.find(
        {"last_seen_at": {"$gte": active_7d_at}}
    ).count()
    active_30d = await UserDoc.find(
        {"last_seen_at": {"$gte": active_30d_at}}
    ).count()
    new_30d = await UserDoc.find(
        {"created_at": {"$gte": active_30d_at}}
    ).count()
    with_default_batch = await UserDoc.find(
        {"default_batch": {"$nin": [None, ""]}}
    ).count()

    top_batches_raw = await UserDoc.aggregate([
        {
            "$match": {"default_batch": {"$nin": [None, ""]}}
        },
        {"$group": {"_id": "$default_batch", "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
        {"$limit": 8},
    ]).to_list()

    registration_raw = await UserDoc.aggregate([
        {
            "$match": {"created_at": {"$gte": registration_start}}
        },
        {
            "$group": {
                "_id": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$created_at",
                    }
                },
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]).to_list()
    registration_trend = _empty_daily_user_trend(registration_start)
    for row in registration_raw:
        date = row.get("_id")
        if date in registration_trend:
            registration_trend[date] = row.get("count", 0)

    personalization_raw = await PersonalCustomizationDoc.aggregate([
        {"$group": {"_id": "$user_id"}},
        {"$count": "count"},
    ]).to_list()
    with_personalization = personalization_raw[0]["count"] if personalization_raw else 0

    calendar_connected = await CalendarConnectionDoc.find({}).count()
    calendar_enabled = await CalendarConnectionDoc.find(
        {"enabled": True}
    ).count()

    return {
        "total": total,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "new_30d": new_30d,
        "with_default_batch": with_default_batch,
        "with_personalization": with_personalization,
        "calendar_connected": calendar_connected,
        "calendar_enabled": calendar_enabled,
        "top_batches": [
            {"batch": row["_id"], "count": row["count"]}
            for row in top_batches_raw
        ],
        "registration_trend": [
            {"date": date, "count": count}
            for date, count in sorted(registration_trend.items())
        ],
        "scope": "Unique timetable identities that opened a timetable",
        "window_timezone": "UTC",
    }


class DownloadEventBody(BaseModel):
    format: Literal["png", "pdf"]
    batch: str
    aspect: Optional[str] = None


@router.post("/analytics/download")
async def log_download(body: DownloadEventBody) -> dict[str, object]:
    """Log a public download event (format, batch, aspect)."""
    event = DownloadEventDoc(
        format=body.format,
        batch=body.batch,
        aspect=body.aspect,
    )
    await event.insert()
    return {"ok": True}


@admin_router.get("/analytics")
async def get_analytics(
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Retrieve summarized download event statistics for the admin dashboard."""
    # 1. Total count
    total = await DownloadEventDoc.count()

    # 2. Format breakdown
    format_pipeline = [
        {"$group": {"_id": "$format", "count": {"$sum": 1}}}
    ]
    formats_raw = await DownloadEventDoc.aggregate(format_pipeline).to_list()
    formats = {item["_id"]: item["count"] for item in formats_raw}
    for fmt in ["png", "pdf"]:
        if fmt not in formats:
            formats[fmt] = 0

    # 3. Batch breakdown (Top 10)
    batch_pipeline = [
        {"$group": {"_id": "$batch", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    batches_raw = await DownloadEventDoc.aggregate(batch_pipeline).to_list()
    top_batches = [{"batch": item["_id"], "count": item["count"]} for item in batches_raw]

    # 4. Daily trend over last 30 days
    now = datetime.now(timezone.utc)
    start_date = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=29)
    )
    trend_pipeline = [
        {"$match": {"created_at": {"$gte": start_date}}},
        {
            "$group": {
                "_id": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                    "format": "$format"
                },
                "count": {"$sum": 1}
            }
        },
        {"$sort": {"_id.date": 1}}
    ]
    trend_raw = await DownloadEventDoc.aggregate(trend_pipeline).to_list()

    # Format the daily trend as a dictionary of date -> {png_count, pdf_count}
    trend_dict = {}
    for i in range(30):
        d_str = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        trend_dict[d_str] = {"png": 0, "pdf": 0}

    for item in trend_raw:
        date_str = item["_id"]["date"]
        fmt = item["_id"]["format"]
        count = item["count"]
        if date_str in trend_dict:
            trend_dict[date_str][fmt] = count

    trend = [{"date": k, "png": v["png"], "pdf": v["pdf"]} for k, v in sorted(trend_dict.items())]

    # 5. Recent downloads (last 20 events)
    recent_docs = await DownloadEventDoc.find_all().sort("-created_at").limit(20).to_list()
    recent = [
        {
            "format": doc.format,
            "batch": doc.batch,
            "aspect": doc.aspect,
            "created_at": doc.created_at.isoformat(),
        }
        for doc in recent_docs
    ]

    user_analytics = await _get_user_analytics(now)

    return {
        "total_downloads": total,
        "format_breakdown": formats,
        "top_batches": top_batches,
        "daily_trend": trend,
        "recent_downloads": recent,
        "users": user_analytics,
    }
