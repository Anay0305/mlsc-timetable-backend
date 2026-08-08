"""Improvement (course re-take) planning.

A student repeating an earlier course has to sit in a *lower* batch's classes
for it while still attending their own semester. Today they do that by opening
each junior batch's timetable and eyeballing the clashes. This module does the
search instead: given the student's batch and the courses they are repeating,
it reports every junior batch whose sessions they could realistically attend.

Two rules shape the search.

**Which semesters are reachable.** Semesters run in parity lockstep — in an odd
term every batch is in an odd semester — so a 5th-semester student can never
attend a 4th-semester course; it simply is not running. Semesters 1 and 2 are
treated as one first-year pool because the first-year pools rotate their
courses between the two halves of the year.

**What counts as an acceptable clash.** A practical cannot be skipped, so any
overlap involving one disqualifies the offering. A lecture or tutorial can be
missed a bounded number of times. The bounds are configuration, not policy
baked into code, because the college's tolerance changes.

A clash is graded by the *stronger* of the two classes involved: if the
student's own lab runs against the improvement lecture, that is a practical
clash, because the lab is the one they cannot skip and the lecture is the one
they would lose every single week.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from server.config import Settings, get_settings
from server.curriculum_projection import base_course_code
from server.schedule_index import (
    DAY_ORDER,
    Occupancy,
    ScheduleIndex,
    parse_minute,
    severity_for_type,
    severity_name,
    SEVERITY_LECTURE,
    SEVERITY_PRACTICAL,
    SEVERITY_TUTORIAL,
)

FIRST_YEAR_SEMESTERS = frozenset({1, 2})


class ImprovementError(Exception):
    """Raised when a request cannot be planned at all."""


@dataclass(frozen=True)
class ClashLimits:
    max_lecture: int = 1
    max_tutorial: int = 1
    max_practical: int = 0

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ClashLimits":
        settings = settings or get_settings()
        return cls(
            max_lecture=settings.improvement_max_lecture_clashes,
            max_tutorial=settings.improvement_max_tutorial_clashes,
            max_practical=settings.improvement_max_practical_clashes,
        )

    def allows(self, tally: Mapping[int, int]) -> bool:
        return (
            tally.get(SEVERITY_LECTURE, 0) <= self.max_lecture
            and tally.get(SEVERITY_TUTORIAL, 0) <= self.max_tutorial
            and tally.get(SEVERITY_PRACTICAL, 0) <= self.max_practical
        )

    def public_dict(self) -> dict[str, int]:
        return {
            "max_lecture_clashes": self.max_lecture,
            "max_tutorial_clashes": self.max_tutorial,
            "max_practical_clashes": self.max_practical,
        }


@dataclass(frozen=True)
class BusyBlock:
    """One window in which the student is already committed."""

    day: str
    start_minute: int
    end_minute: int
    start_time: str
    end_time: str
    severity: int
    label: str | None
    code: str | None
    uncertain: bool  # an unresolved elective: severity is the worst case

    def overlaps(self, start_minute: int, end_minute: int) -> bool:
        return self.start_minute < end_minute and start_minute < self.end_minute

    def public_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "code": self.code,
            "subject": self.label,
            "type": severity_name(self.severity),
            "uncertain": self.uncertain,
        }


def is_reachable_semester(
    student_semester: int,
    candidate_semester: int,
    *,
    pool_first_year: bool = True,
) -> bool:
    """Can a student in ``student_semester`` attend ``candidate_semester``?

    Only strictly earlier semesters, and only those actually running alongside
    the student's own — which means matching parity. First-year semesters are
    pooled when ``pool_first_year`` is set, because the first-year pools swap
    their course sets between semesters 1 and 2.
    """
    if candidate_semester >= student_semester:
        return False
    if pool_first_year and candidate_semester in FIRST_YEAR_SEMESTERS:
        return True
    return candidate_semester % 2 == student_semester % 2


def busy_blocks_from_classes(classes: Iterable[Any]) -> list[BusyBlock]:
    """Turn a student's own timetable into the windows they cannot leave.

    An elective cell the student has not resolved yet blocks its slot at the
    worst severity among its options and is marked ``uncertain`` — picking the
    lecture option may free the slot from the practical rule, and the caller
    can say so rather than silently rejecting good offerings.
    """
    blocks: list[BusyBlock] = []
    for raw in classes or []:
        entry = raw.model_dump(exclude_none=False) if hasattr(raw, "model_dump") else dict(raw)
        day = str(entry.get("day") or "").strip()
        start_minute = parse_minute(entry.get("start_time"))
        end_minute = parse_minute(entry.get("end_time"))
        if not day or start_minute is None or end_minute is None or end_minute <= start_minute:
            continue

        options = entry.get("options")
        if isinstance(options, list) and len(options) > 1:
            severities = {
                severity_for_type(option.get("type"))
                for option in options
                if isinstance(option, Mapping)
            }
            severity = max(severities) if severities else SEVERITY_LECTURE
            uncertain = len(severities) > 1
            label = entry.get("subject") or "Elective"
            code = entry.get("code")
        else:
            severity = severity_for_type(entry.get("type"))
            uncertain = False
            label = entry.get("subject")
            code = entry.get("code")

        blocks.append(
            BusyBlock(
                day=day,
                start_minute=start_minute,
                end_minute=end_minute,
                start_time=str(entry.get("start_time")),
                end_time=str(entry.get("end_time")),
                severity=severity,
                label=str(label) if label else None,
                code=str(code) if code else None,
                uncertain=uncertain,
            )
        )
    return blocks


def _session_block(item: Occupancy) -> BusyBlock:
    return BusyBlock(
        day=item.day,
        start_minute=item.start_minute,
        end_minute=item.end_minute,
        start_time=item.start_time,
        end_time=item.end_time,
        severity=item.severity,
        label=item.subject,
        code=item.code,
        uncertain=False,
    )


def _clashes(
    sessions: Sequence[Occupancy],
    blocks: Sequence[BusyBlock],
    *,
    include_teacher: bool,
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Pair every offering session against every commitment it overlaps."""
    found: list[dict[str, Any]] = []
    tally: dict[int, int] = {}
    for session in sessions:
        for block in blocks:
            if block.day != session.day:
                continue
            if not block.overlaps(session.start_minute, session.end_minute):
                continue
            severity = max(session.severity, block.severity)
            tally[severity] = tally.get(severity, 0) + 1
            found.append(
                {
                    "day": session.day,
                    "start_time": session.start_time,
                    "end_time": session.end_time,
                    "severity": severity_name(severity),
                    "blocking": severity == SEVERITY_PRACTICAL,
                    "uncertain": block.uncertain,
                    "improvement_class": session.public_dict(include_teacher=include_teacher),
                    "your_class": block.public_dict(),
                }
            )
    return found, tally


