from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


Day = Literal["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfidenceReasonResponse(ApiModel):
    code: str
    penalty: int
    detail: str


class ElectiveOptionResponse(ApiModel):
    subject_code: str
    subject_name: str | None
    type: str
    place: str | None
    teacher: str | None
    confidence: str
    confidence_score: int
    confidence_reasons: list[ConfidenceReasonResponse]
    raw: list[str]


class CellBoundsResponse(ApiModel):
    start: str
    end: str


class ClassResponse(ApiModel):
    start_time: str
    end_time: str
    start_slot: int
    periods: int
    subject_code: str | None
    subject_name: str | None
    type: str
    confidence: str
    confidence_score: int
    confidence_reasons: list[ConfidenceReasonResponse]
    block_kind: str
    options: list[ElectiveOptionResponse]
    raw: list[str]
    bounds: CellBoundsResponse


class ClassSearchResult(ClassResponse):
    batch: str
    day: Day


class HealthResponse(ApiModel):
    status: Literal["ok"]
    storage: Literal["memory"]
    batches: int
    classes: int


class BatchListResponse(ApiModel):
    count: int
    batches: list[str]


class TimetableResponse(ApiModel):
    batch: str
    source_sheet: str | None
    days: dict[Day, list[ClassResponse]]


class ClassSearchResponse(ApiModel):
    count: int
    total: int
    offset: int
    limit: int
    classes: list[ClassSearchResult]


class MetadataResponse(ApiModel):
    source: str
    loaded_at: str
    sheets: list[str]
    batches: int
    classes: int


class WorkbookUploadResponse(MetadataResponse):
    status: Literal["updated"]
