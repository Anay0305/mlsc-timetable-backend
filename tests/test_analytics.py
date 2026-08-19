from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from server.routers.analytics import (
    _empty_daily_user_trend,
    _get_user_analytics,
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
        user_filters: list[dict] = []
        user_aggregates = iter([
            # 1. top batches
            _AggregateResult([
                {"_id": "3C11", "count": 22},
                {"_id": "2Q22", "count": 16},
            ]),
            # 2. per-batch rollup, folded into years
            _AggregateResult([
                {"_id": "1A11", "users": 12, "active_24h": 3, "active_7d": 8},
                {"_id": "1B12", "users": 5, "active_24h": 1, "active_7d": 4},
                {"_id": "3C11", "users": 22, "active_24h": 6, "active_7d": 15},
                {"_id": "????", "users": 2, "active_24h": 0, "active_7d": 1},
            ]),
            # 3. registration trend
            _AggregateResult([
                {"_id": "2026-07-29", "count": 3},
            ]),
        ])
        calendar_counts = iter([14, 9])
        calendar_filters: list[dict] = []

        def user_find(query: dict) -> _CountQuery:
            user_filters.append(query)
            return _CountQuery(next(user_counts))

        def calendar_find(query: dict) -> _CountQuery:
            calendar_filters.append(query)
            return _CountQuery(next(calendar_counts))

        with (
            patch.object(UserDoc, "find", side_effect=user_find),
            patch.object(UserDoc, "aggregate", side_effect=lambda *_args, **_kwargs: next(user_aggregates)),
            patch.object(
                PersonalCustomizationDoc,
                "aggregate",
                return_value=_AggregateResult([{"count": 18}]),
            ),
            patch.object(
                CalendarConnectionDoc,
                "find",
                side_effect=calendar_find,
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

        # Year rollup: batches fold into their year, active counts add up, and
        # a code we cannot read is kept as its own bucket rather than dropped.
        self.assertEqual(result["by_year"], [
            {"year": 1, "users": 17, "active_24h": 4, "active_7d": 12, "batches": 2},
            {"year": 3, "users": 22, "active_24h": 6, "active_7d": 15, "batches": 1},
            {"year": None, "users": 2, "active_24h": 0, "active_7d": 1, "batches": 1},
        ])
        # 100 total, 80 with a saved batch.
        self.assertEqual(result["without_batch"], 20)
        self.assertEqual(len(result["registration_trend"]), 30)
        self.assertEqual(result["registration_trend"][0]["date"], "2026-07-01")
        self.assertEqual(result["registration_trend"][-1]["date"], "2026-07-30")
        self.assertEqual(
            next(row for row in result["registration_trend"] if row["date"] == "2026-07-29")["count"],
            3,
        )
        self.assertNotIn("email", result)
        self.assertNotIn("user_ids", result)
        self.assertEqual(user_filters[0], {})
        self.assertTrue(all("user_id" not in query for query in user_filters))
        self.assertEqual(calendar_filters, [{}, {"enabled": True}])


if __name__ == "__main__":
    unittest.main()
