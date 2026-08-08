"""Cross-batch folding, free/busy queries and improvement planning."""

from __future__ import annotations

import unittest

from server import availability as availability_lib
from server import improvement as improvement_lib
from server.improvement import ClashLimits, busy_blocks_from_classes, is_reachable_semester
from server.room_names import normalize_room
from server.schedule_index import build_index, parse_minute


def klass(day, start, end, **kwargs):
    entry = {
        "day": day,
        "start_time": start,
        "end_time": end,
        "subject": None,
        "code": None,
        "teacher": None,
        "type": "Lecture",
        "room": None,
        "options": [],
        "alternate_week_start": None,
    }
    entry.update(kwargs)
    return entry


ODD = "ODD 26-27"


class ParseMinuteTests(unittest.TestCase):
    def test_parses_24_hour(self):
        self.assertEqual(parse_minute("08:50"), 530)
        self.assertEqual(parse_minute("18:00"), 1080)

    def test_parses_legacy_meridiem(self):
        self.assertEqual(parse_minute("9:00 AM"), 540)
        self.assertEqual(parse_minute("12:00 AM"), 0)
        self.assertEqual(parse_minute("12:30 PM"), 750)

    def test_rejects_junk(self):
        self.assertIsNone(parse_minute(""))
        self.assertIsNone(parse_minute("lunch"))
        self.assertIsNone(parse_minute("25:00"))


class BuildIndexTests(unittest.TestCase):
    def test_shared_lecture_folds_into_one_occupancy(self):
        shared = klass("Monday", "08:00", "08:50", code="UCS503L", room="LT102", teacher="ASB")
        index = build_index(
            [
                ("3C11", ODD, [dict(shared)]),
                ("3C12", ODD, [dict(shared)]),
                ("3C13", ODD, [dict(shared)]),
            ],
            semester_label=ODD,
        )
        self.assertEqual(len(index.occupancies), 1)
        self.assertEqual(index.occupancies[0].batches, ("3C11", "3C12", "3C13"))
        self.assertEqual(len(index.by_room["LT102"]), 1)
        self.assertEqual(len(index.by_teacher["ASB"]), 1)

    def test_same_room_different_course_stays_separate(self):
        index = build_index(
            [
                ("3C11", ODD, [klass("Monday", "08:00", "08:50", code="UCS503L", room="LT102", teacher="ASB")]),
                ("3C21", ODD, [klass("Monday", "08:50", "09:40", code="UCS510L", room="LT102", teacher="YAD")]),
            ],
            semester_label=ODD,
        )
        self.assertEqual(len(index.by_room["LT102"]), 2)

    def test_elective_options_book_their_own_rooms(self):
        elective = klass(
            "Monday",
            "15:30",
            "16:20",
            type="Elective",
            options=[
                {"subject_code": "UCS534P", "subject_name": "Security", "type": "Practical", "place": "L408", "teacher": "MAN"},
                {"subject_code": "UCS550P", "subject_name": "Defence", "type": "Practical", "place": "L102", "teacher": "SAL"},
            ],
        )
        index = build_index([("3C15", ODD, [elective])], semester_label=ODD)
        self.assertEqual(sorted(index.by_room), ["L102", "L408"])
        self.assertTrue(all(item.from_elective for item in index.occupancies))

    def test_batch_semester_derives_from_year_and_parity(self):
        index = build_index(
            [
                ("1B11", ODD, []),
                ("2C31", ODD, []),
                ("3C15", ODD, []),
                ("4C12", ODD, []),
            ],
            semester_label=ODD,
        )
        self.assertEqual(index.batch_semester["1B11"], 1)
        self.assertEqual(index.batch_semester["2C31"], 3)
        self.assertEqual(index.batch_semester["3C15"], 5)
        self.assertEqual(index.batch_semester["4C12"], 7)

    def test_entries_without_room_or_teacher_are_skipped(self):
        index = build_index(
            [("3C11", ODD, [klass("Monday", "08:00", "08:50", code="UCS503L")])],
            semester_label=ODD,
        )
        self.assertEqual(index.occupancies, ())


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.index = build_index(
            [
                (
                    "3C11",
                    ODD,
                    [
                        klass("Monday", "08:00", "08:50", code="UCS503L", room="LT102", teacher="ASB"),
                        klass("Monday", "10:30", "11:20", code="UCS510L", room="LT102", teacher="YAD"),
                        klass("Monday", "08:00", "08:50", code="UCS511L", room="LT101", teacher="KAP"),
                    ],
                )
            ],
            semester_label=ODD,
        )

    def test_busy_room_is_reported_with_its_class(self):
        result = availability_lib.availability(self.index, "room", day="Monday", at="08:10")
        busy = {item["name"] for item in result["busy"]}
        self.assertEqual(busy, {"LT101", "LT102"})
        self.assertEqual(result["free"], [])

    def test_free_room_reports_its_next_class(self):
        result = availability_lib.availability(self.index, "room", day="Monday", at="09:00")
        free = {item["name"]: item for item in result["free"]}
        self.assertEqual(set(free), {"LT101", "LT102"})
        self.assertEqual(free["LT102"]["next_class"]["start_time"], "10:30")
        self.assertIsNone(free["LT101"]["next_class"])

    def test_class_end_frees_the_next_slot(self):
        # A class running 08:00–08:50 must not hold the 08:50 slot.
        result = availability_lib.availability(self.index, "room", day="Monday", at="08:50")
        self.assertEqual({item["name"] for item in result["free"]}, {"LT101", "LT102"})

    def test_window_query_spans_multiple_classes(self):
        result = availability_lib.availability(
            self.index, "room", day="Monday", start="08:00", end="11:20"
        )
        self.assertEqual({item["name"] for item in result["busy"]}, {"LT101", "LT102"})

    def test_other_days_are_free(self):
        result = availability_lib.availability(self.index, "room", day="Tuesday", at="08:10")
        self.assertEqual(result["busy"], [])
        self.assertEqual(result["free_count"], 2)

    def test_day_prefix_is_accepted(self):
        result = availability_lib.availability(self.index, "room", day="mon", at="08:10")
        self.assertEqual(result["day"], "Monday")

    def test_free_windows_collapse_gaps(self):
        result = availability_lib.free_windows(self.index, "room", "LT102", day="Monday")
        self.assertEqual(
            result["free_windows"],
            [
                {"start_time": "08:50", "end_time": "10:30"},
                {"start_time": "11:20", "end_time": "18:50"},
            ],
        )

    def test_unknown_resource_raises(self):
        with self.assertRaises(availability_lib.UnknownResource):
            availability_lib.weekly_schedule(self.index, "room", "NOPE")


