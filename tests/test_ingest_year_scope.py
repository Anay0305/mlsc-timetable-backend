"""Year-scoped ingestion.

A workbook normally carries the whole institute, but a new intake arrives on
its own: the first-year sheets are republished while years 2-4 are untouched.
Ingesting that file without a scope prunes every batch it does not mention, so
these cover both halves — what gets parsed, and what is allowed to be deleted.
"""

from __future__ import annotations

import unittest

from server.ingest import batch_year, filter_blocks_by_year, parse_year_selector


class YearSelectorTests(unittest.TestCase):
    def test_blank_and_all_mean_the_whole_institute(self):
        for value in (None, "", "   ", "all", "ALL", []):
            self.assertIsNone(parse_year_selector(value), repr(value))

    def test_single_year(self):
        self.assertEqual(parse_year_selector("1"), frozenset({1}))

    def test_comma_separated_years(self):
        self.assertEqual(parse_year_selector("1,2"), frozenset({1, 2}))
        self.assertEqual(parse_year_selector(" 1 , 2 "), frozenset({1, 2}))

    def test_accepts_a_list(self):
        self.assertEqual(parse_year_selector([1, 3]), frozenset({1, 3}))

    def test_duplicates_collapse(self):
        self.assertEqual(parse_year_selector("2,2"), frozenset({2}))

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            parse_year_selector("first")

    def test_rejects_out_of_range(self):
        for bad in ("0", "6", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                parse_year_selector(bad)


class BatchYearTests(unittest.TestCase):
    def test_reads_the_leading_digit(self):
        self.assertEqual(batch_year("1B14"), 1)
        self.assertEqual(batch_year("3C15"), 3)
        self.assertEqual(batch_year("4O11"), 4)

    def test_handles_longer_codes(self):
        self.assertEqual(batch_year("2MCA2"), 2)

    def test_unparseable_code_has_no_year(self):
        self.assertIsNone(batch_year(""))
        self.assertIsNone(batch_year("???"))


class FilterTests(unittest.TestCase):
    MERGED = {"1A11": ["a"], "1B12": ["b"], "2C31": ["c"], "3C15": ["d"], "4O11": ["e"]}

    def test_no_scope_passes_everything_through(self):
        kept, skipped = filter_blocks_by_year(self.MERGED, None)
        self.assertEqual(kept, self.MERGED)
        self.assertEqual(skipped, [])

    def test_first_year_only(self):
        kept, skipped = filter_blocks_by_year(self.MERGED, frozenset({1}))
        self.assertEqual(sorted(kept), ["1A11", "1B12"])
        self.assertEqual(skipped, ["2C31", "3C15", "4O11"])

    def test_multiple_years(self):
        kept, _ = filter_blocks_by_year(self.MERGED, frozenset({1, 3}))
        self.assertEqual(sorted(kept), ["1A11", "1B12", "3C15"])

    def test_a_year_absent_from_the_workbook_keeps_nothing(self):
        kept, skipped = filter_blocks_by_year(self.MERGED, frozenset({5}))
        self.assertEqual(kept, {})
        self.assertEqual(len(skipped), 5)

    def test_unparseable_codes_are_left_out_of_a_scoped_run(self):
        merged = {**self.MERGED, "WEIRD": ["x"]}
        kept, skipped = filter_blocks_by_year(merged, frozenset({1}))
        self.assertNotIn("WEIRD", kept)
        self.assertIn("WEIRD", skipped)


class PruneScopeTests(unittest.IsolatedAsyncioTestCase):
    """The dangerous half: what a scoped run is allowed to delete."""

    class FakeDoc:
        def __init__(self, code, year=None):
            self.code = code
            self.year = year
            self.deleted = False

        async def delete(self):
            self.deleted = True

    async def _run_prune(self, docs, keep, scope):
        """Drive replace_timetables against stand-in documents."""
        from unittest.mock import patch

        from server import storage

        class FakeCursor:
            def __init__(self, items):
                self._items = list(items)

            def __aiter__(self):
                async def gen():
                    for item in self._items:
                        yield item
                return gen()

        with patch.object(storage.TimetableDoc, "find_all", lambda: FakeCursor(docs)):
            return await storage.replace_timetables(keep, scope_years=scope)

    async def test_without_a_scope_everything_absent_is_pruned(self):
        docs = [self.FakeDoc("1A11"), self.FakeDoc("3C15"), self.FakeDoc("4O11")]
        removed = await self._run_prune(docs, ["1A11"], None)
        self.assertEqual(removed, 2)
        self.assertEqual([d.code for d in docs if d.deleted], ["3C15", "4O11"])

    async def test_a_first_year_run_cannot_touch_other_years(self):
        docs = [self.FakeDoc("1A11"), self.FakeDoc("1B12"), self.FakeDoc("3C15"), self.FakeDoc("4O11")]
        # New intake: only 1A11 is in the workbook. 1B12 really is gone.
        removed = await self._run_prune(docs, ["1A11"], {1})
        self.assertEqual(removed, 1)
        self.assertEqual([d.code for d in docs if d.deleted], ["1B12"])
        self.assertTrue(all(not d.deleted for d in docs if d.code in ("3C15", "4O11")))

    async def test_unrecognised_codes_survive_a_scoped_run(self):
        docs = [self.FakeDoc("1A11"), self.FakeDoc("???")]
        removed = await self._run_prune(docs, ["1A11"], {1})
        self.assertEqual(removed, 0, "an unparseable code is not evidence the batch is gone")


if __name__ == "__main__":
    unittest.main()
