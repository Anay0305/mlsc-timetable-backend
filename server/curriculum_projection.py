"""Live Curriculum Library projection for stored timetable observations.

The parser/database store only what the workbook said.  This module overlays
the current Library meaning at read time, so moving a course between Core and
an elective section takes effect immediately without another ingest.
"""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


LIBRARY_SECTION_LABELS = {
    "core": "Core Subjects",
    "elective_1": "Elective 1",
    "elective_2": "Elective 2",
    "elective_3": "Elective 3",
    "general_elective": "General Elective",
}
LIBRARY_SECTION_ORDER = tuple(LIBRARY_SECTION_LABELS)
ELECTIVE_SECTION_ORDER = tuple(kind for kind in LIBRARY_SECTION_ORDER if kind != "core")
LIBRARY_POOL_BRANCHES = frozenset({"POOL-A", "POOL-B", "POOL-C", "POOL-D"})
LIBRARY_ALL_SEMESTER_BRANCHES = frozenset({"X", "G", "J", "R"})
LIBRARY_BRANCH_INHERITANCE = {"CE-2+2": "C"}
SPECIAL_BATCH_BRANCHES = {
    "2UOQ": "CE-2+2",
    "2UNSW": "CE-2+2",
    "2TCD": "CE-2+2",
}

_LIBRARY_BRANCH_RE = re.compile(r"^[A-Z][A-Z0-9+\-]{0,15}$")
_BATCH_RE = re.compile(r"^(?P<year>\d)(?P<branch>[A-Z])")
_COURSE_TOKEN_RE = re.compile(r"([A-Z]{3}(?:\d{3}|XXX)|[A-Z]{5}\d)([LTP]?)")


@dataclass(frozen=True)
class CurriculumContext:
    batch: str
    year: int
    semester: int
    requested_branch: str
    resolved_branch: str
    requested_key: str
    resolved_key: str


def normalize_library_branch(branch: str) -> str:
    cleaned = re.sub(r"\s+", "", str(branch or "").strip().upper())
    if not _LIBRARY_BRANCH_RE.fullmatch(cleaned):
        raise ValueError("branch must be 1-16 uppercase letters/numbers (plus '+' or '-')")
    return cleaned


def library_key(branch: str, semester: int) -> str:
    clean_branch = normalize_library_branch(branch)
    try:
        clean_semester = int(semester)
    except (TypeError, ValueError) as exc:
        raise ValueError("semester must be an integer from 1 to 8") from exc
    if not 1 <= clean_semester <= 8:
        raise ValueError("semester must be an integer from 1 to 8")
    resolved = LIBRARY_BRANCH_INHERITANCE.get(clean_branch, clean_branch)
    if resolved in LIBRARY_POOL_BRANCHES:
        allowed = {1, 2}
        rule = "only supports semesters 1 and 2"
    elif resolved in LIBRARY_ALL_SEMESTER_BRANCHES:
        allowed = set(range(1, 9))
        rule = "supports semesters 1 through 8"
    else:
        allowed = set(range(3, 9))
        rule = "only supports semesters 3 through 8"
    if clean_semester not in allowed:
        raise ValueError(f"{clean_branch} {rule}")
    return f"{clean_branch}:S{clean_semester}"


def resolve_curriculum_context(batch: str, semester_label: str) -> CurriculumContext:
    code = "".join(ch for ch in str(batch or "").strip().upper() if ch.isalnum())
    match = _BATCH_RE.match(code)
    if not match:
        raise ValueError(f"cannot resolve curriculum branch for batch {batch!r}")
    year = int(match.group("year"))
    label = str(semester_label or "").strip().upper()
    if label.startswith("ODD") or label == "O":
        semester = year * 2 - 1
    elif label.startswith("EVEN") or label == "E":
        semester = year * 2
    else:
        raise ValueError(f"cannot resolve student semester from {semester_label!r}")

    if code in SPECIAL_BATCH_BRANCHES:
        requested_branch = SPECIAL_BATCH_BRANCHES[code]
    else:
        branch_letter = match.group("branch")
        requested_branch = (
            f"POOL-{branch_letter}"
            if year == 1 and branch_letter in {"A", "B", "C", "D"}
            else branch_letter
        )
    resolved_branch = LIBRARY_BRANCH_INHERITANCE.get(requested_branch, requested_branch)
    return CurriculumContext(
        batch=code,
        year=year,
        semester=semester,
        requested_branch=requested_branch,
        resolved_branch=resolved_branch,
        requested_key=library_key(requested_branch, semester),
        resolved_key=library_key(resolved_branch, semester),
    )


def base_course_code(value: Any) -> str:
    compact = "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum())
    match = _COURSE_TOKEN_RE.search(compact)
    if match:
        return match.group(1)
    if compact and compact[-1:] in {"L", "T", "P"}:
        compact = compact[:-1]
    return compact


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=False)
    return deepcopy(dict(value))


