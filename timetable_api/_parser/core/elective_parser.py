from __future__ import annotations

import re

from timetable_api._parser.core.confidence import assess_confidence
from timetable_api._parser.core.models import ElectiveOption
from timetable_api._parser.core.subject_catalog import SubjectCatalog
from timetable_api._parser.core.subject_parser import class_type_for_subject


SUBJECT_TOKEN_PATTERN = re.compile(r"([A-Z]{3}\d{3}|[A-Z]{5}\d)[LTP]")


def build_elective_options(raw: list[str], subject_catalog: SubjectCatalog) -> list[ElectiveOption]:
    subject_codes = find_subject_codes(raw)
    if len(subject_codes) <= 1:
        return []

    places = collect_place_candidates(raw)
    teachers = collect_teacher_candidates(raw)

    options: list[ElectiveOption] = []
    for index, subject_code in enumerate(subject_codes):
        subject_name = subject_catalog.name_for(subject_code)
        place = value_at_or_none(places, index)
        teacher = value_at_or_none(teachers, index)
        confidence = assess_confidence(
            subject_code=subject_code,
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
                type=class_type_for_subject(subject_code),
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
    return len(subject_codes), len(collect_place_candidates(raw)), len(collect_teacher_candidates(raw))


def find_subject_codes(raw: list[str]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalized = value.strip().upper().replace(" ", "")
        for match in SUBJECT_TOKEN_PATTERN.finditer(normalized):
            code = match.group(0)
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def collect_place_candidates(raw: list[str]) -> list[str]:
    candidates: list[str] = []
    for value in raw:
        if should_skip_metadata(value):
            continue
        for token in split_multi_value(value):
            if is_place_like(token):
                candidates.append(token)
    return candidates


def collect_teacher_candidates(raw: list[str]) -> list[str]:
    candidates: list[str] = []
    for value in raw:
        if should_skip_metadata(value):
            continue
        for token in split_multi_value(value):
            if is_teacher_like(token):
                candidates.append(token)
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
