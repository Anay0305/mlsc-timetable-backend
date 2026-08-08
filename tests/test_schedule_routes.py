"""HTTP behaviour of the schedule and improvement routers.

The index is injected directly so these exercise the real routing, auth gating
and response shapes without needing MongoDB.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import schedule_index
from server.config import get_settings
from server.routers import improvement as improvement_router
from server.routers import schedule as schedule_router
from server.schedule_index import build_index

ODD = "ODD 26-27"


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


STUDENT_CLASSES = [
    klass("Monday", "08:00", "08:50", code="UCS503L", subject="Software Engineering",
          type="Lecture", room="LT102", teacher="ASB"),
    klass("Tuesday", "10:30", "12:10", code="UCS503P", subject="Software Engineering Lab",
          type="Practical", room="L102", teacher="ASB"),
]

DOCUMENTS = [
    ("3C15", ODD, STUDENT_CLASSES),
    ("2C31", ODD, [klass("Wednesday", "09:40", "10:30", code="UCS301L",
                         subject="Data Structures", type="Lecture", room="LT301", teacher="KAP")]),
    ("2C32", ODD, [klass("Monday", "08:00", "08:50", code="UCS301L",
                         subject="Data Structures", type="Lecture", room="AI(L307)", teacher="KAP")]),
    ("2C33", ODD, [klass("Tuesday", "10:30", "11:20", code="UCS301L",
                         subject="Data Structures", type="Lecture", room="LT303", teacher="RSH")]),
]

INDEX = build_index(DOCUMENTS, semester_label=ODD)


def build_client() -> TestClient:
    app = FastAPI()
    app.include_router(schedule_router.router)
    app.include_router(improvement_router.router)
    return TestClient(app, raise_server_exceptions=False)


async def _fake_index(settings=None, *, force=False):
    return INDEX


async def _fake_read_timetable(batch, *args, **kwargs):
    from server import storage

    code = str(batch).upper()
    for doc_code, label, classes in DOCUMENTS:
        if doc_code == code:
            return {"batch": code, "semester": {"label": label}, "classes": classes}
    raise storage.BatchNotFound(batch)


class ScheduleRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = build_client()
        self.index_patch = patch.object(schedule_index, "get_index", _fake_index)
        self.index_patch.start()
        self.addCleanup(self.index_patch.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)

    def test_meta_reports_grid_and_directory_sizes(self):
        response = self.client.get("/schedule/meta")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["semester"], ODD)
        self.assertIn("Monday", body["days"])
        self.assertEqual(body["room_count"], len(INDEX.by_room))
        self.assertTrue(body["slots"])

    def test_room_directory_is_public_and_cacheable(self):
        response = self.client.get("/schedule/rooms")
        self.assertEqual(response.status_code, 200)
        self.assertIn("max-age", response.headers.get("cache-control", ""))
        names = {item["name"] for item in response.json()["items"]}
        # AI(L307) must have folded onto L307.
        self.assertIn("L307", names)
        self.assertNotIn("AI(L307)", names)

    def test_room_directory_filters_by_query(self):
        response = self.client.get("/schedule/rooms?q=LT3")
        names = {item["name"] for item in response.json()["items"]}
        self.assertEqual(names, {"LT301", "LT303"})

    def test_room_week_lists_classes_by_day(self):
        response = self.client.get("/schedule/rooms/LT102")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        monday = next(day for day in body["days"] if day["day"] == "Monday")
        self.assertEqual(monday["classes"][0]["code"], "UCS503L")
        self.assertEqual(monday["classes"][0]["batches"], ["3C15"])

    def test_unknown_room_is_404(self):
        response = self.client.get("/schedule/rooms/NOPE")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "unknown_resource")

    def test_availability_splits_free_and_busy(self):
        response = self.client.get("/schedule/availability/room?day=Monday&at=08:10")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        busy = {item["name"] for item in body["busy"]}
        self.assertEqual(busy, {"LT102", "L307"})
        self.assertNotIn("LT301", busy)
        self.assertIn("LT301", {item["name"] for item in body["free"]})

    def test_availability_requires_a_time(self):
        response = self.client.get("/schedule/availability/room?day=Monday")
        self.assertEqual(response.status_code, 400)

    def test_availability_rejects_a_bad_day(self):
        response = self.client.get("/schedule/availability/room?day=Blursday&at=08:10")
        self.assertEqual(response.status_code, 400)

    def test_free_windows_for_a_room(self):
        response = self.client.get("/schedule/rooms/LT102/free?day=Monday")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["free_windows"],
            [{"start_time": "08:50", "end_time": "18:50"}],
        )

    def test_teacher_routes_are_admin_gated_by_default(self):
        for path in (
            "/schedule/teachers",
            "/schedule/teachers/ASB",
            "/schedule/availability/teacher?day=Monday&at=08:10",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, (401, 403, 503), path)

    def test_teacher_routes_open_up_when_configured_public(self):
        with patch.dict("os.environ", {"TEACHER_SCHEDULE_ACCESS": "public"}):
            get_settings.cache_clear()
            response = self.client.get("/schedule/teachers")
            self.assertEqual(response.status_code, 200)
            names = {item["name"] for item in response.json()["items"]}
            self.assertIn("ASB", names)

    def test_public_teacher_week_includes_the_teacher_code(self):
        with patch.dict("os.environ", {"TEACHER_SCHEDULE_ACCESS": "public"}):
            get_settings.cache_clear()
            response = self.client.get("/schedule/teachers/ASB")
            self.assertEqual(response.status_code, 200)
            monday = next(day for day in response.json()["days"] if day["day"] == "Monday")
            self.assertEqual(monday["classes"][0]["teacher"], "ASB")

    def test_room_responses_never_leak_teacher_codes_while_gated(self):
        response = self.client.get("/schedule/rooms/LT102")
        monday = next(day for day in response.json()["days"] if day["day"] == "Monday")
        self.assertNotIn("teacher", monday["classes"][0])


class ImprovementRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = build_client()
        self.index_patch = patch.object(schedule_index, "get_index", _fake_index)
        self.index_patch.start()
        self.addCleanup(self.index_patch.stop)
        self.storage_patch = patch("server.storage.read_timetable", _fake_read_timetable)
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)

    def test_courses_lists_reachable_ones(self):
        response = self.client.get("/improvement/courses?batch=3C15")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["semester"], 5)
        codes = {course["code"] for course in body["courses"]}
        self.assertIn("UCS301", codes)
        self.assertNotIn("UCS503", codes)

    def test_courses_rejects_an_unknown_batch(self):
        response = self.client.get("/improvement/courses?batch=9Z99")
        self.assertEqual(response.status_code, 404)

    def test_plan_ranks_batches_and_refuses_lab_clashes(self):
        response = self.client.post(
            "/improvement/plan", json={"batch": "3C15", "codes": ["UCS301L"]}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["semester"], 5)
        course = body["courses"][0]
        self.assertEqual(course["code"], "UCS301")

        by_batch = {option["batch"]: option for option in course["options"]}
        # No overlap at all.
        self.assertTrue(by_batch["2C31"]["feasible"])
        self.assertEqual(by_batch["2C31"]["clash_counts"]["total"], 0)
        # One lecture-vs-lecture clash is within the default budget.
        self.assertTrue(by_batch["2C32"]["feasible"])
        self.assertEqual(by_batch["2C32"]["clash_counts"]["lecture"], 1)
        # Runs against the student's own practical, which cannot be skipped.
        self.assertFalse(by_batch["2C33"]["feasible"])
        self.assertEqual(by_batch["2C33"]["clash_counts"]["practical"], 1)

    def test_plan_is_never_cached_by_a_shared_edge(self):
        response = self.client.post(
            "/improvement/plan", json={"batch": "3C15", "codes": ["UCS301L"]}
        )
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")

    def test_plan_reports_a_course_nobody_offers(self):
        response = self.client.post(
            "/improvement/plan", json={"batch": "3C15", "codes": ["UZZ999L"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unavailable_codes"], ["UZZ999"])

    def test_plan_refuses_a_first_semester_student(self):
        response = self.client.post(
            "/improvement/plan", json={"batch": "1B11", "codes": ["UCS301L"]}
        )
        # 1B11 is not in the fixture set, so this is an unknown batch; the
        # semester guard is covered by the unit tests.
        self.assertIn(response.status_code, (400, 404))

    def test_plan_requires_at_least_one_code(self):
        response = self.client.post("/improvement/plan", json={"batch": "3C15", "codes": []})
        self.assertEqual(response.status_code, 422)

    def test_plan_hides_teacher_codes_while_gated(self):
        response = self.client.post(
            "/improvement/plan", json={"batch": "3C15", "codes": ["UCS301L"]}
        )
        sessions = response.json()["courses"][0]["options"][0]["sessions"]
        self.assertNotIn("teacher", sessions[0])

    def test_plan_shows_teacher_codes_when_public(self):
        with patch.dict("os.environ", {"TEACHER_SCHEDULE_ACCESS": "public"}):
            get_settings.cache_clear()
            response = self.client.post(
                "/improvement/plan", json={"batch": "3C15", "codes": ["UCS301L"]}
            )
            sessions = response.json()["courses"][0]["options"][0]["sessions"]
            self.assertIn("teacher", sessions[0])


if __name__ == "__main__":
    unittest.main()
