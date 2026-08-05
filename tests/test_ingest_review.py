import unittest

from server.ingest_review import diff_batch, resolve_reviewed_classes


def entry(
    code="UCS301L",
    *,
    day="Monday",
    start="08:00",
    end="08:50",
    room="LT101",
    teacher="ABC",
    kind="Lecture",
):
    return {
        "day": day,
        "start_time": start,
        "end_time": end,
        "subject": "Data Structures",
        "code": code,
        "teacher": teacher,
        "type": kind,
        "room": room,
        "options": [],
    }


class IngestReviewTests(unittest.TestCase):
    def test_identical_batch_has_no_changes(self):
        _, _, changes = diff_batch("2Q22", [entry()], [entry()])
        self.assertEqual(changes, [])

    def test_room_and_teacher_edit_is_one_modified_change(self):
        before, after, changes = diff_batch(
            "2Q22",
            [entry()],
            [entry(room="LT202", teacher="XYZ")],
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "modified")
        self.assertEqual(changes[0]["changed_fields"], ["teacher", "room"])
        self.assertEqual(before[0]["class_id"], after[0]["class_id"])

    def test_unambiguous_move_preserves_class_id(self):
        before, after, changes = diff_batch(
            "2Q22",
            [entry()],
            [entry(day="Tuesday", start="09:40", end="10:30")],
        )
        self.assertEqual(changes[0]["kind"], "moved")
        self.assertEqual(before[0]["class_id"], after[0]["class_id"])
        self.assertEqual(changes[0]["changed_fields"], ["day", "start_time", "end_time"])

    def test_same_slot_course_change_is_replacement(self):
        _, _, changes = diff_batch("2Q22", [entry()], [entry(code="UCS302L")])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "replaced")

    def test_add_remove_and_mixed_decisions(self):
        old = entry()
        added = entry(code="UMA021L", day="Tuesday")
        before, _, changes = diff_batch("2Q22", [old], [added])
        decisions = {
            change["change_id"]: (
                "keep_current" if change["kind"] == "removed" else "use_uploaded"
            )
            for change in changes
        }
        resolved = resolve_reviewed_classes(before, changes, decisions)
        self.assertEqual({row["code"] for row in resolved}, {"UCS301L", "UMA021L"})

    def test_catalog_backed_subject_name_does_not_create_change(self):
        old = entry()
        new = entry()
        new["subject"] = "A catalog rename"
        _, _, changes = diff_batch("2Q22", [old], [new])
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
