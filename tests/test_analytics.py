from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from server.routers.analytics import (
    _empty_daily_user_trend,
    _get_user_analytics,
    _signed_in_user_filter,
)
from server.db.models import CalendarConnectionDoc, PersonalCustomizationDoc, UserDoc


class _CountQuery:
    def __init__(self, count: int):
        self.value = count

    async def count(self) -> int:
        return self.value


class _AggregateResult:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def to_list(self) -> list[dict]:
        return self.rows


class UserAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def test_signed_in_filter_keeps_clerk_scope_and_extra_constraints(self):
        result = _signed_in_user_filter(last_seen_at={"$gte": "cutoff"})
        self.assertEqual(result["user_id"], {"$regex": r"^user_"})
        self.assertEqual(result["last_seen_at"], {"$gte": "cutoff"})

    def test_empty_trend_has_exact_window(self):
        trend = _empty_daily_user_trend(
            datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
            days=3,
        )
        self.assertEqual(trend, {
            "2026-07-01": 0,
            "2026-07-02": 0,
            "2026-07-03": 0,
        })

    async def test_user_summary_is_aggregate_only_and_fills_missing_days(self):
        user_counts = iter([100, 10, 35, 70, 12, 80])
        user_aggregates = iter([
            _AggregateResult([
                {"_id": "3C11", "count": 22},
                {"_id": "2Q22", "count": 16},
            ]),
            _AggregateResult([
                {"_id": "2026-07-29", "count": 3},
            ]),
        ])
        calendar_counts = iter([14, 9])

        with (
            patch.object(UserDoc, "find", side_effect=lambda *_args, **_kwargs: _CountQuery(next(user_counts))),
            patch.object(UserDoc, "aggregate", side_effect=lambda *_args, **_kwargs: next(user_aggregates)),
            patch.object(
                PersonalCustomizationDoc,
                "aggregate",
                return_value=_AggregateResult([{"count": 18}]),
            ),
            patch.object(
                CalendarConnectionDoc,
                "find",
                side_effect=lambda *_args, **_kwargs: _CountQuery(next(calendar_counts)),
            ),
        ):
            result = await _get_user_analytics(
                datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
            )

        self.assertEqual(result["total"], 100)
        self.assertEqual(result["active_24h"], 10)
        self.assertEqual(result["active_7d"], 35)
        self.assertEqual(result["active_30d"], 70)
        self.assertEqual(result["new_30d"], 12)
        self.assertEqual(result["with_default_batch"], 80)
        self.assertEqual(result["with_personalization"], 18)
        self.assertEqual(result["calendar_connected"], 14)
        self.assertEqual(result["calendar_enabled"], 9)
        self.assertEqual(result["top_batches"][0], {"batch": "3C11", "count": 22})
        self.assertEqual(len(result["registration_trend"]), 30)
        self.assertEqual(result["registration_trend"][0]["date"], "2026-07-01")
        self.assertEqual(result["registration_trend"][-1]["date"], "2026-07-30")
        self.assertEqual(
            next(row for row in result["registration_trend"] if row["date"] == "2026-07-29")["count"],
            3,
        )
        self.assertNotIn("email", result)
        self.assertNotIn("user_ids", result)


if __name__ == "__main__":
    unittest.main()
