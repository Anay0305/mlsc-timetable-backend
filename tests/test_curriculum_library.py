from __future__ import annotations

import unittest

from server import storage
from server.routers.admin import (
    _library_kind_for_scheme_course,
    _library_plan_from_scheme_rows,
    _library_scheme_rows,
)


class CurriculumLibraryTests(unittest.IsolatedAsyncioTestCase):
    def test_library_key_supports_regular_and_special_branches(self) -> None:
        self.assertEqual(storage.library_key("c", 3), "C:S3")
        self.assertEqual(storage.library_key("CE-2+2", 4), "CE-2+2:S4")
        self.assertEqual(storage.library_key("POOL-A", 2), "POOL-A:S2")
        self.assertEqual(storage.library_key("POOL-C", 1), "POOL-C:S1")
        self.assertEqual(storage.library_key("POOL-D", 2), "POOL-D:S2")
        self.assertEqual(storage.library_key("X", 1), "X:S1")
        with self.assertRaises(ValueError):
            storage.library_key("C", 9)
        with self.assertRaisesRegex(ValueError, "only supports semesters 1 and 2"):
            storage.library_key("POOL-B", 3)
        with self.assertRaisesRegex(ValueError, "only supports semesters 1 and 2"):
            storage.library_key("POOL-C", 3)
        with self.assertRaisesRegex(ValueError, "only supports semesters 3 through 8"):
            storage.library_key("C", 2)
        with self.assertRaisesRegex(ValueError, "only supports semesters 3 through 8"):
            storage.library_key("CE-2+2", 2)

    async def test_inherited_branch_cannot_be_written_separately(self) -> None:
        with self.assertRaisesRegex(ValueError, "inherits automatically"):
            await storage.write_library_entry("CE-2+2", 3, [])

    async def test_core_is_permanent(self) -> None:
        sections = await storage._clean_library_sections([])
        self.assertEqual([section.kind for section in sections], ["core"])

    async def test_subject_cannot_appear_in_two_sections(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than one section"):
            await storage._clean_library_sections([
                {"kind": "core", "subject_codes": ["UCS301"]},
                {"kind": "elective_1", "subject_codes": ["UCS301"]},
            ])

    def test_pdf_placeholder_classification(self) -> None:
        self.assertEqual(
            _library_kind_for_scheme_course({"title": "Elective-II"}),
            "elective_2",
        )
        self.assertEqual(
            _library_kind_for_scheme_course({"title": "General Elective"}),
            "general_elective",
        )

    def test_pool_rotation_and_library_plan(self) -> None:
        parsed = {"semesters": [
            {"number": 1, "options": [{"courses": [{"code": "UCS101", "title": "First"}]}]},
            {"number": 2, "options": [{"courses": [{"code": "UCS102", "title": "Second"}]}]},
            {"number": 3, "options": [{"courses": [
                {"code": "UCS301", "title": "Core", "L": "3", "T": "1", "P": "0"},
                {"code": None, "title": "Elective-I"},
            ]}]},
        ]}
        pool = _library_scheme_rows(parsed, "POOL")
        self.assertEqual(
            [(row["branch"], row["semester"]) for row in pool],
            [("POOL-A", 1), ("POOL-A", 2), ("POOL-B", 1), ("POOL-B", 2)],
        )
        self.assertEqual(pool[2]["courses"][0]["code"], "UCS102")

        rows = _library_scheme_rows(parsed, "C")
        plan = _library_plan_from_scheme_rows(rows, "scheme.pdf")
        self.assertEqual(plan[0]["key"], "C:S3")
        self.assertEqual(plan[0]["sections"][0]["subject_codes"], ["UCS301"])
        self.assertIn("elective_1", [section["kind"] for section in plan[0]["sections"]])
        self.assertEqual(plan[0]["baseline_suggestion"], {"Lecture": 3, "Tutorial": 1})


if __name__ == "__main__":
    unittest.main()