def resolve_code(index: ScheduleIndex, code: str) -> str:
    """Map a course code onto the key the index actually uses.

    ``base_course_code`` is not idempotent for the non-standard codes in this
    data — it strips a trailing L/T/P from anything it does not recognise, so
    ``BEST`` becomes ``BES`` on a second pass. Callers hand us both raw codes
    from a timetable and normalized ones echoed back from our own responses, so
    prefer an exact hit before normalizing.
    """
    raw = str(code or "").strip().upper()
    if raw and raw in index.by_code:
        return raw
    return base_course_code(raw)


def _ordered(items: Iterable[Occupancy]) -> list[Occupancy]:
    """Sessions in reading order: Monday first, then by start time."""
    return sorted(
        items,
        key=lambda item: (
            DAY_ORDER.index(item.day) if item.day in DAY_ORDER else len(DAY_ORDER),
            item.start_minute,
        ),
    )


def _signature(sessions: Iterable[Occupancy]) -> tuple:
    """What a student would actually sit through, ignoring which batch it is."""
    return tuple(
        sorted(
            (item.day, item.start_minute, item.end_minute, item.code or "", item.type)
            for item in sessions
        )
    )


def _group_equivalent(grouped: Mapping[str, list[Occupancy]]) -> dict[tuple, list[str]]:
    """Collapse batches that attend exactly the same sessions.

    Parallel batches like 2H21/2H22/2H2A sit in one lecture together, so
    offering them as separate choices produces pages of identical options. They
    are interchangeable to the student, so present them as one.
    """
    equivalent: dict[tuple, list[str]] = {}
    for batch, sessions in grouped.items():
        equivalent.setdefault(_signature(sessions), []).append(batch)
    return {signature: sorted(batches) for signature, batches in equivalent.items()}


def _dedupe_sessions(items: Iterable[Occupancy]) -> list[Occupancy]:
    """Collapse the same class booked into more than one room.

    A cell reading ``LT101/LT102`` indexes as two occupancies so both rooms
    show as busy, but a student attends it once. Counting both would double
    every clash it causes.
    """
    seen: dict[tuple, Occupancy] = {}
    for item in items:
        key = (item.day, item.start_minute, item.end_minute, item.code, item.type)
        if key not in seen:
            seen[key] = item
    return list(seen.values())


