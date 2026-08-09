"""What the calendar should contain — the rules that keep it honest."""

from __future__ import annotations

import unittest

from server.gcal.projection import (
    DesiredEvent,
    ProjectionError,
    day_index,
    first_on_or_after,
    is_visible_to_student,
    merge_adjacent,
    minutes_of,
    parse_time,
    project,
    visible_classes,
)

TERM = {"term_start": "2026-07-27", "term_end": "2026-12-19"}


def klass(day="Monday", start="09:40", end="10:30", **kw):
    entry = {
        "day": day, "start_time": start, "end_time": end,
        "subject": "Software Engineering", "code": "UCS503L", "type": "Lecture",
        "room": "LT102", "teacher": "ASB", "options": [],
        "alternate_week_start": None,
    }
    entry.update(kw)
    return entry


ELECTIVE_OPTIONS = [
    {"subject_code": "UCS534P", "subject_name": "Security", "type": "Practical", "place": "L408"},
    {"subject_code": "UCS550P", "subject_name": "Defence", "type": "Practical", "place": "L102"},
]


class TimeHelperTests(unittest.TestCase):
    def test_parses_both_clock_styles(self):
        self.assertEqual(parse_time("09:40"), "09:40:00")
        self.assertEqual(parse_time("9:40 AM"), "09:40:00")
        self.assertEqual(parse_time("1:05 PM"), "13:05:00")
        self.assertEqual(parse_time("12:30 AM"), "00:30:00")

    def test_rejects_nonsense(self):
        for bad in ("", None, "lunch", "25:00", "10"):
            self.assertIsNone(parse_time(bad), bad)

    def test_minutes_and_days(self):
        self.assertEqual(minutes_of("09:40"), 580)
        self.assertEqual(day_index("Monday"), 0)
        self.assertEqual(day_index("saturday"), 5)
        self.assertIsNone(day_index("Blursday"))

    def test_first_occurrence_includes_the_start_day(self):
        from datetime import date
        # 2026-07-27 is a Monday; asking for Monday must not skip a week.
        self.assertEqual(first_on_or_after(0, date(2026, 7, 27)), date(2026, 7, 27))
        self.assertEqual(first_on_or_after(2, date(2026, 7, 27)), date(2026, 7, 29))


class VisibilityTests(unittest.TestCase):
    """The single most important rule: never invent a class."""

    def test_a_normal_class_is_visible(self):
        self.assertTrue(is_visible_to_student(klass()))

    def test_unchosen_elective_is_hidden(self):
        entry = klass(code=None, subject=None, type="Elective", room=None,
                      options=ELECTIVE_OPTIONS)
        self.assertFalse(is_visible_to_student(entry))

    def test_chosen_elective_is_visible(self):
        entry = klass(code="UCS534P", subject="Security", type="Practical",
                      room="L408", options=ELECTIVE_OPTIONS, electiveChoice="UCS534P")
        self.assertTrue(is_visible_to_student(entry))

    def test_dismissed_elective_is_hidden(self):
        # Student picked a course offered in another slot; this slot is free.
        entry = klass(options=ELECTIVE_OPTIONS, electiveChoice="UCS550P",
                      electiveDismissed=True)
        self.assertFalse(is_visible_to_student(entry))

    def test_snake_case_flags_are_honoured(self):
        self.assertFalse(is_visible_to_student(klass(elective_dismissed=True)))

    def test_single_option_cell_is_not_an_elective(self):
        entry = klass(options=[ELECTIVE_OPTIONS[0]])
        self.assertTrue(is_visible_to_student(entry))

    def test_unchosen_elective_produces_no_event_at_all(self):
        events = project(batch="3C15", classes=[
            klass(code=None, subject=None, type="Elective", room=None, options=ELECTIVE_OPTIONS),
        ], **TERM)
        self.assertEqual(events, [])

    def test_chosen_elective_produces_the_chosen_course_only(self):
        events = project(batch="3C15", classes=[
            klass(code="UCS534P", subject="Security", type="Practical",
                  room="L408", options=ELECTIVE_OPTIONS, electiveChoice="UCS534P"),
        ], **TERM)
        self.assertEqual(len(events), 1)
        self.assertIn("Security", events[0].summary)
        self.assertNotIn("Defence", events[0].description)
        self.assertNotIn("Elective", events[0].summary)


