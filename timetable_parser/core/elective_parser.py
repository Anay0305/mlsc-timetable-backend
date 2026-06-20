from __future__ import annotations

import re

from timetable_parser.core.models import ElectiveOption
from timetable_parser.core.subject_catalog import SubjectCatalog
from timetable_parser.core.subject_parser import class_type_for_subject, confidence_for_subject


SUBJECT_TOKEN_PATTERN = re.compile(r"([A-Z]{3}\d{3}|[A-Z]{5}\d)[LTP]")


def build_elective_options(raw: list[str], subject_catalog: SubjectCatalog) -> list[ElectiveOption]:
    subject_codes = find_subject_codes(raw)
    if len(subject_codes) <= 1:
        return []

    places = collect_place_candidates(raw)
    teachers = collect_teacher_candidates(raw)
    count_matches = len(places) in {0, len(subject_codes)} and len(teachers) in {0, len(subject_codes)}

    options: list[ElectiveOption] = []
    for index, subject_code in enumerate(subject_codes):
        subject_name = subject_catalog.name_for(subject_code)
        place = value_at_or_none(places, index)
        teacher = value_at_or_none(teachers, index)
        confidence = confidence_for_subject(subject_code, subject_name)
        if confidence == "Good" and not count_matches:
            confidence = "Normal"
        options.append(
            ElectiveOption(
                subject_code=subject_code,
                subject_name=subject_name,
                type=class_type_for_subject(subject_code),
                place=place,
                teacher=teacher,
                confidence=confidence,
                raw=[value for value in (subject_code, place, teacher) if value],
            )
        )

    return options


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