def offerings_for_code(
    index: ScheduleIndex,
    code: str,
    *,
    student_semester: int,
    pool_first_year: bool = True,
    exclude_batches: Iterable[str] = (),
) -> dict[str, list[Occupancy]]:
    """Group a course's sessions by the junior batch that attends them.

    A candidate batch's offering is every session of the course that batch
    sits in, which is what the student would join.
    """
    normalized = resolve_code(index, code)
    excluded = {str(value).strip().upper() for value in exclude_batches}
    grouped: dict[str, list[Occupancy]] = {}
    for item in index.by_code.get(normalized, ()):
        for batch in item.batches:
            if batch in excluded:
                continue
            semester = index.batch_semester.get(batch)
            if semester is None:
                continue
            if not is_reachable_semester(
                student_semester, semester, pool_first_year=pool_first_year
            ):
                continue
            grouped.setdefault(batch, []).append(item)
    return {batch: _dedupe_sessions(items) for batch, items in grouped.items()}


def available_courses(
    index: ScheduleIndex,
    *,
    student_batch: str,
    student_semester: int,
    pool_first_year: bool = True,
) -> list[dict[str, Any]]:
    """Every course a student could sit for improvement, with its semesters."""
    batch = str(student_batch or "").strip().upper()
    catalog: dict[str, dict[str, Any]] = {}
    for normalized, items in index.by_code.items():
        for item in items:
            for candidate in item.batches:
                if candidate == batch:
                    continue
                semester = index.batch_semester.get(candidate)
                if semester is None:
                    continue
                if not is_reachable_semester(
                    student_semester, semester, pool_first_year=pool_first_year
                ):
                    continue
                record = catalog.setdefault(
                    normalized,
                    {
                        "code": normalized,
                        "subject": item.subject,
                        "semesters": set(),
                        "batches": set(),
                        "types": set(),
                    },
                )
                if not record["subject"] and item.subject:
                    record["subject"] = item.subject
                record["semesters"].add(semester)
                record["batches"].add(candidate)
                record["types"].add(item.type)

    return sorted(
        (
            {
                "code": record["code"],
                "subject": record["subject"],
                "semesters": sorted(record["semesters"]),
                "batch_count": len(record["batches"]),
                "types": sorted(record["types"]),
            }
            for record in catalog.values()
        ),
        key=lambda record: (record["semesters"][0] if record["semesters"] else 99, record["code"]),
    )


def evaluate_course(
    index: ScheduleIndex,
    *,
    code: str,
    student_batch: str,
    student_semester: int,
    blocks: Sequence[BusyBlock],
    limits: ClashLimits,
    pool_first_year: bool = True,
    include_teacher: bool = True,
) -> dict[str, Any]:
    """Rank every junior batch offering ``code`` for one student."""
    normalized = resolve_code(index, code)
    grouped = offerings_for_code(
        index,
        normalized,
        student_semester=student_semester,
        pool_first_year=pool_first_year,
        exclude_batches=[student_batch],
    )

    options: list[dict[str, Any]] = []
    for signature, batches in _group_equivalent(grouped).items():
        ordered = _ordered(grouped[batches[0]])
        found, tally = _clashes(ordered, blocks, include_teacher=include_teacher)
        feasible = limits.allows(tally)
        options.append(
            {
                "batches": batches,
                "batch": batches[0],
                "semester": index.batch_semester.get(batches[0]),
                "feasible": feasible,
                "clash_counts": {
                    "lecture": tally.get(SEVERITY_LECTURE, 0),
                    "tutorial": tally.get(SEVERITY_TUTORIAL, 0),
                    "practical": tally.get(SEVERITY_PRACTICAL, 0),
                    "total": sum(tally.values()),
                },
                "has_uncertain_clash": any(clash["uncertain"] for clash in found),
                "sessions": [
                    item.public_dict(include_teacher=include_teacher) for item in ordered
                ],
                "clashes": found,
            }
        )

    options.sort(
        key=lambda option: (
            not option["feasible"],
            option["clash_counts"]["practical"],
            option["clash_counts"]["total"],
            option["batch"],
        )
    )
    subject = next(
        (item.subject for item in index.by_code.get(normalized, ()) if item.subject),
        None,
    )
    return {
        "code": normalized,
        "subject": subject,
        "offered": bool(grouped),
        "feasible_count": sum(1 for option in options if option["feasible"]),
        "options": options,
    }


