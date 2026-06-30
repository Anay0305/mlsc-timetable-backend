"""Confidence scoring for internally parsed workbook records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConfidenceReasonCode(StrEnum):
    SUBJECT_CODE_NOT_DETECTED = "SUBJECT_CODE_NOT_DETECTED"
    SUBJECT_NOT_IN_CATALOG = "SUBJECT_NOT_IN_CATALOG"
    RAW_DETAILS_MISSING = "RAW_DETAILS_MISSING"
    SUSPICIOUS_PERIOD_SPAN = "SUSPICIOUS_PERIOD_SPAN"
    MALFORMED_EXCEL = "MALFORMED_EXCEL"
    ELECTIVE_MAPPING_COUNT_MISMATCH = "ELECTIVE_MAPPING_COUNT_MISMATCH"
    ELECTIVE_PLACE_NOT_DETECTED = "ELECTIVE_PLACE_NOT_DETECTED"
    ELECTIVE_TEACHER_NOT_DETECTED = "ELECTIVE_TEACHER_NOT_DETECTED"


@dataclass(frozen=True)
class ConfidenceReason:
    code: ConfidenceReasonCode
    penalty: int
    detail: str


@dataclass(frozen=True)
class ConfidenceAssessment:
    score: int
    level: str
    reasons: tuple[ConfidenceReason, ...]


def assess_confidence(
    *,
    subject_code: str | None,
    subject_name: str | None,
    raw: list[str],
    periods: int,
    malformed_excel_detail: str | None = None,
    elective_mapping_counts: tuple[int, int, int] | None = None,
    missing_elective_place: bool = False,
    missing_elective_teacher: bool = False,
) -> ConfidenceAssessment:
    reasons: list[ConfidenceReason] = []

    if subject_code is None:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.SUBJECT_CODE_NOT_DETECTED,
                penalty=55,
                detail="No value in the block matched the timetable subject-code format.",
            )
        )
    elif subject_name is None:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.SUBJECT_NOT_IN_CATALOG,
                penalty=25,
                detail=f"{subject_code} was detected but is absent from subjects.json.",
            )
        )

    non_code_values = [value for value in raw if subject_code is None or normalized(value) != subject_code]
    if subject_code is not None and not non_code_values:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.RAW_DETAILS_MISSING,
                penalty=10,
                detail="The block contains a subject code but no room, lab, or teacher details.",
            )
        )

    if periods > 4:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.SUSPICIOUS_PERIOD_SPAN,
                penalty=15,
                detail=f"The extracted block spans {periods} periods; the expected maximum is 4.",
            )
        )

    if malformed_excel_detail:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.MALFORMED_EXCEL,
                penalty=30,
                detail=malformed_excel_detail,
            )
        )

    if elective_mapping_counts is not None:
        subject_count, place_count, teacher_count = elective_mapping_counts
        counts_match = place_count in {0, subject_count} and teacher_count in {0, subject_count}
        if not counts_match:
            reasons.append(
                ConfidenceReason(
                    code=ConfidenceReasonCode.ELECTIVE_MAPPING_COUNT_MISMATCH,
                    penalty=20,
                    detail=(
                        f"Found {subject_count} subjects, {place_count} places, and "
                        f"{teacher_count} teachers in the elective block."
                    ),
                )
            )

    if missing_elective_place:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.ELECTIVE_PLACE_NOT_DETECTED,
                penalty=10,
                detail="No place could be mapped to this elective subject.",
            )
        )

    if missing_elective_teacher:
        reasons.append(
            ConfidenceReason(
                code=ConfidenceReasonCode.ELECTIVE_TEACHER_NOT_DETECTED,
                penalty=10,
                detail="No teacher could be mapped to this elective subject.",
            )
        )

    score = max(0, 100 - sum(reason.penalty for reason in reasons))
    return ConfidenceAssessment(score=score, level=confidence_level(score), reasons=tuple(reasons))


def confidence_level(score: int) -> str:
    if score >= 85:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    if score >= 30:
        return "LOW"
    return "UNRELIABLE"


def normalized(value: str) -> str:
    return value.strip().upper().replace(" ", "")
