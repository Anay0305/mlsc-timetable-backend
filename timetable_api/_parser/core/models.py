from __future__ import annotations

from dataclasses import dataclass

from timetable_api._parser.core.confidence import ConfidenceReason


@dataclass(frozen=True)
class CellBounds:
    min_row: int
    min_col: int
    max_row: int
    max_col: int


@dataclass(frozen=True)
class RawCell:
    coordinate: str
    row: int
    column: int
    value: str


@dataclass(frozen=True)
class ElectiveOption:
    subject_code: str
    subject_name: str | None
    type: str
    place: str | None
    teacher: str | None
    confidence: str
    confidence_score: int
    confidence_reasons: tuple[ConfidenceReason, ...]
    raw: list[str]


@dataclass(frozen=True)
class ClassBlock:
    batch: str
    day: str
    start_slot: int
    periods: int
    start_time: str
    end_time: str
    subject_code: str | None
    subject_name: str | None
    type: str
    confidence: str
    confidence_score: int
    confidence_reasons: tuple[ConfidenceReason, ...]
    block_kind: str
    options: list[ElectiveOption]
    bounds: CellBounds
    raw: list[str]
    cells: list[RawCell]
