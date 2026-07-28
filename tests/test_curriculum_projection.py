from __future__ import annotations

import unittest

from server.curriculum_projection import (
    project_curriculum_classes,
    resolve_curriculum_context,
)
from server.storage import _normalize_classes_for_write


def observed(
    code: str,
    *,
    room: str = "LT-201",
    teacher: str = "ABC",
    day: str = "Monday",
    start: str = "08:50",
) -> dict:
    return {
        "class_id": "c_source",
        "day": day,
        "start_time": start,
        "end_time": "09:40",
        "subject": code,
        "code": code,
        "teacher": teacher,
        "type": "Lecture",
        "room": room,
        "options": [],
    }


def option(code: str, place: str, teacher: str) -> dict:
    return {
        "subject_code": code,
        "subject_name": code,
        "type": "Lecture",
        "place": place,
        "teacher": teacher,
    }


SECTIONS = [
    {"kind": "core", "subject_codes": ["UMA101"]},
    {"kind": "elective_1", "subject_codes": ["UCS501", "UCS502", "UCS503"]},
    {"kind": "elective_2", "subject_codes": ["UEC501", "UEC502"]},
]


class CurriculumProjectionTests(unittest.TestCase):
    def test_batch_context_pool_regular_independent_and_inherited(self) -> None:
        self.assertEqual(resolve_curriculum_context("1A11", "ODD 26-27").requested_key, "POOL-A:S1")
        self.assertEqual(resolve_curriculum_context("1B11", "EVEN 26-27").requested_key, "POOL-B:S2")
        self.assertEqual(resolve_curriculum_context("2C11", "ODD 26-27").requested_key, "C:S3")
        self.assertEqual(resolve_curriculum_context("1X11", "EVEN 26-27").requested_key, "X:S2")
        special = resolve_curriculum_context("2UOQ", "ODD 26-27")
        self.assertEqual(special.requested_key, "CE-2+2:S3")
        self.assertEqual(special.resolved_key, "C:S3")

    def test_single_elective_keeps_excel_location_and_teacher(self) -> None:
        context = resolve_curriculum_context("2C11", "ODD 26-27")
        classes, issues = project_curriculum_classes(
            [observed("UCS501L", room="LT-9", teacher="TCH")],
            context=context,
            sections=SECTIONS,
            library_revision=3,
        )
        entry = classes[0]
        self.assertTrue(entry["requires_selection"])
        self.assertEqual(entry["curriculum_section"], "elective_1")
        self.assertIsNone(entry["code"])
        self.assertEqual(entry["options"][0]["subject_code"], "UCS501L")
        self.assertEqual(entry["options"][0]["place"], "LT-9")
        self.assertEqual(entry["options"][0]["teacher"], "TCH")
        self.assertEqual([row["code"] for row in issues], ["ELECTIVE_OPTION_SET_MISMATCH"])

    def test_unknown_single_course_is_non_blocking_core(self) -> None:
        context = resolve_curriculum_context("2C11", "ODD 26-27")
        classes, issues = project_curriculum_classes(
            [observed("UNO999L")],
            context=context,
            sections=SECTIONS,
            library_revision=3,
        )
        self.assertFalse(classes[0]["requires_selection"])
        self.assertEqual(classes[0]["code"], "UNO999L")
        self.assertEqual(classes[0]["curriculum_section"], "core")
        self.assertEqual([row["code"] for row in issues], ["SUBJECT_NOT_IN_LIBRARY"])

    def test_majority_section_wins_without_hiding_other_candidates(self) -> None:
        context = resolve_curriculum_context("2C11", "ODD 26-27")
        source = observed("UCS501L")
        source.update({
            "code": None,
            "options": [
                option("UCS501L", "LT1", "A"),
                option("UCS502L", "LT2", "B"),
                option("UEC501L", "LT3", "C"),
            ],
        })
        classes, issues = project_curriculum_classes(
            [source], context=context, sections=SECTIONS, library_revision=3,
        )
        entry = classes[0]
        self.assertEqual(entry["curriculum_section"], "elective_1")
        self.assertEqual([item["subject_code"] for item in entry["options"]], ["UCS501L", "UCS502L", "UEC501L"])
        self.assertEqual(
            {row["code"] for row in issues},
            {"ELECTIVE_SECTION_CONFLICT", "ELECTIVE_OPTION_SET_MISMATCH"},
        )
        option_issue = next(row for row in issues if row["code"] == "ELECTIVE_OPTION_SET_MISMATCH")
        self.assertEqual(option_issue["missing_codes"], ["UCS503"])
        self.assertEqual(option_issue["extra_codes"], ["UEC501"])

    def test_library_edit_reclassifies_same_stored_observation(self) -> None:
        context = resolve_curriculum_context("2C11", "ODD 26-27")
        source = observed("UCS501L")
        core_classes, _ = project_curriculum_classes(
            [source],
            context=context,
            sections=[{"kind": "core", "subject_codes": ["UCS501"]}],
            library_revision=1,
        )
        elective_classes, _ = project_curriculum_classes(
            [source], context=context, sections=SECTIONS, library_revision=2,
        )
        self.assertEqual(core_classes[0]["code"], "UCS501L")
        self.assertFalse(core_classes[0]["requires_selection"])
        self.assertIsNone(elective_classes[0]["code"])
        self.assertTrue(elective_classes[0]["requires_selection"])
        self.assertEqual(core_classes[0]["class_id"], elective_classes[0]["class_id"])

    def test_write_normalization_never_persists_projection_metadata(self) -> None:
        rows = _normalize_classes_for_write([{
            "day": "Monday",
            "start_time": "08:00",
            "end_time": "08:50",
            "code": "UCS501L",
            "type": "Lecture",
            "curriculum_section": "elective_1",
            "requires_selection": True,
            "elective_group_id": "C:S5:elective_1",
        }], catalog=None)
        self.assertNotIn("curriculum_section", rows[0])
        self.assertNotIn("requires_selection", rows[0])
        self.assertNotIn("elective_group_id", rows[0])


if __name__ == "__main__":
    unittest.main()
