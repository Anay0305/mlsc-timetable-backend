from __future__ import annotations

import re

from timetable_parser.core.confidence import assess_confidence
from timetable_parser.core.models import ElectiveOption
from timetable_parser.core.subject_catalog import SubjectCatalog
from timetable_parser.core.subject_parser import class_type_for_subject


# TODO(temporary): the XXX alternative accepts placeholder codes like UMCXXX.
# The L/T/P suffix is optional (group 2); a code written without one defaults to
# a lecture, mirroring find_subject_code in subject_parser.
SUBJECT_TOKEN_PATTERN = re.compile(r"([A-Z]{3}(?:\d{3}|XXX)|[A-Z]{5}\d)([LTP]?)")


def build_elective_options(raw: list[str], subject_catalog: SubjectCatalog) -> list[ElectiveOption]:
    subject_codes = find_subject_codes(raw)
    if len(subject_codes) <= 1:
        return []

    places, teachers = resolve_places_teachers(raw, len(subject_codes))

    options: list[ElectiveOption] = []
    for index, subject_code in enumerate(subject_codes):
        # subject_code may carry attached text (e.g. group tag "ULC512P(G1)");
        # look the bare code up in the catalog and for the class-type suffix.
        bare_code = bare_subject_code(subject_code)
        subject_name = subject_catalog.name_for(bare_code)
        place = value_at_or_none(places, index)
        teacher = value_at_or_none(teachers, index)
        confidence = assess_confidence(
            subject_code=bare_code,
            subject_name=subject_name,
            raw=[value for value in (subject_code, place, teacher) if value],
            periods=1,
            elective_mapping_counts=(len(subject_codes), len(places), len(teachers)),
            missing_elective_place=bool(places) and place is None,
            missing_elective_teacher=bool(teachers) and teacher is None,
        )
        options.append(
            ElectiveOption(
                subject_code=subject_code,
                subject_name=subject_name,
                type=class_type_for_subject(bare_code),
                place=place,
                teacher=teacher,
                confidence=confidence.level,
                confidence_score=confidence.score,
                confidence_reasons=confidence.reasons,
                raw=[value for value in (subject_code, place, teacher) if value],
            )
        )

    return options


def elective_mapping_counts(raw: list[str]) -> tuple[int, int, int] | None:
    subject_codes = find_subject_codes(raw)
    if len(subject_codes) <= 1:
        return None
    places, teachers = resolve_places_teachers(raw, len(subject_codes))
    return len(subject_codes), len(places), len(teachers)


def find_subject_codes(raw: list[str]) -> list[str]:
    """Return the elective subject codes, preserving any text attached to a code.

    Each ``/``-separated segment that contains a subject code is kept whole
    (e.g. the group tag in ``ULC512P(G1)`` survives), so two segments that share
    a bare code but differ in their attached text stay distinct — matching the
    number of rooms/teachers listed alongside them. Repeats within a single cell
    value are kept (they are separate positional slots); an identical code that
    reappears in a *later* raw value is skipped, preserving the old de-dup guard.
    """
    codes: list[str] = []
    seen_prior: set[str] = set()
    for value in raw:
        segments = split_multi_value(value)
        # A group of codes usually shares one L/T/P suffix written only on the
        # last one (e.g. "UCS546/URA411/URA732/URA414P" is all Practical). Use the
        # last explicit suffix in the value for every code that omits its own,
        # falling back to Lecture when none of them carry a suffix.
        default_suffix = "L"
        for segment in segments:
            for token_match in SUBJECT_TOKEN_PATTERN.finditer(segment.upper().replace(" ", "")):
                if token_match.group(2):
                    default_suffix = token_match.group(2)
        this_value: set[str] = set()
        for segment in segments:
            normalized = segment.strip().upper().replace(" ", "")
            matches = list(SUBJECT_TOKEN_PATTERN.finditer(normalized))
            if not matches:
                continue
            # One code in this segment: keep the whole thing so attached text
            # (e.g. a group tag) survives. Several codes jammed together (a
            # missing "/" between them) are split back into bare codes so their
            # count still matches the rooms/teachers listed alongside.
            if len(matches) == 1:
                match = matches[0]
                if match.group(2):
                    candidates = [normalized]
                else:
                    # inject the shared suffix right after the bare code, keeping
                    # any attached text (e.g. a group tag) that follows it.
                    candidates = [normalized[: match.end(1)] + default_suffix + normalized[match.end(1) :]]
            else:
                candidates = [m.group(1) + (m.group(2) or default_suffix) for m in matches]
            for candidate in candidates:
                if candidate in seen_prior:
                    continue
                codes.append(candidate)
                this_value.add(candidate)
        seen_prior |= this_value
    return codes


