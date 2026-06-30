from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from timetable_api._parser.extractors.class_blocks import ClassBlockExtractor
from timetable_api._parser.serializers.json import class_blocks_to_jsonable


ClassData = dict[str, Any]
TimetableData = dict[str, dict[str, list[ClassData]]]


class TimetableStore:
    """Read-only in-memory view of parsed timetable data."""

    def __init__(
        self,
        data: TimetableData,
        source: str,
        sheets: list[str],
        batch_sources: dict[str, str] | None = None,
    ) -> None:
        self._data = data
        self._batch_sources = batch_sources or {}
        self.source = source
        self.sheets = sheets
        self.loaded_at = datetime.now(UTC).isoformat()
        self.class_count = sum(
            len(classes)
            for days in self._data.values()
            for classes in days.values()
        )

    @classmethod
    def empty(cls) -> "TimetableStore":
        return cls.from_data({}, source="pending-upload")

    @classmethod
    def from_workbook(cls, workbook_path: Path, source_name: str | None = None) -> "TimetableStore":
        if not workbook_path.is_file():
            raise FileNotFoundError(f"Timetable workbook not found: {workbook_path}")

        workbook = load_workbook(workbook_path, data_only=True)
        data: TimetableData = {}
        batch_sources: dict[str, str] = {}
        parsed_sheets: list[str] = []

        try:
            for sheet in workbook.worksheets:
                parsed = ClassBlockExtractor.extract(sheet)
                if not parsed:
                    continue

                parsed_sheets.append(sheet.title)
                cls._upsert_batches(
                    data,
                    batch_sources,
                    class_blocks_to_jsonable(parsed),
                    sheet.title,
                )
        finally:
            workbook.close()

        return cls(
            data=data,
            source=source_name or workbook_path.name,
            sheets=parsed_sheets,
            batch_sources=batch_sources,
        )

    @classmethod
    def from_data(
        cls,
        data: TimetableData,
        source: str = "in-memory",
        sheets: list[str] | None = None,
    ) -> "TimetableStore":
        source_sheets = sheets or []
        default_sheet = source_sheets[0] if len(source_sheets) == 1 else None
        batch_sources = {batch: default_sheet for batch in data if default_sheet is not None}
        return cls(
            data=deepcopy(data),
            source=source,
            sheets=source_sheets,
            batch_sources=batch_sources,
        )

    @staticmethod
    def _upsert_batches(
        target: TimetableData,
        batch_sources: dict[str, str],
        incoming: TimetableData,
        source_sheet: str,
    ) -> None:
        for batch, days in incoming.items():
            has_classes = any(classes for classes in days.values())
            if batch not in target or has_classes:
                target[batch] = days
                batch_sources[batch] = source_sheet

    @property
    def batch_count(self) -> int:
        return len(self._data)

    def batches(self) -> list[str]:
        return sorted(self._data)

    def source_sheet(self, batch: str) -> str | None:
        return self._batch_sources.get(batch.upper())

    def timetable(
        self,
        batch: str,
        day: str | None = None,
    ) -> dict[str, list[ClassData]] | None:
        days = self._data.get(batch.upper())
        if days is None:
            return None
        if day is None:
            return days
        normalized_day = day.upper()
        return {normalized_day: days.get(normalized_day, [])}

    def search_classes(
        self,
        *,
        subject_code: str | None = None,
        day: str | None = None,
        class_type: str | None = None,
        query: str | None = None,
    ) -> list[ClassData]:
        normalized_code = subject_code.upper() if subject_code else None
        normalized_day = day.upper() if day else None
        normalized_type = class_type.upper() if class_type else None
        normalized_query = query.casefold() if query else None
        matches: list[ClassData] = []

        for batch, days in self._data.items():
            for current_day, classes in days.items():
                if normalized_day and current_day != normalized_day:
                    continue
                for class_data in classes:
                    if normalized_code and not self._contains_subject(
                        class_data,
                        normalized_code,
                    ):
                        continue
                    if normalized_type and class_data["type"].upper() != normalized_type:
                        continue
                    if normalized_query and not self._contains_text(class_data, normalized_query):
                        continue
                    matches.append({"batch": batch, "day": current_day, **class_data})

        return matches

    @staticmethod
    def _contains_subject(class_data: ClassData, subject_code: str) -> bool:
        if class_data.get("subject_code") == subject_code:
            return True
        return any(
            option.get("subject_code") == subject_code
            for option in class_data.get("options", [])
        )

    @staticmethod
    def _contains_text(class_data: ClassData, query: str) -> bool:
        values = [
            class_data.get("subject_code"),
            class_data.get("subject_name"),
            *class_data.get("raw", []),
        ]
        for option in class_data.get("options", []):
            values.extend(
                [
                    option.get("subject_code"),
                    option.get("subject_name"),
                    option.get("place"),
                    option.get("teacher"),
                    *option.get("raw", []),
                ]
            )
        return any(query in str(value).casefold() for value in values if value is not None)