def _observed_candidates(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    options = entry.get("options")
    if isinstance(options, list) and options:
        return [_as_dict(option) for option in options if isinstance(option, Mapping) or hasattr(option, "model_dump")]
    code = entry.get("code")
    if not code:
        return []
    return [{
        "subject_code": code,
        "subject_name": entry.get("subject"),
        "type": entry.get("type") or "Unknown",
        "place": entry.get("room"),
        "teacher": entry.get("teacher"),
    }]


def _section_index(sections: Iterable[Any]) -> tuple[dict[str, str], dict[str, set[str]]]:
    by_code: dict[str, str] = {}
    by_section: dict[str, set[str]] = {kind: set() for kind in LIBRARY_SECTION_ORDER}
    for raw in sections or []:
        section = _as_dict(raw)
        kind = str(section.get("kind") or "").strip().lower()
        if kind not in LIBRARY_SECTION_LABELS:
            continue
        for raw_code in section.get("subject_codes") or []:
            code = base_course_code(raw_code)
            if not code:
                continue
            by_code[code] = kind
            by_section[kind].add(code)
    return by_code, by_section


def project_curriculum_classes(
    classes: Iterable[Any],
    *,
    context: CurriculumContext,
    sections: Iterable[Any],
    library_revision: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return live-projected classes and non-blocking Fix issues."""
    by_code, by_section = _section_index(sections)
    projected: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    section_rank = {kind: index for index, kind in enumerate(ELECTIVE_SECTION_ORDER)}

    for raw in classes:
        entry = _as_dict(raw)
        candidates = _observed_candidates(entry)
        observed_codes = [base_course_code(candidate.get("subject_code")) for candidate in candidates]
        observed_codes = [code for code in observed_codes if code]
        distinct_codes = list(dict.fromkeys(observed_codes))
        unknown = [code for code in distinct_codes if code not in by_code]
        for code in unknown:
            issues.append(_issue(
                entry,
                context=context,
                library_revision=library_revision,
                error_type="SUBJECT_NOT_IN_LIBRARY",
                subject_code=code,
                message=f"{code} is not assigned to any section in {context.requested_key}; treated as Core.",
                extra={"observed_codes": distinct_codes},
            ))

        votes = Counter(
            section
            for code in distinct_codes
            if (section := by_code.get(code)) in ELECTIVE_SECTION_ORDER
        )
        winner: str | None = None
        tied: list[str] = []
        if votes:
            top = max(votes.values())
            tied = sorted((kind for kind, count in votes.items() if count == top), key=section_rank.get)
            winner = tied[0]

        if winner is None and len(candidates) <= 1:
            entry["curriculum_section"] = by_code.get(distinct_codes[0], "core") if distinct_codes else "core"
            entry["requires_selection"] = False
            entry["elective_group_id"] = None
            projected.append(entry)
            continue

        section_votes = {kind: votes[kind] for kind in ELECTIVE_SECTION_ORDER if votes.get(kind)}
        if len(votes) > 1 or winner is None:
            issues.append(_issue(
                entry,
                context=context,
                library_revision=library_revision,
                error_type="ELECTIVE_SECTION_CONFLICT",
                subject_code=None,
                message=(
                    f"Elective cell spans multiple Library sections; {winner or 'unclassified'} selected by majority."
                    if winner else "Multi-course cell has no elective section in the Curriculum Library."
                ),
                extra={
                    "selected_section": winner,
                    "section_votes": section_votes,
                    "tied_sections": tied if len(tied) > 1 else [],
                    "observed_codes": distinct_codes,
                },
            ))

        if winner:
            expected = by_section[winner]
            observed = set(distinct_codes)
            missing = sorted(expected - observed)
            extra_codes = sorted(observed - expected)
            if missing or extra_codes:
                issues.append(_issue(
                    entry,
                    context=context,
                    library_revision=library_revision,
                    error_type="ELECTIVE_OPTION_SET_MISMATCH",
                    subject_code=None,
                    message=f"Observed elective options do not match {LIBRARY_SECTION_LABELS[winner]}.",
                    extra={
                        "section": winner,
                        "expected_codes": sorted(expected),
                        "observed_codes": distinct_codes,
                        "missing_codes": missing,
                        "extra_codes": extra_codes,
                    },
                ))

        entry.update({
            "subject": None,
            "code": None,
            "teacher": None,
            "type": "Elective",
            "room": None,
            "options": candidates,
            "curriculum_section": winner or "unclassified",
            "requires_selection": True,
            "elective_group_id": f"{context.requested_key}:{winner or 'unclassified'}",
        })
        projected.append(entry)

    return projected, issues


def _issue(
    entry: Mapping[str, Any],
    *,
    context: CurriculumContext,
    library_revision: int | None,
    error_type: str,
    subject_code: str | None,
    message: str,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "batch": context.batch,
        "day": entry.get("day"),
        "start_time": entry.get("start_time"),
        "severity": "MEDIUM",
        "code": error_type,
        "subject_code": subject_code,
        "message": message,
        "library_key": context.requested_key,
        "resolved_library_key": context.resolved_key,
        "library_revision": library_revision,
        **dict(extra),
    }
