"""Batch extraction used only during API startup."""

from __future__ import annotations

import logging
import re
from time import perf_counter

from openpyxl.cell.cell import Cell
from openpyxl.worksheet.worksheet import Worksheet


logger = logging.getLogger(__name__)


class BatchExtractor:
    """Find batch anchor cells such as 1B14, 1A11, or 3C11."""

    batch_code_pattern = re.compile(r"^\d[A-Z]\d{2}$")

    @classmethod
    def find_anchor_cell(cls, sheet: Worksheet) -> Cell | None:
        start = perf_counter()
        for row in sheet.iter_rows():
            for cell in row:
                if cls.is_batch_code(cell.value):
                    elapsed_ms = (perf_counter() - start) * 1000
                    logger.info(
                        "Found batch anchor %s at %s for sheet %s in %.2f ms",
                        cell.value,
                        cell.coordinate,
                        sheet.title,
                        elapsed_ms,
                    )
                    return cell

        elapsed_ms = (perf_counter() - start) * 1000
        logger.info("No batch anchor found for sheet %s in %.2f ms", sheet.title, elapsed_ms)
        return None

    @classmethod
    def is_batch_code(cls, value: object) -> bool:
        text = cls.normalize(value)
        return bool(text and cls.batch_code_pattern.fullmatch(text))

    @staticmethod
    def normalize(value: object) -> str | None:
        if value is None:
            return None
        text = re.sub(r"\s+", "", str(value).upper())
        return text or None


def find_first_batch_anchor(sheet: Worksheet) -> Cell | None:
    return BatchExtractor.find_anchor_cell(sheet)