def _plan_search(
    courses: Sequence[dict[str, Any]],
    base_blocks: Sequence[BusyBlock],
    limits: ClashLimits,
    *,
    index: ScheduleIndex,
    include_teacher: bool,
    max_plans: int,
) -> list[dict[str, Any]]:
    """Pick one batch per course such that every pick stays inside the budget.

    Each course is checked against the student's own timetable *plus* the
    offerings already chosen, so two improvement courses cannot be scheduled
    on top of each other.
    """
    plans: list[dict[str, Any]] = []
    # Fewest viable options first: the most constrained course prunes hardest.
    ordered_courses = sorted(courses, key=lambda course: len(course["viable"]))

    def recurse(depth: int, blocks: list[BusyBlock], picks: list[dict[str, Any]]) -> None:
        if len(plans) >= max_plans:
            return
        if depth == len(ordered_courses):
            if picks:
                plans.append(
                    {
                        "picks": [dict(pick) for pick in picks],
                        "total_clashes": sum(pick["clash_counts"]["total"] for pick in picks),
                        "practical_clashes": sum(
                            pick["clash_counts"]["practical"] for pick in picks
                        ),
                    }
                )
            return

        course = ordered_courses[depth]
        for batches, sessions in course["viable"]:
            found, tally = _clashes(sessions, blocks, include_teacher=include_teacher)
            if not limits.allows(tally):
                continue
            pick = {
                "code": course["code"],
                "subject": course["subject"],
                "batches": batches,
                "batch": batches[0],
                "semester": index.batch_semester.get(batches[0]),
                "clash_counts": {
                    "lecture": tally.get(SEVERITY_LECTURE, 0),
                    "tutorial": tally.get(SEVERITY_TUTORIAL, 0),
                    "practical": tally.get(SEVERITY_PRACTICAL, 0),
                    "total": sum(tally.values()),
                },
                "sessions": [
                    item.public_dict(include_teacher=include_teacher) for item in sessions
                ],
                "clashes": found,
            }
            recurse(
                depth + 1,
                blocks + [_session_block(item) for item in sessions],
                picks + [pick],
            )
            if len(plans) >= max_plans:
                return

    recurse(0, list(base_blocks), [])
    plans.sort(key=lambda plan: (plan["practical_clashes"], plan["total_clashes"]))
    return plans


def plan_improvements(
    index: ScheduleIndex,
    *,
    student_batch: str,
    student_semester: int,
    student_classes: Iterable[Any],
    codes: Sequence[str],
    settings: Settings | None = None,
    include_teacher: bool = True,
) -> dict[str, Any]:
    """Full improvement plan: per-course options plus combined timetables."""
    settings = settings or get_settings()
    limits = ClashLimits.from_settings(settings)
    pool_first_year = settings.improvement_pool_first_year_semesters
    batch = str(student_batch or "").strip().upper()

    blocks = busy_blocks_from_classes(student_classes)
    normalized_codes: list[str] = []
    for raw in codes:
        code = resolve_code(index, raw)
        if code and code not in normalized_codes:
            normalized_codes.append(code)
    if not normalized_codes:
        raise ImprovementError("at least one course code is required")

    results = [
        evaluate_course(
            index,
            code=code,
            student_batch=batch,
            student_semester=student_semester,
            blocks=blocks,
            limits=limits,
            pool_first_year=pool_first_year,
            include_teacher=include_teacher,
        )
        for code in normalized_codes
    ]

    # Only courses with at least one workable batch can take part in a plan.
    planable = []
    for result in results:
        grouped = offerings_for_code(
            index,
            result["code"],
            student_semester=student_semester,
            pool_first_year=pool_first_year,
            exclude_batches=[batch],
        )
        viable = [
            (option["batches"], _ordered(grouped.get(option["batch"], [])))
            for option in result["options"]
            if option["feasible"]
        ]
        if viable:
            planable.append(
                {"code": result["code"], "subject": result["subject"], "viable": viable}
            )

    plans: list[dict[str, Any]] = []
    if len(planable) == len(normalized_codes) and planable:
        plans = _plan_search(
            planable,
            blocks,
            limits,
            index=index,
            include_teacher=include_teacher,
            max_plans=settings.improvement_max_plan_options,
        )

    unavailable = [result["code"] for result in results if not result["offered"]]
    blocked = [
        result["code"]
        for result in results
        if result["offered"] and result["feasible_count"] == 0
    ]

    return {
        "batch": batch,
        "semester": student_semester,
        "semester_label": index.semester_label,
        "limits": limits.public_dict(),
        "first_year_semesters_pooled": pool_first_year,
        "courses": results,
        "plans": plans,
        "unavailable_codes": unavailable,
        "blocked_codes": blocked,
        "has_unresolved_electives": any(block.uncertain for block in blocks),
    }