class ReachableSemesterTests(unittest.TestCase):
    def test_cannot_take_own_or_later_semester(self):
        self.assertFalse(is_reachable_semester(5, 5))
        self.assertFalse(is_reachable_semester(5, 7))

    def test_opposite_parity_is_never_running(self):
        # A 5th-semester student cannot attend semester 4; in an odd term no
        # batch is in an even semester at all.
        self.assertFalse(is_reachable_semester(5, 4))
        self.assertFalse(is_reachable_semester(6, 3))

    def test_same_parity_below_is_reachable(self):
        self.assertTrue(is_reachable_semester(5, 3))
        self.assertTrue(is_reachable_semester(7, 5))
        self.assertTrue(is_reachable_semester(6, 4))

    def test_first_year_semesters_are_pooled(self):
        self.assertTrue(is_reachable_semester(5, 1))
        self.assertTrue(is_reachable_semester(5, 2))
        self.assertTrue(is_reachable_semester(4, 1))

    def test_pooling_can_be_disabled(self):
        self.assertFalse(is_reachable_semester(5, 2, pool_first_year=False))
        self.assertTrue(is_reachable_semester(5, 1, pool_first_year=False))


class BusyBlockTests(unittest.TestCase):
    def test_elective_blocks_at_worst_severity_and_is_flagged(self):
        blocks = busy_blocks_from_classes(
            [
                klass(
                    "Monday",
                    "15:30",
                    "16:20",
                    type="Elective",
                    options=[
                        {"subject_code": "UCS534P", "type": "Practical", "place": "L408"},
                        {"subject_code": "UCS539L", "type": "Lecture", "place": "LT102"},
                    ],
                )
            ]
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].severity, 2)
        self.assertTrue(blocks[0].uncertain)

    def test_resolved_elective_is_certain(self):
        blocks = busy_blocks_from_classes(
            [klass("Monday", "15:30", "16:20", type="Lecture", code="UCS539L", room="LT102")]
        )
        self.assertEqual(blocks[0].severity, 0)
        self.assertFalse(blocks[0].uncertain)


