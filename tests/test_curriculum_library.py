from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from server import storage
from server.db.models import CurriculumLibraryDoc, CurriculumSection
from server.routers.admin import (
    _library_elective_tables_from_parsed,
    _library_kind_for_scheme_course,
    _library_plan_from_scheme_rows,
    _library_scheme_rows,
)
from server.scheme_parser import (
    _elective_heading_above_table,
    _extract_courses_from_table,
    _standalone_elective_kind,
)


class CurriculumLibraryTests(unittest.IsolatedAsyncioTestCase):
    class _LibraryRepo:
        class _Key:
            def __eq__(self, _other):
                return True

        key = _Key()
        find_one = AsyncMock()

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

    async def test_existing_document_is_preserved_as_inactive_draft(self) -> None:
        doc = CurriculumLibraryDoc.model_construct(
            key="X:S3",
            branch="X",
            semester=3,
            sections=[CurriculumSection(kind="core", subject_codes=["UCS301"])],
        )
        payload = await storage._library_payload(doc, include_subjects=False)
        self.assertEqual(payload["subject_count"], 1)
        self.assertEqual(payload["sections"][0]["subject_codes"], ["UCS301"])
        self.assertEqual(payload["status"], "draft")
        self.assertFalse(payload["published"])
        self.assertEqual(payload["published_subject_count"], 0)

    async def test_missing_or_draft_library_does_not_project_or_warn(self) -> None:
        source = {
            "batch": "2X11",
            "semester": {"label": "ODD 26-27"},
            "classes": [{"code": "UCS301L", "type": "Lecture", "options": []}],
        }
        draft = CurriculumLibraryDoc.model_construct(
            key="X:S3",
            branch="X",
            semester=3,
            sections=[CurriculumSection(kind="elective_1", subject_codes=["UCS301"])],
        )
        for row in (None, draft):
            repo = self._LibraryRepo
            repo.find_one = AsyncMock(return_value=row)
            with patch.object(storage, "CurriculumLibraryDoc", repo):
                projected, issues = await storage.project_curriculum_payload("2X11", source)
            self.assertEqual(projected["classes"], source["classes"])
            self.assertFalse(projected["curriculum"]["available"])
            self.assertEqual(issues, [])

    async def test_published_snapshot_is_independent_from_newer_draft(self) -> None:
        doc = CurriculumLibraryDoc.model_construct(
            key="X:S3",
            branch="X",
            semester=3,
            sections=[CurriculumSection(kind="core", subject_codes=["UCS301"])],
            revision=2,
            published_sections=[CurriculumSection(kind="elective_1", subject_codes=["UCS301"])],
            published_revision=1,
        )
        payload = await storage._library_payload(doc, include_subjects=False)
        self.assertEqual(payload["status"], "changes_pending")
        self.assertTrue(payload["published"])
        self.assertEqual(payload["published_revision"], 1)

        source = {
            "batch": "2X11",
            "semester": {"label": "ODD 26-27"},
            "classes": [{
                "code": "UCS301L",
                "subject": "Data Structures",
                "type": "Lecture",
                "room": "LT301",
                "teacher": "ABC",
                "options": [],
            }],
        }
        repo = self._LibraryRepo
        repo.find_one = AsyncMock(return_value=doc)
        with patch.object(storage, "CurriculumLibraryDoc", repo):
            projected, _ = await storage.project_curriculum_payload("2X11", source)
        self.assertTrue(projected["curriculum"]["available"])
        self.assertTrue(projected["classes"][0]["requires_selection"])
        self.assertEqual(projected["classes"][0]["curriculum_section"], "elective_1")

    async def test_unpublish_preserves_the_draft(self) -> None:
        doc = CurriculumLibraryDoc.model_construct(
            key="X:S3",
            branch="X",
            semester=3,
            sections=[CurriculumSection(kind="core", subject_codes=["UCS301"])],
            revision=2,
            published_sections=[CurriculumSection(kind="core", subject_codes=["UCS301"])],
            published_revision=2,
        )

        async def apply_updates(updates):
            for field, value in updates.items():
                object.__setattr__(doc, field, value)

        object.__setattr__(doc, "set", AsyncMock(side_effect=apply_updates))
        repo = self._LibraryRepo
        repo.find_one = AsyncMock(return_value=doc)
        with (
            patch.object(storage, "CurriculumLibraryDoc", repo),
            patch.object(storage, "refresh_curriculum_errors_for_library", AsyncMock()),
            patch.object(storage, "_library_payload", AsyncMock(return_value={
                "sections": [{"kind": "core", "subject_codes": ["UCS301"]}],
                "revision": 2,
                "published": False,
                "published_revision": 0,
            })),
        ):
            payload = await storage.unpublish_library_entry(
                "X", 3, expected_revision=2, actor="admin@example.com",
            )

        self.assertEqual(payload["sections"][0]["subject_codes"], ["UCS301"])
        self.assertEqual(payload["revision"], 2)
        self.assertFalse(payload["published"])
        self.assertEqual(payload["published_revision"], 0)

    def test_pdf_placeholder_classification(self) -> None:
        self.assertEqual(
            _library_kind_for_scheme_course({"title": "Elective-II"}),
            "elective_2",
        )
        self.assertEqual(
            _library_kind_for_scheme_course({"title": "Elective-IV"}),
            "elective_4",
        )
        self.assertEqual(
            _library_kind_for_scheme_course({"title": "General Elective"}),
            "general_elective",
        )

    def test_standalone_elective_table_schema_is_extracted(self) -> None:
        table = [
            ["S.\nNo.", "COURSE\nNO.", "TITLE", "CODE", "L", "T", "P", "CR"],
            ["1", "UCS531", "CLOUD COMPUTING", "PEC", "2", "0", "2", "3.0"],
            ["2", "UEC646", "CONNECTED VEHICLES", "PEC", "3", "0", "0", "3.0"],
        ]
        courses, _ = _extract_courses_from_table(table)
        self.assertEqual([course.code for course in courses], ["UCS531", "UEC646"])
        self.assertEqual(courses[0].category, "PEC")
        self.assertEqual(_standalone_elective_kind("Elective IV"), "elective_4")

        class _Page:
            @staticmethod
            def extract_words():
                return [
                    {"text": "Elective", "top": 50, "bottom": 62, "x0": 20},
                    {"text": "I", "top": 50, "bottom": 62, "x0": 80},
                ]

        self.assertEqual(
            _elective_heading_above_table(_Page(), 130),
            ("elective_1", "Elective I"),
        )

        class _SemesterTablePage:
            @staticmethod
            def extract_words():
                return [
                    {"text": "GENERIC", "top": 50, "bottom": 62, "x0": 20},
                    {"text": "ELECTIVE", "top": 50, "bottom": 62, "x0": 80},
                    {"text": "SEMESTER-VII", "top": 100, "bottom": 112, "x0": 45},
                ]

        self.assertIsNone(
            _elective_heading_above_table(_SemesterTablePage(), 130),
        )

        normalized = _library_elective_tables_from_parsed({
            "elective_tables": [{
                "id": "elective_1:p9:t1",
                "section": "elective_1",
                "heading": "Elective I",
                "page": 9,
                "courses": [course.__dict__ for course in courses],
            }],
        })
        self.assertEqual(normalized[0]["section"], "elective_1")
        self.assertEqual(normalized[0]["sections"][0]["subject_codes"], ["UCS531", "UEC646"])
        self.assertIsNone(normalized[0]["target_semester"])

        assigned = _library_elective_tables_from_parsed({
            "elective_tables": [{
                "id": "elective_1:p9:t1",
                "section": "elective_1",
                "heading": "Elective I",
                "page": 9,
                "courses": [course.__dict__ for course in courses],
            }],
        }, [{
            "semester": 5,
            "sections": [
                {"kind": "core", "subject_codes": ["UCS415"]},
                {"kind": "elective_1", "subject_codes": []},
            ],
        }])
        self.assertEqual(assigned[0]["target_semester"], 5)

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
        self.assertNotIn("baseline_suggestion", plan[0])
        self.assertEqual(
            plan[0]["extracted_courses"],
            [
                {
                    "code": "UCS301",
                    "title": "Core",
                    "category": None,
                    "credits": None,
                    "section": "core",
                },
                {
                    "code": None,
                    "title": "Elective-I",
                    "category": None,
                    "credits": None,
                    "section": "elective_1",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
