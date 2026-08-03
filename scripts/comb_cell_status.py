"""Cell status constants for comb_workbook_data."""
from __future__ import annotations

BASE_WORKBOOK_YEAR = 2017
# Template baseline years (2017–2025); columns extend beyond when data is stored.
PILOT_YEARS = list(range(BASE_WORKBOOK_YEAR, 2026))


def is_workbook_year(year: int) -> bool:
    """True for any calendar year at or after the workbook template start."""
    return year >= BASE_WORKBOOK_YEAR

STATUS_FILLED = "filled"
STATUS_PENDING = "pending"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_CONFIRMED_ABSENT = "confirmed_absent"
STATUS_TEMPLATE_MISMATCH = "template_mismatch"

DISPLAY_EMPTY = "-"


def is_red_status(status: str | None) -> bool:
    return status in (STATUS_CONFIRMED_ABSENT, STATUS_TEMPLATE_MISMATCH)


def is_pilot_empty(status: str | None, year: int, pilot_years: list[int]) -> bool:
    if not is_workbook_year(year):
        return True
    return status in (None, STATUS_PENDING, STATUS_EXTRACTION_FAILED, STATUS_CONFIRMED_ABSENT)