class EvaluateCourseTests(unittest.TestCase):
    """A 5th-semester student repeating a 3rd-semester course."""

    def setUp(self):
        self.limits = ClashLimits(max_lecture=1, max_tutorial=1, max_practical=0)
        # UCS301L is offered to three junior batches at different times.
        self.index = build_index(
            [
                # The student's own semester-5 batch.
                (
                    "3C15",
                    ODD,
                    [
                        klass("Monday", "08:00", "08:50", code="UCS503L", type="Lecture", room="LT102", teacher="ASB"),
                        klass("Tuesday", "10:30", "12:10", code="UCS503P", type="Practical", room="L102", teacher="ASB"),
                    ],
                ),
                # Clean: no overlap at all.
                ("2C31", ODD, [klass("Wednesday", "09:40", "10:30", code="UCS301L", type="Lecture", room="LT301", teacher="KAP")]),
                # One lecture-vs-lecture clash: allowed.
                ("2C32", ODD, [klass("Monday", "08:00", "08:50", code="UCS301L", type="Lecture", room="LT302", teacher="KAP")]),
                # Clashes with the student's practical: rejected.
                ("2C33", ODD, [klass("Tuesday", "10:30", "11:20", code="UCS301L", type="Lecture", room="LT303", teacher="KAP")]),
            ],
            semester_label=ODD,
        )
        self.blocks = busy_blocks_from_classes(self.index.by_batch["3C15"] and [
            klass("Monday", "08:00", "08:50", code="UCS503L", type="Lecture", room="LT102"),
            klass("Tuesday", "10:30", "12:10", code="UCS503P", type="Practical", room="L102"),
        ])

    def _evaluate(self):
        return improvement_lib.evaluate_course(
            self.index,
            code="UCS301L",
            student_batch="3C15",
            student_semester=5,
            blocks=self.blocks,
            limits=self.limits,
        )

    def test_offers_only_junior_batches(self):
        result = self._evaluate()
        self.assertEqual({option["batch"] for option in result["options"]}, {"2C31", "2C32", "2C33"})
        self.assertTrue(all(option["semester"] == 3 for option in result["options"]))

    def test_clean_batch_ranks_first(self):
        result = self._evaluate()
        self.assertEqual(result["options"][0]["batch"], "2C31")
        self.assertEqual(result["options"][0]["clash_counts"]["total"], 0)

    def test_single_lecture_clash_is_allowed(self):
        option = next(o for o in self._evaluate()["options"] if o["batch"] == "2C32")
        self.assertTrue(option["feasible"])
        self.assertEqual(option["clash_counts"]["lecture"], 1)

    def test_clash_against_own_practical_is_rejected(self):
        option = next(o for o in self._evaluate()["options"] if o["batch"] == "2C33")
        self.assertFalse(option["feasible"])
        self.assertEqual(option["clash_counts"]["practical"], 1)
        self.assertTrue(option["clashes"][0]["blocking"])

    def test_feasible_count_excludes_blocked_batches(self):
        self.assertEqual(self._evaluate()["feasible_count"], 2)

    def test_practical_limit_is_configurable(self):
        self.limits = ClashLimits(max_lecture=1, max_tutorial=1, max_practical=1)
        option = next(o for o in self._evaluate()["options"] if o["batch"] == "2C33")
        self.assertTrue(option["feasible"])

    def test_lecture_limit_of_zero_rejects_any_overlap(self):
        self.limits = ClashLimits(max_lecture=0, max_tutorial=1, max_practical=0)
        option = next(o for o in self._evaluate()["options"] if o["batch"] == "2C32")
        self.assertFalse(option["feasible"])


