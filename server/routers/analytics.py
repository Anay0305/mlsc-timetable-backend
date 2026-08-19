"""Analytics endpoints: public logging and admin queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.auth import AdminPrincipal, require_admin
from server.storage import _derive_batch_meta
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


def batch_year_filter(year: Optional[int], field: str = "default_batch") -> dict:
    """Mongo filter selecting batch codes belonging to ``year``.

    `_derive_batch_meta` reads the year off the leading digit
    (``^(?P<year>\\d)(?P<section>[A-Z]+)``), and its three special-cased codes
    — 2UOQ, 2UNSW, 2TCD — happen to start with their own year digit too. So
    this regex and that function agree on every code in the corpus, which lets
    the filtering happen in the database instead of in Python.
    """
    if year is None:
        return {}
    return {field: {"$regex": f"^{year}[A-Z]", "$options": "i"}}


async def _scoped_user_ids_count(collection_model, user_scope: dict) -> int:
    """Distinct users in ``collection_model`` whose UserDoc matches ``user_scope``.

    Only used when a year filter is on. Unscoped counts stay on the cheaper
    direct aggregation below.
    """
    rows = await UserDoc.aggregate([
        {"$match": user_scope},
        {
            "$lookup": {
                "from": collection_model.Settings.name,
                "localField": "user_id",
                "foreignField": "user_id",
                "as": "matched",
            }
        },
        {"$match": {"matched.0": {"$exists": True}}},
        {"$count": "count"},
    ]).to_list()
    return rows[0]["count"] if rows else 0


async def _get_user_analytics(
    now: datetime, year: Optional[int] = None
) -> dict[str, object]:
    """Aggregate privacy-safe analytics for unique timetable identities.

    ``year`` narrows every figure to users whose saved batch belongs to that
    year group. ``by_year`` is the one exception and always covers everyone.
    """
    active_24h_at = now - timedelta(hours=24)
    active_7d_at = now - timedelta(days=7)
    active_30d_at = now - timedelta(days=30)
    registration_start = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=29)
    )

    # Every user query below is intersected with this. Empty when the page is
    # showing all years, so the unfiltered numbers are unchanged.
    scope = batch_year_filter(year)
    scoped = bool(scope)

    def within(*extra: dict) -> dict:
        """Intersect the year scope with additional clauses.

        Combined with ``$and`` rather than by merging dicts: several callers
        below also constrain ``default_batch``, and a plain ``update()`` let
        that clause overwrite the scope's regex — which silently returned
        every year's numbers under a year filter.
        """
        clauses = [dict(clause) for clause in (scope, *extra) if clause]
        if not clauses:
            return {}
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    total = await UserDoc.find(within()).count()
    active_24h = await UserDoc.find(
        within({"last_seen_at": {"$gte": active_24h_at}})
    ).count()
    active_7d = await UserDoc.find(
        within({"last_seen_at": {"$gte": active_7d_at}})
    ).count()
    active_30d = await UserDoc.find(
        within({"last_seen_at": {"$gte": active_30d_at}})
    ).count()
    new_30d = await UserDoc.find(
        within({"created_at": {"$gte": active_30d_at}})
    ).count()
    with_default_batch = await UserDoc.find(
        within({"default_batch": {"$nin": [None, ""]}})
    ).count()

    top_batches_raw = await UserDoc.aggregate([
        {
            "$match": within({"default_batch": {"$nin": [None, ""]}})
        },
        {"$group": {"_id": "$default_batch", "count": {"$sum": 1}}},
        {"$sort": {"count": -1, "_id": 1}},
        {"$limit": 8},
    ]).to_list()

    # Year rollup. Deliberately NOT scoped — this is what the page's year
    # switcher is built from, so it has to keep describing every year even
    # while the rest of the response is narrowed to one of them.
    #
    # Grouped per batch first and folded in Python so the year is derived by
    # the same rule the rest of the codebase uses, rather than by a `$substr`
    # on the code that could quietly disagree with it.
    per_batch_raw = await UserDoc.aggregate([
        {"$match": {"default_batch": {"$nin": [None, ""]}}},
        {
            "$group": {
                "_id": "$default_batch",
                "users": {"$sum": 1},
                "active_24h": {
                    "$sum": {"$cond": [{"$gte": ["$last_seen_at", active_24h_at]}, 1, 0]}
                },
                "active_7d": {
                    "$sum": {"$cond": [{"$gte": ["$last_seen_at", active_7d_at]}, 1, 0]}
                },
            }
        },
    ]).to_list()

    buckets: dict[Optional[int], dict[str, int]] = {}
    for row in per_batch_raw:
        # Named `bucket_year` rather than `year` — the parameter of the same
        # name is still needed below.
        bucket_year = _derive_batch_meta(str(row.get("_id") or "")).get("year")
        bucket = buckets.setdefault(
            bucket_year, {"users": 0, "active_24h": 0, "active_7d": 0, "batches": 0}
        )
        bucket["users"] += row.get("users", 0)
        bucket["active_24h"] += row.get("active_24h", 0)
        bucket["active_7d"] += row.get("active_7d", 0)
        bucket["batches"] += 1

    # Known years first, then anything whose code we could not read.
    by_year = [
        {"year": known, **buckets[known]}
        for known in sorted(y for y in buckets if y is not None)
    ]
    if None in buckets:
        by_year.append({"year": None, **buckets[None]})

    registration_raw = await UserDoc.aggregate([
        {
            "$match": within({"created_at": {"$gte": registration_start}})
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

    # These three live in their own collections keyed by user_id, so scoping
    # them to a year means joining back to users. Unscoped keeps the cheap
    # direct count it always had.
    if scoped:
        with_personalization = await _scoped_user_ids_count(
            PersonalCustomizationDoc, scope
        )
        calendar_connected = await _scoped_user_ids_count(
            CalendarConnectionDoc, scope
        )
        enabled_rows = await UserDoc.aggregate([
            {"$match": scope},
            {
                "$lookup": {
                    "from": CalendarConnectionDoc.Settings.name,
                    "localField": "user_id",
                    "foreignField": "user_id",
                    "as": "matched",
                }
            },
            {"$match": {"matched": {"$elemMatch": {"enabled": True}}}},
            {"$count": "count"},
        ]).to_list()
        calendar_enabled = enabled_rows[0]["count"] if enabled_rows else 0
    else:
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
        "by_year": by_year,
        # Everyone who has never saved a batch, so the year rollup and this
        # add up to `total` instead of silently losing people. Zero under a
        # year filter, since selecting a year selects people who have one.
        "without_batch": max(total - with_default_batch, 0),
        "year": year,
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
    year: Optional[int] = Query(
        None,
        ge=1,
        le=9,
        description="Restrict every figure to one year group (1-4). Omit for all years.",
    ),
    principal: AdminPrincipal = Depends(require_admin),
) -> dict[str, object]:
    """Summarized download + user statistics, optionally scoped to one year.

    Download events carry the batch they were exported for, so the same year
    filter narrows this half too rather than leaving the exports panel showing
    institute-wide totals beside per-year user counts.
    """
    # Downloads store the batch on the event itself, so no join is needed.
    dl_scope = batch_year_filter(year, field="batch")
    match_stage = [{"$match": dl_scope}] if dl_scope else []

    # 1. Total count
    total = await DownloadEventDoc.find(dl_scope).count()

    # 2. Format breakdown
    format_pipeline = [
        *match_stage,
        {"$group": {"_id": "$format", "count": {"$sum": 1}}}
    ]
    formats_raw = await DownloadEventDoc.aggregate(format_pipeline).to_list()
    formats = {item["_id"]: item["count"] for item in formats_raw}
    for fmt in ["png", "pdf"]:
        if fmt not in formats:
            formats[fmt] = 0

    # 3. Batch breakdown (Top 10)
    batch_pipeline = [
        *match_stage,
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
        {"$match": {**dl_scope, "created_at": {"$gte": start_date}}},
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
    recent_docs = await DownloadEventDoc.find(dl_scope).sort("-created_at").limit(20).to_list()
    recent = [
        {
            "format": doc.format,
            "batch": doc.batch,
            "aspect": doc.aspect,
            "created_at": doc.created_at.isoformat(),
        }
        for doc in recent_docs
    ]

    user_analytics = await _get_user_analytics(now, year=year)

    return {
        "total_downloads": total,
        "format_breakdown": formats,
        "top_batches": top_batches,
        "daily_trend": trend,
        "recent_downloads": recent,
        "users": user_analytics,
    }
