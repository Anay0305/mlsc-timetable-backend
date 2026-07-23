from __future__ import annotations

import re


# TODO(temporary): the XXX alternative accepts placeholder codes like UMCXXX.
SUBJECT_CODE_PATTERN = re.compile(r"^([A-Z]{3}(?:\d{3}|XXX)|[A-Z]{5}\d)([LTP]?)$")

CLASS_TYPE_BY_SUFFIX = {
    "L": "LECTURE",
    "T": "TUTORIAL",
    "P": "PRACTICAL",
}

# Free-text capstone cells ("CAPSTONE PROJECT", "CAPSTONE LECTURE", "CAPSTONE L")
# carry no subject code; detect them so they parse as capstone blocks instead of
# SUBJECT_CODE_NOT_DETECTED errors.
CAPSTONE_PATTERN = re.compile(r"^CAPSTONE(PROJECT|LECTURE|L)?$")
CAPSTONE_CODE = "CAPSTONE"
CAPSTONE_NAME = "Capstone Project"


def find_capstone_type(raw: list[str]) -> str | None:
    """Return the class type of a capstone block, or None if not a capstone cell."""
    for value in raw:
        candidate = value.strip().upper().replace(" ", "")
        match = CAPSTONE_PATTERN.fullmatch(candidate)
        if match:
            return "LECTURE" if match.group(1) in ("LECTURE", "L") else "UNKNOWN"
    return None


def find_subject_code(raw: list[str]) -> str | None:
    for value in raw:
        candidate = value.strip().upper().replace(" ", "")
        match = SUBJECT_CODE_PATTERN.fullmatch(candidate)
        if match:
            # Bare codes without an L/T/P suffix default to lecture.
            return candidate if match.group(2) else candidate + "L"
    return None


def class_type_for_subject(subject_code: str | None) -> str:
    if subject_code is None:
        return "UNKNOWN"
    return CLASS_TYPE_BY_SUFFIX.get(subject_code[-1], "UNKNOWN")
