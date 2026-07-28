from __future__ import annotations

import unittest
from datetime import datetime, timezone

from server.personal_timetable import (
    apply_operations,
    class_fingerprint,
    convert_legacy_entries,
    merge_draft_operations,
    with_stable_class_ids,
)
from server.storage import _apply_change_to_classes


def klass(
    day: str,
    start: str,
    code: str,
    *,
    subject: str | None = None,
    kind: str = "Lecture",
    room: str = "LT1",
) -> dict:
    return {
        "day": day,
        "start_time": start,
        "end_time": "09:40",
        "subject": subject or code,
        "code": code,
        "teacher": None,
        "type": kind,
        "room": room,
        "options": [],
    }


class PersonalTimetableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.canonical = with_stable_class_ids("1A11", [
            klass("Monday", "08:50", "UMA101"),
            klass("Monday", "08:50", "UCS101", room="LT2"),
            klass("Tuesday", "09:40", "UPH101"),
        ])

    def test_stable_ids_are_deterministic_and_unique_for_duplicates(self) -> None:
        first = with_stable_class_ids("1A11", [klass("Monday", "08:50", "UMA101")] * 2)
        second = with_stable_class_ids("1A11", [klass("Monday", "08:50", "UMA101")] * 2)
        self.assertEqual([row["class_id"] for row in first], [row["class_id"] for row in second])
        self.assertNotEqual(first[0]["class_id"], first[1]["class_id"])

    def test_edit_targets_only_one_class_in_a_shared_slot(self) -> None:
        target = self.canonical[0]
        operations = merge_draft_operations(self.canonical, {}, [{
            "kind": "edit",
            "target_id": target["class_id"],
            "entry": {**target, "room": "LT9"},
        }])
        merged, stale = apply_operations(self.canonical, operations)
        self.assertEqual(stale, [])
        self.assertEqual([row["room"] for row in merged if row["start_time"] == "08:50"], ["LT9", "LT2"])

    def test_delete_is_persisted_as_tombstone(self) -> None:
        target = self.canonical[0]
        operations = merge_draft_operations(self.canonical, {}, [{
            "kind": "delete", "target_id": target["class_id"], "entry": None,
        }])
        self.assertEqual(operations[target["class_id"]]["kind"], "delete")
        merged, _ = apply_operations(self.canonical, operations)
        self.assertNotIn("UMA101", [row["code"] for row in merged])
        self.assertIn("UCS101", [row["code"] for row in merged])

    def test_move_and_swap_keep_stable_targets(self) -> None:
        left, _, right = self.canonical
        operations = merge_draft_operations(self.canonical, {}, [
            {"kind": "edit", "target_id": left["class_id"], "entry": {**left, "day": right["day"], "start_time": right["start_time"]}},
            {"kind": "edit", "target_id": right["class_id"], "entry": {**right, "day": left["day"], "start_time": left["start_time"]}},
        ])
        merged, _ = apply_operations(self.canonical, operations)
        by_id = {row["class_id"]: row for row in merged}
        self.assertEqual((by_id[left["class_id"]]["day"], by_id[left["class_id"]]["start_time"]), ("Tuesday", "09:40"))
        self.assertEqual((by_id[right["class_id"]]["day"], by_id[right["class_id"]]["start_time"]), ("Monday", "08:50"))

    def test_added_class_can_be_edited_then_deleted_without_residue(self) -> None:
        personal_id = "p_test-class"
        added = klass("Wednesday", "10:30", "UHU101")
        operations = merge_draft_operations(self.canonical, {}, [{
            "kind": "add", "target_id": personal_id, "entry": added,
        }])
        operations = merge_draft_operations(self.canonical, operations, [{
            "kind": "edit", "target_id": personal_id, "entry": {**added, "room": "LT8"},
        }])
        self.assertEqual(operations[personal_id]["kind"], "add")
        self.assertEqual(operations[personal_id]["entry"]["room"], "LT8")
        operations = merge_draft_operations(self.canonical, operations, [{
            "kind": "delete", "target_id": personal_id,
        }])
        self.assertNotIn(personal_id, operations)

    def test_official_change_marks_saved_operation_stale(self) -> None:
        target = self.canonical[0]
        operations = {
            target["class_id"]: {
                "kind": "edit",
                "target_id": target["class_id"],
                "entry": {**target, "room": "LT9"},
                "base_fingerprint": class_fingerprint(target),
            }
        }
        changed = [{**target, "room": "OFFICIAL-NEW"}, *self.canonical[1:]]
        merged, stale = apply_operations(changed, operations)
        self.assertEqual(stale, [target["class_id"]])
        self.assertEqual(merged[0]["room"], "OFFICIAL-NEW")

    def test_legacy_elective_maps_to_matching_group(self) -> None:
        option_a = {"subject_code": "UCS501L", "subject_name": "A", "type": "Lecture"}
        option_b = {"subject_code": "UCS502L", "subject_name": "B", "type": "Lecture"}
        elective = {**klass("Friday", "11:20", ""), "options": [option_a, option_b]}
        canonical = [elective, klass("Friday", "11:20", "UMA501")]
        chosen = {**elective, "code": "UCS502L", "subject": "B", "electiveChoice": "UCS502L"}
        operations, conflicts = convert_legacy_entries(
            user_id="user-1",
            batch="3C11",
            canonical_classes=canonical,
            entries={"Friday|11:20": {"kind": "elective_pick", "entry": chosen}},
            migrated_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(len(operations), 1)
        self.assertEqual(next(iter(operations.values()))["kind"], "elective_pick")

    def test_change_request_move_replaces_source_not_destination(self) -> None:
        source = self.canonical[0]
        moved = {**source, "day": "Thursday", "start_time": "12:10"}
        result = _apply_change_to_classes(
            self.canonical,
            batch="1A11",
            kind="edit",
            target_id=source["class_id"],
            day=source["day"],
            start_time=source["start_time"],
            entry=moved,
            existing_entry=source,
        )
        uma = [row for row in result if row["code"] == "UMA101"]
        self.assertEqual(len(uma), 1)
        self.assertEqual((uma[0]["day"], uma[0]["start_time"]), ("Thursday", "12:10"))

    def test_change_request_delete_keeps_other_class_in_shared_slot(self) -> None:
        source = self.canonical[0]
        result = _apply_change_to_classes(
            self.canonical,
            batch="1A11",
            kind="delete",
            target_id=source["class_id"],
            day=source["day"],
            start_time=source["start_time"],
            entry=None,
            existing_entry=source,
        )
        self.assertNotIn("UMA101", [row["code"] for row in result])
        self.assertIn("UCS101", [row["code"] for row in result])

    def test_class_scope_fallback_matches_existing_entry_in_shared_slot(self) -> None:
        source = self.canonical[1]
        result = _apply_change_to_classes(
            self.canonical,
            batch="1A12",
            kind="edit",
            target_id=None,
            # A personal move can make the submitted source coordinates differ
            # from the official before-image used across the class scope.
            day="Friday",
            start_time="14:40",
            entry={**source, "day": "Friday", "start_time": "14:40", "room": "LT8"},
            existing_entry=source,
        )
        by_code = {row["code"]: row for row in result}
        self.assertEqual(by_code["UMA101"]["room"], "LT1")
        self.assertEqual(by_code["UCS101"]["room"], "LT8")

    def test_missing_edit_target_never_becomes_an_add(self) -> None:
        proposed = klass("Friday", "14:40", "UNO999")
        result = _apply_change_to_classes(
            self.canonical,
            batch="1A11",
            kind="edit",
            target_id="c_missing",
            day="Friday",
            start_time="14:40",
            entry=proposed,
            existing_entry=None,
        )
        self.assertNotIn("UNO999", [row["code"] for row in result])
        self.assertEqual(len(result), len(self.canonical))


if __name__ == "__main__":
    unittest.main()
