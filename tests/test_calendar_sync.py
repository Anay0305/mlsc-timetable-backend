from __future__ import annotations

import unittest

from server.calendar_sync import (
    _build_base_event,
    _merge_adjacent_classes_for_calendar,
)


def klass(
    start: str,
    end: str,
    *,
    day: str = "Monday",
    code: str = "UCS301P",
    room: str = "PL-4(L011)",
    teacher: str = "RAJ",
    class_type: str = "Practical",
    alternate_week_start: int | None = None,
) -> dict:
    return {
        "day": day,
        "start_time": start,
        "end_time": end,
        "subject": "Data Structures",
        "code": code,
        "teacher": teacher,
        "type": class_type,
        "room": room,
        "alternate_week_start": alternate_week_start,
        "options": [],
    }


class CalendarClassMergeTests(unittest.TestCase):
    def test_merges_contiguous_practical_slots_into_one_event_source(self) -> None:
        merged = _merge_adjacent_classes_for_calendar([
            klass("09:40", "10:30"),
            klass("08:50", "09:40"),
            klass("10:30", "11:20"),
        ])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["start_time"], "08:50")
        self.assertEqual(merged[0]["end_time"], "11:20")

        event = _build_base_event(
            "2Q22",
            merged[0],
            "2026-12-31",
            {},
            term_start_date="2026-08-01",
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["start"]["dateTime"], "2026-08-03T08:50:00")
        self.assertEqual(event["end"]["dateTime"], "2026-08-03T11:20:00")

    def test_rule_applies_to_lectures_too_and_preserves_teacher_codes(self) -> None:
        merged = _merge_adjacent_classes_for_calendar([
            klass("08:50", "09:40", code="UCS301L", room="LT302", teacher="AJD", class_type="Lecture"),
            klass("09:40", "10:30", code="ucs301l", room=" lt302 ", teacher="KAP", class_type="Lecture"),
        ])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["end_time"], "10:30")
        self.assertEqual(merged[0]["teacher"], "AJD / KAP")

    def test_does_not_merge_across_gap_different_room_or_different_code(self) -> None:
        merged = _merge_adjacent_classes_for_calendar([
            klass("08:50", "09:40"),
            klass("10:30", "11:20"),
            klass("11:20", "12:10", room="PL-5"),
            klass("12:10", "13:00", code="UCS302P", room="PL-5"),
        ])

        self.assertEqual(len(merged), 4)

    def test_does_not_merge_different_alternate_week_recurrences(self) -> None:
        merged = _merge_adjacent_classes_for_calendar([
            klass("08:50", "09:40", alternate_week_start=1),
            klass("09:40", "10:30", alternate_week_start=2),
        ])

        self.assertEqual(len(merged), 2)

    def test_merges_parallel_class_chains_independently(self) -> None:
        merged = _merge_adjacent_classes_for_calendar([
            klass("08:50", "09:40", code="UCS301P", room="PL-1"),
            klass("08:50", "09:40", code="UCS302P", room="PL-2"),
            klass("09:40", "10:30", code="UCS301P", room="PL-1"),
            klass("09:40", "10:30", code="UCS302P", room="PL-2"),
        ])

        self.assertEqual(len(merged), 2)
        self.assertEqual({entry["end_time"] for entry in merged}, {"10:30"})

    def test_incomplete_entries_without_code_or_room_stay_separate(self) -> None:
        merged = _merge_adjacent_classes_for_calendar([
            klass("08:50", "09:40", code=""),
            klass("09:40", "10:30", code=""),
            klass("10:30", "11:20", room=""),
            klass("11:20", "12:10", room=""),
        ])

        self.assertEqual(len(merged), 4)


if __name__ == "__main__":
    unittest.main()