class RoomNormalizationTests(unittest.TestCase):
    def test_bracketed_lab_folds_onto_its_room(self):
        self.assertEqual(normalize_room("AI(L307)")[0], ["L307"])
        self.assertEqual(normalize_room("L307")[0], ["L307"])

    def test_unclosed_bracket_is_tolerated(self):
        self.assertEqual(normalize_room("GC-2(L107")[0], ["L107"])

    def test_hyphenated_lab_name_yields_its_room(self):
        self.assertEqual(normalize_room("HIGH VOLTAGE-C101")[0], ["C101"])

    def test_two_real_rooms_both_count(self):
        self.assertEqual(normalize_room("B204/F314")[0], ["B204", "F314"])
        self.assertEqual(normalize_room("LT101/LT102")[0], ["LT101", "LT102"])

    def test_label_beside_room_is_not_a_second_room(self):
        self.assertEqual(normalize_room("CBCL/G114")[0], ["G114"])
        self.assertEqual(normalize_room("AK/TA27")[0], ["TA27"])

    def test_placeholders_book_nothing(self):
        self.assertEqual(normalize_room("Not Given")[0], [])
        self.assertEqual(normalize_room("?")[0], [])
        self.assertEqual(normalize_room("")[0], [])

    def test_named_space_without_a_number_is_kept(self):
        self.assertEqual(normalize_room("BAJAJ LAB")[0], ["BAJAJ LAB"])

    def test_original_string_is_kept_as_label(self):
        self.assertEqual(normalize_room("AI(L307)")[1], "AI(L307)")

    def test_spellings_of_one_room_share_an_index_entry(self):
        index = build_index(
            [
                ("3C11", ODD, [klass("Monday", "08:00", "08:50", code="UCS1L", room="AI(L307)", teacher="A")]),
                ("3C12", ODD, [klass("Monday", "09:40", "10:30", code="UCS2L", room="L307", teacher="B")]),
            ],
            semester_label=ODD,
        )
        self.assertEqual(sorted(index.by_room), ["L307"])
        self.assertEqual(len(index.by_room["L307"]), 2)

    def test_class_in_two_rooms_marks_both_busy(self):
        index = build_index(
            [("3C11", ODD, [klass("Monday", "08:00", "08:50", code="UCS1L", room="LT101/LT102", teacher="A")])],
            semester_label=ODD,
        )
        result = availability_lib.availability(index, "room", day="Monday", at="08:10")
        self.assertEqual({item["name"] for item in result["busy"]}, {"LT101", "LT102"})


class StaleTermLabelTests(unittest.TestCase):
    def test_current_term_overrides_a_document_left_behind(self):
        # A batch missed by the latest ingest still says EVEN; deriving its
        # semester from that would put it in an even semester mid-odd-term.
        index = build_index(
            [("2C31", "EVEN 25-26", []), ("2C32", ODD, [])],
            semester_label=ODD,
        )
        self.assertEqual(index.batch_semester["2C31"], 3)
        self.assertEqual(index.batch_semester["2C32"], 3)
        self.assertEqual(index.stale_term_batches, frozenset({"2C31"}))


class CodeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.index = build_index(
            [
                ("2C31", ODD, [
                    klass("Monday", "08:00", "08:50", code="UCS301L", room="LT301", teacher="KAP"),
                    klass("Tuesday", "08:00", "08:50", code="BEST33", room="LT302", teacher="RSH"),
                ]),
            ],
            semester_label=ODD,
        )

    def test_suffixed_code_normalizes_to_its_base(self):
        self.assertEqual(improvement_lib.resolve_code(self.index, "UCS301L"), "UCS301")
        self.assertEqual(improvement_lib.resolve_code(self.index, "UCS301"), "UCS301")

    def test_already_normalized_odd_code_is_not_mangled(self):
        # base_course_code strips a trailing T from anything it does not
        # recognise, so a second pass would turn BEST33 into something else.
        self.assertEqual(improvement_lib.resolve_code(self.index, "BEST33"), "BEST33")

    def test_offering_survives_a_round_trip_through_our_own_output(self):
        courses = improvement_lib.available_courses(
            self.index, student_batch="3C15", student_semester=5
        )
        for course in courses:
            offerings = improvement_lib.offerings_for_code(
                self.index, course["code"], student_semester=5
            )
            self.assertTrue(offerings, f"{course['code']} lost on round trip")