class MergeTests(unittest.TestCase):
    def test_contiguous_periods_become_one_event(self):
        merged = merge_adjacent([
            klass(start="09:40", end="10:30", code="UCS503P", type="Practical"),
            klass(start="10:30", end="11:20", code="UCS503P", type="Practical"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["start_time"], "09:40")
        self.assertEqual(merged[0]["end_time"], "11:20")

    def test_a_gap_keeps_them_separate(self):
        merged = merge_adjacent([
            klass(start="09:40", end="10:30"),
            klass(start="11:20", end="12:10"),
        ])
        self.assertEqual(len(merged), 2)

    def test_different_rooms_do_not_merge(self):
        merged = merge_adjacent([
            klass(start="09:40", end="10:30", room="LT102"),
            klass(start="10:30", end="11:20", room="LT103"),
        ])
        self.assertEqual(len(merged), 2)

    def test_different_days_do_not_merge(self):
        merged = merge_adjacent([
            klass(day="Monday", start="09:40", end="10:30"),
            klass(day="Tuesday", start="10:30", end="11:20"),
        ])
        self.assertEqual(len(merged), 2)


class ProjectionTests(unittest.TestCase):
    def test_weekly_class_anchors_on_the_first_matching_day(self):
        events = project(batch="3C15", classes=[klass(day="Wednesday")], **TERM)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].start_date, "2026-07-29")
        self.assertIn("BYDAY=WE", events[0].recurrence[0])
        self.assertIn("UNTIL=20261219", events[0].recurrence[0])

    def test_sunday_is_never_scheduled(self):
        self.assertEqual(project(batch="3C15", classes=[klass(day="Sunday")], **TERM), [])

    def test_saturday_is_kept(self):
        events = project(batch="3C15", classes=[klass(day="Saturday")], **TERM)
        self.assertEqual(len(events), 1)

    def test_holiday_adds_an_exdate_only_for_that_weekday(self):
        events = project(batch="3C15", classes=[klass(day="Monday", start="09:40")],
                         overrides=[{"kind": "holiday", "date": "2026-08-17"}], **TERM)
        joined = " ".join(events[0].recurrence)
        self.assertIn("EXDATE", joined)
        self.assertIn("20260817T094000", joined)

    def test_holiday_on_another_weekday_is_ignored(self):
        events = project(batch="3C15", classes=[klass(day="Monday")],
                         overrides=[{"kind": "holiday", "date": "2026-08-18"}], **TERM)
        self.assertEqual(len(events[0].recurrence), 1)

    def test_follow_day_creates_a_one_off_and_cancels_the_normal_day(self):
        events = project(
            batch="3C15",
            classes=[klass(day="Monday", start="09:40")],
            overrides=[{"kind": "follow_day", "date": "2026-08-22", "follows_day": 0, "id": "o1"}],
            **TERM,
        )
        weekly = [e for e in events if e.kind == "class"]
        oneoff = [e for e in events if e.kind == "follow_day"]
        self.assertEqual(len(oneoff), 1)
        self.assertEqual(oneoff[0].start_date, "2026-08-22")
        # 2026-08-22 is a Saturday, so the Monday series is untouched.
        self.assertNotIn("20260822", " ".join(weekly[0].recurrence))

    def test_holiday_beats_a_follow_day_on_the_same_date(self):
        events = project(
            batch="3C15",
            classes=[klass(day="Monday")],
            overrides=[
                {"kind": "follow_day", "date": "2026-08-17", "follows_day": 0, "id": "o1"},
                {"kind": "holiday", "date": "2026-08-17"},
            ],
            **TERM,
        )
        self.assertEqual([e for e in events if e.kind == "follow_day"], [])

    def test_exam_period_becomes_an_all_day_banner(self):
        events = project(batch="3C15", classes=[],
                         overrides=[{"kind": "mst", "date": "2026-09-14", "reason": "MST week"}],
                         **TERM)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "all_day")
        self.assertIsNone(events[0].start_time)
        self.assertEqual(events[0].summary, "MST week")

    def test_alternate_weeks_use_a_fortnightly_rule(self):
        events = project(batch="3C15", classes=[klass(alternate_week_start=1)], **TERM)
        self.assertIn("INTERVAL=2", events[0].recurrence[0])

    def test_identical_rows_collapse_to_one_event(self):
        events = project(batch="3C15", classes=[klass(), klass()], **TERM)
        self.assertEqual(len(events), 1)

    def test_slot_ids_are_stable_across_runs(self):
        a = project(batch="3C15", classes=[klass()], **TERM)
        b = project(batch="3C15", classes=[klass()], **TERM)
        self.assertEqual([e.slot_id for e in a], [e.slot_id for e in b])
        self.assertEqual([e.fingerprint for e in a], [e.fingerprint for e in b])

    def test_fingerprint_changes_when_the_room_changes(self):
        a = project(batch="3C15", classes=[klass(room="LT102")], **TERM)
        b = project(batch="3C15", classes=[klass(room="LT999")], **TERM)
        self.assertEqual(a[0].slot_id, b[0].slot_id)      # same class...
        self.assertNotEqual(a[0].fingerprint, b[0].fingerprint)  # ...changed content

    def test_a_bare_code_is_never_shown_when_a_name_exists(self):
        events = project(batch="3C15", classes=[klass(subject="Software Engineering")], **TERM)
        self.assertTrue(events[0].summary.startswith("Software Engineering"))

    def test_code_is_used_when_no_name_is_available(self):
        events = project(batch="3C15", classes=[klass(subject=None)], **TERM)
        self.assertIn("UCS503L", events[0].summary)

    def test_rejects_a_reversed_term(self):
        with self.assertRaises(ProjectionError):
            project(batch="3C15", classes=[], term_start="2026-12-19", term_end="2026-07-27")

    def test_rejects_a_missing_batch(self):
        with self.assertRaises(ProjectionError):
            project(batch="", classes=[], **TERM)

    def test_classes_outside_the_term_are_dropped(self):
        events = project(batch="3C15", classes=[klass(day="Monday")],
                         term_start="2026-07-27", term_end="2026-07-28")
        self.assertEqual(len(events), 1)   # Monday 27th falls inside
        events = project(batch="3C15", classes=[klass(day="Friday")],
                         term_start="2026-07-27", term_end="2026-07-28")
        self.assertEqual(events, [])       # no Friday before the term ends


if __name__ == "__main__":
    unittest.main()
