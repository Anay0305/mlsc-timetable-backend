from __future__ import annotations

from openpyxl.cell.cell import Cell
from openpyxl.worksheet.worksheet import Worksheet

from timetable_api._parser.core.confidence import assess_confidence
from timetable_api._parser.core.elective_parser import (
    build_elective_options,
    elective_mapping_counts,
    find_subject_codes,
)
from timetable_api._parser.core.models import CellBounds, ClassBlock, RawCell
from timetable_api._parser.core.sheet_geometry import (
    build_merge_bounds,
    dedupe_raw_cells,
    dedupe_values,
    end_time,
    malformed_excel_detail,
    raw_cells_in_bounds,
    rectangle_has_raw,
    row_has_bottom_border,
    row_has_top_border,
    slot_end_row,
    visible_bounds_for_cell,
)
from timetable_api._parser.core.subject_catalog import (
    SubjectCatalog,
    load_default_subject_catalog,
)
from timetable_api._parser.core.subject_parser import (
    class_type_for_subject,
    find_subject_code,
)
from timetable_api._parser.extractors.batch import BatchExtractor
from timetable_api._parser.extractors.day_slots import DaySlotExtractor, Slot


class ClassBlockExtractor:
    """Extract raw class blocks using batch columns and SR NO slot rows."""

    @classmethod
    def extract(
        cls,
        sheet: Worksheet,
        subject_catalog: SubjectCatalog | None = None,
    ) -> dict[str, dict[str, list[ClassBlock]]]:
        subject_catalog = subject_catalog or load_default_subject_catalog()
        batch_cells = cls.find_batch_cells(sheet)
        day_schedules = DaySlotExtractor.extract(sheet)
        merge_bounds = build_merge_bounds(sheet)
        result: dict[str, dict[str, list[ClassBlock]]] = {
            BatchExtractor.normalize(cell.value): {day: [] for day in day_schedules}
            for cell in batch_cells
        }

        for batch_cell in batch_cells:
            batch = BatchExtractor.normalize(batch_cell.value)
            if batch is None:
                continue

            for day, schedule in day_schedules.items():
                consumed_slots: set[int] = set()
                ordered_slots = sorted(schedule.slots.values(), key=lambda slot: slot.sr_no)

                for index, slot in enumerate(ordered_slots):
                    if slot.sr_no in consumed_slots:
                        continue

                    # A merged cell can span multiple SR NO rows, so one successful block may consume
                    # several following slots for this batch/day pair.
                    block = cls.extract_from_slot(
                        sheet=sheet,
                        merge_bounds=merge_bounds,
                        batch=batch,
                        batch_col=batch_cell.column,
                        day=day,
                        start_slot=slot,
                        remaining_slots=ordered_slots[index:],
                        subject_catalog=subject_catalog,
                    )
                    if block is None:
                        continue

                    for sr_no in range(block.start_slot, block.start_slot + block.periods):
                        consumed_slots.add(sr_no)
                    result[batch][day].append(block)

        return result

    @classmethod
    def extract_from_slot(
        cls,
        sheet: Worksheet,
        merge_bounds: dict[tuple[int, int], CellBounds],
        batch: str,
        batch_col: int,
        day: str,
        start_slot: Slot,
        remaining_slots: list[Slot],
        subject_catalog: SubjectCatalog,
    ) -> ClassBlock | None:
        base_bounds = visible_bounds_for_cell(merge_bounds, start_slot.cell.row, batch_col)
        first_slot_end = slot_end_row(start_slot, remaining_slots, 0)
        if not rectangle_has_raw(sheet, start_slot.cell.row, first_slot_end, base_bounds):
            return None

        periods = 0
        raw_cells: list[RawCell] = []
        max_row = start_slot.cell.row

        for offset, slot in enumerate(remaining_slots):
            if slot.sr_no != start_slot.sr_no + offset:
                break
            if offset > 0 and row_has_top_border(sheet, slot.cell.row, base_bounds):
                break

            period_end_row = slot_end_row(slot, remaining_slots, offset)
            if not rectangle_has_raw(sheet, slot.cell.row, period_end_row, base_bounds):
                break

            # Keep extending while adjacent slot rows belong to the same visible class box.
            periods += 1
            max_row = period_end_row
            raw_cells.extend(raw_cells_in_bounds(sheet, slot.cell.row, period_end_row, base_bounds))
            if row_has_bottom_border(sheet, period_end_row, base_bounds):
                break

        if periods == 0:
            return None

        raw_cells = dedupe_raw_cells(raw_cells)
        raw = dedupe_values(cell.value for cell in raw_cells)
        if not raw:
            return None

        # Elective cells can contain several subject codes; the primary code anchors the
        # block while options preserve the individual subject/place/teacher combinations.
        subject_codes = find_subject_codes(raw)
        subject_code = subject_codes[0] if subject_codes else find_subject_code(raw)
        subject_name = subject_catalog.name_for(subject_code)
        options = build_elective_options(raw, subject_catalog)
        final_bounds = CellBounds(
            min_row=start_slot.cell.row,
            min_col=base_bounds.min_col,
            max_row=max_row,
            max_col=base_bounds.max_col,
        )
        confidence = assess_confidence(
            subject_code=subject_code,
            subject_name=subject_name,
            raw=raw,
            periods=periods,
            malformed_excel_detail=malformed_excel_detail(
                sheet=sheet,
                bounds=final_bounds,
                merge_bounds=merge_bounds,
                periods=periods,
            ),
            elective_mapping_counts=elective_mapping_counts(raw),
        )
        return ClassBlock(
            batch=batch,
            day=day,
            start_slot=start_slot.sr_no,
            periods=periods,
            start_time=start_slot.time,
            end_time=end_time(start_slot.time, periods),
            subject_code=subject_code,
            subject_name=subject_name,
            type=class_type_for_subject(subject_code),
            confidence=confidence.level,
            confidence_score=confidence.score,
            confidence_reasons=confidence.reasons,
            block_kind="ELECTIVE_GROUP" if options else "CLASS",
            options=options,
            bounds=final_bounds,
            raw=raw,
            cells=raw_cells,
        )

    @staticmethod
    def find_batch_cells(sheet: Worksheet) -> list[Cell]:
        anchor = BatchExtractor.find_anchor_cell(sheet)
        if anchor is None:
            return []

        cells: list[Cell] = []
        for col in range(anchor.column, sheet.max_column + 1):
            cell = sheet.cell(row=anchor.row, column=col)
            if BatchExtractor.is_batch_code(cell.value):
                cells.append(cell)
        return cells