class SessionDedupeTests(unittest.TestCase):
    def test_class_in_two_rooms_counts_as_one_clash(self):
        index = build_index(
            [
                ("2C31", ODD, [
                    klass("Monday", "08:00", "08:50", code="UCS301L", room="LT101/LT102", teacher="KAP"),
                ]),
            ],
            semester_label=ODD,
        )
        blocks = busy_blocks_from_classes(
            [klass("Monday", "08:00", "08:50", code="UCS503L", type="Lecture", room="LT999")]
        )
        result = improvement_lib.evaluate_course(
            index,
            code="UCS301",
            student_batch="3C15",
            student_semester=5,
            blocks=blocks,
            limits=ClashLimits(max_lecture=1, max_tutorial=1, max_practical=0),
        )
        option = result["options"][0]
        self.assertEqual(len(option["sessions"]), 1)
        self.assertEqual(option["clash_counts"]["lecture"], 1)
        self.assertTrue(option["feasible"])


class PlanSearchTests(unittest.TestCase):
    """Two improvement courses must not be scheduled on top of each other."""

    def setUp(self):
        self.index = build_index(
            [
                ("3C15", ODD, []),
                # Both courses are offered in the same slot by batch "A"
                # batches, so a plan must split across the alternatives.
                ("2C31", ODD, [
                    klass("Monday", "09:40", "10:30", code="UCS301L", type="Lecture", room="LT301", teacher="KAP"),
                    klass("Monday", "09:40", "10:30", code="UCS302L", type="Lecture", room="LT301", teacher="KAP"),
                ]),
                ("2C32", ODD, [
                    klass("Tuesday", "09:40", "10:30", code="UCS302L", type="Lecture", room="LT302", teacher="RSH"),
                ]),
            ],
            semester_label=ODD,
        )

    def _plan(self, codes, **overrides):
        from server.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "improvement_max_lecture_clashes", overrides.get("lecture", 0))
        object.__setattr__(settings, "improvement_max_tutorial_clashes", 0)
        object.__setattr__(settings, "improvement_max_practical_clashes", 0)
        object.__setattr__(settings, "improvement_pool_first_year_semesters", True)
        object.__setattr__(settings, "improvement_max_plan_options", 20)
        return improvement_lib.plan_improvements(
            self.index,
            student_batch="3C15",
            student_semester=5,
            student_classes=[],
            codes=codes,
            settings=settings,
        )

    def test_plan_avoids_stacking_two_courses_in_one_slot(self):
        result = self._plan(["UCS301L", "UCS302L"])
        self.assertTrue(result["plans"])
        for plan in result["plans"]:
            # Codes normalize to their base form: one course covers L/T/P.
            picked = {pick["code"]: pick["batch"] for pick in plan["picks"]}
            self.assertEqual(picked["UCS301"], "2C31")
            # 2C31 also teaches UCS302 in the same slot, so it must be avoided.
            self.assertEqual(picked["UCS302"], "2C32")
            self.assertEqual(plan["total_clashes"], 0)

    def test_unknown_course_is_reported_not_planned(self):
        result = self._plan(["UCS999L"])
        self.assertEqual(result["unavailable_codes"], ["UCS999"])
        self.assertEqual(result["plans"], [])

    def test_single_course_still_produces_a_plan(self):
        result = self._plan(["UCS302L"])
        self.assertTrue(result["plans"])
        self.assertEqual(len(result["plans"][0]["picks"]), 1)


class AvailableCoursesTests(unittest.TestCase):
    def test_lists_only_reachable_semesters(self):
        index = build_index(
            [
                ("3C15", ODD, [klass("Monday", "08:00", "08:50", code="UCS503L", room="LT102", teacher="ASB")]),
                ("2C31", ODD, [klass("Monday", "09:40", "10:30", code="UCS301L", room="LT301", teacher="KAP")]),
                ("4C12", ODD, [klass("Monday", "09:40", "10:30", code="UCS701L", room="LT401", teacher="XYZ")]),
            ],
            semester_label=ODD,
        )
        courses = improvement_lib.available_courses(
            index, student_batch="3C15", student_semester=5
        )
        codes = {course["code"] for course in courses}
        self.assertIn("UCS301", codes)
        self.assertNotIn("UCS503", codes)  # own semester
        self.assertNotIn("UCS701", codes)  # senior semester


if __name__ == "__main__":
    unittest.main()
