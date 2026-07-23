"""Analytics endpoints: public logging and admin queries."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.auth import AdminPrincipal, require_admin
from server.db.models import DownloadEventDoc

router = APIRouter()
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


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
    start_date = now - timedelta(days=30)
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

    return {
        "total_downloads": total,
        "format_breakdown": formats,
        "top_batches": top_batches,
        "daily_trend": trend,
        "recent_downloads": recent,
    }
