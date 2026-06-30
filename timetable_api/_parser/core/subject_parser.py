"""Subject token parsing for internal workbook ingestion."""

from __future__ import annotations

import re


SUBJECT_CODE_PATTERN = re.compile(r"^([A-Z]{3}\d{3}|[A-Z]{5}\d)[LTP]$")

CLASS_TYPE_BY_SUFFIX = {
    "L": "LECTURE",
    "T": "TUTORIAL",
    "P": "PRACTICAL",
}


def find_subject_code(raw: list[str]) -> str | None:
    for value in raw:
        candidate = value.strip().upper().replace(" ", "")
        if SUBJECT_CODE_PATTERN.fullmatch(candidate):
            return candidate
    return None


def class_type_for_subject(subject_code: str | None) -> str:
    if subject_code is None:
        return "UNKNOWN"
    return CLASS_TYPE_BY_SUFFIX.get(subject_code[-1], "UNKNOWN")