def bare_subject_code(subject_code: str) -> str:
    """Strip any attached text from a preserved code: ``ULC512P(G1)`` -> ``ULC512P``."""
    match = SUBJECT_TOKEN_PATTERN.search(subject_code.upper().replace(" ", ""))
    return match.group(0) if match else subject_code


# A "---" cell marks an elective slot that has no room/teacher of its own. It is
# kept as an explicit placeholder (not dropped) so the room/teacher counts still
# line up with the subject codes listed alongside.
PLACEHOLDER_PATTERN = re.compile(r"^-{2,}$")
NOT_GIVEN = "Not Given"


def resolve_places_teachers(raw: list[str], code_count: int) -> tuple[list[str], list[str]]:
    """Return (places, teachers) aligned to the subject codes.

    Prefers positional line mapping (rooms and teachers sit on their own
    ``/``-separated lines, room line above teacher line) which keeps ambiguous
    tokens like ``TA9`` in the column they belong to. Falls back to the
    heuristic token collectors when the block is not cleanly columnar.
    """
    positional = _positional_places_teachers(raw, code_count)
    if positional is not None:
        return positional
    return collect_place_candidates(raw), collect_teacher_candidates(raw)


def _positional_places_teachers(
    raw: list[str], code_count: int
) -> tuple[list[str], list[str]] | None:
    """Map rooms/teachers by column position, or None if the block isn't columnar.

    Only commits when the detail lines (everything except the subject code line
    and the ``LAB`` marker) are one or two lines that each split into exactly
    ``code_count`` tokens. Order decides role: the earlier line is the rooms, the
    later one the teachers — the room block sits above the teacher block in the
    spreadsheet cell.
    """
    detail = [
        tokens
        for value in raw
        if not should_skip_metadata(value)
        for tokens in (split_multi_value(value),)
        if tokens
    ]
    if not detail or len(detail) > 2:
        return None
    if any(len(tokens) != code_count for tokens in detail):
        return None
    if len(detail) == 2:
        return _resolve_placeholders(detail[0]), _resolve_placeholders(detail[1])
    # A single detail line: fall back to the heuristic to tell rooms from teachers.
    tokens = detail[0]
    place_score = sum(is_place_like(token) for token in tokens)
    teacher_score = sum(is_teacher_like(token) for token in tokens)
    if teacher_score > place_score:
        return [], _resolve_placeholders(tokens)
    return _resolve_placeholders(tokens), []


def _resolve_placeholders(tokens: list[str]) -> list[str]:
    return [NOT_GIVEN if PLACEHOLDER_PATTERN.match(token) else token for token in tokens]


def collect_place_candidates(raw: list[str]) -> list[str]:
    candidates: list[str] = []
    for value in raw:
        if should_skip_metadata(value):
            continue
        tokens = split_multi_value(value)
        # Only honour placeholders on a line that actually lists rooms, so a
        # "---" in the teacher line doesn't leak into the place list.
        line_has_place = any(is_place_like(token) for token in tokens)
        for token in tokens:
            if is_place_like(token):
                candidates.append(token)
            elif line_has_place and PLACEHOLDER_PATTERN.match(token):
                candidates.append(NOT_GIVEN)
    return candidates


def collect_teacher_candidates(raw: list[str]) -> list[str]:
    candidates: list[str] = []
    for value in raw:
        if should_skip_metadata(value):
            continue
        tokens = split_multi_value(value)
        line_has_teacher = any(is_teacher_like(token) for token in tokens)
        for token in tokens:
            if is_teacher_like(token):
                candidates.append(token)
            elif line_has_teacher and PLACEHOLDER_PATTERN.match(token):
                candidates.append(NOT_GIVEN)
    return candidates


def split_multi_value(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[/\n]", value) if token.strip()]


def should_skip_metadata(value: str) -> bool:
    normalized = value.strip().upper().replace(" ", "")
    if not normalized or normalized == "LAB":
        return True
    return bool(SUBJECT_TOKEN_PATTERN.search(normalized))


def is_place_like(value: str) -> bool:
    upper = value.upper()
    if re.search(r"\d", upper) and re.search(r"[A-Z]", upper):
        return True
    return any(marker in upper for marker in ("LAB", "LT", "LP", "GC-", "FIST"))


def is_teacher_like(value: str) -> bool:
    upper = value.upper()
    if len(upper) > 18 or re.search(r"\d", upper):
        return False
    return bool(re.fullmatch(r"[A-Z]{2,5}(?:[-/][A-Z]{1,5})*", upper))


def value_at_or_none(values: list[str], index: int) -> str | None:
    if index < len(values):
        return values[index]
    return None
