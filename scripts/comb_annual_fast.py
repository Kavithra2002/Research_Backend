"""
Fast annual DB extraction helpers (target ~2 min per report).

  1. FS Description values from financial_tables (one index build, light retry)
  2. Note-table flags from manifest + Note column index
  3. Note page PNG captures (single PDF text scan + single render pass)
"""
from __future__ import annotations

from typing import Any

from comb_annual_fs_pdf_verify import is_suspicious_fs_value
from comb_annual_fs_validate import aggressive_fs_lookup, reconcile_operating_profit_fs_tax
from comb_annual_memorandum import is_memorandum_fs_label, lookup_memorandum_from_tables
from comb_cell_status import STATUS_FILLED
from comb_workbook_store import COMB_COLLECTION
from generate_comb_model import DataExtractor


def load_existing_fs_cells(
    db,
    company_slug: str,
    year: int,
) -> dict[str, dict[str, Any]]:
    """All filled FS cells for a year in one query."""
    cursor = db[COMB_COLLECTION].find(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "sheet": "FS",
            "status": STATUS_FILLED,
            "value": {"$ne": None},
        }
    )
    return {str(doc["label"]): doc for doc in cursor}


def fast_extract_fs_values(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    *,
    sections: dict[str, str] | None = None,
) -> dict[str, float | None]:
    """Lookup all FS labels using one cached table index + one missing-label retry."""
    values: dict[str, float | None] = {}
    fs_ext.index_for_year(year)
    sections = sections or {}

    for label in labels:
        section = sections.get(label)
        if is_memorandum_fs_label(label):
            values[label] = lookup_memorandum_from_tables(fs_ext, year, label)
        else:
            values[label] = fs_ext.lookup(year, label, section=section)

    missing = [lbl for lbl in labels if values.get(lbl) is None]
    if missing:
        for label in missing:
            values[label] = aggressive_fs_lookup(fs_ext, year, label)

    suspicious = [
        lbl
        for lbl in labels
        if values.get(lbl) is not None
        and is_suspicious_fs_value(lbl, values.get(lbl), values)
    ]
    if suspicious:
        fs_ext._year_cache.pop((year, "group"), None)
        fs_ext._section_cache.pop((year, "group"), None)
        fs_ext._year_cache.pop(year, None)
        for label in suspicious:
            retry = aggressive_fs_lookup(fs_ext, year, label)
            if retry is not None and not is_suspicious_fs_value(
                label, retry, values
            ):
                values[label] = retry

    values, _ = reconcile_operating_profit_fs_tax(values)
    return values


def fast_extract_fs_values_for_entity(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    *,
    entity_column: str = "group",
    sections: dict[str, str] | None = None,
) -> dict[str, float | None]:
    """Lookup FS labels for a specific GROUP/BANK entity column."""
    entity = (entity_column or "group").lower()
    values: dict[str, float | None] = {}
    sections = sections or {}
    fs_ext.index_for_year(year, entity_column=entity)
    for label in labels:
        section = sections.get(label)
        if is_memorandum_fs_label(label):
            if entity == "group":
                values[label] = lookup_memorandum_from_tables(fs_ext, year, label)
            else:
                values[label] = fs_ext.lookup(
                    year, label, entity_column=entity, section=section
                )
        else:
            values[label] = fs_ext.lookup(
                year, label, entity_column=entity, section=section
            )
    return values
