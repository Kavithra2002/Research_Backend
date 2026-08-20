"""
Build /db FS, Notes, and Quarterly grids from a company's own financial_tables.

Main description rows keep that issuer's statement order. Expanded note-table
columns follow each report's own layout. Used for non-bank issuers (e.g.
Ambeon Holdings) whose line items do not match the COMB bank template.
"""
from __future__ import annotations

import re
from typing import Any

from generate_comb_model import (
    DataExtractor,
    _find_quarterly_group_col,
    _note_tables_for_sources,
    _ui_note_tables_index,
    baseline_export_years,
    load_env,
    norm_label,
    parse_number,
    quarterly_column_defs,
)
from pymongo import MongoClient

NATIVE_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("INCOME STATEMENT", ("income_statement", "consolidated_income_statement", "profit_loss")),
    ("OCI", ("oci", "comprehensive_income")),
    ("BALANCE SHEET", ("sofp", "financial_position")),
    ("CASH FLOW STATEMENT", ("cash_flows", "cash_flow_statement")),
]

_JUNK_LABEL = re.compile(
    r"^(note|notes?|lkr|rs\.?|rs\.?'?0*0*|%|change %|year ended.*|"
    r"for the year.*|for the (period|quarter).*|continuing operations|"
    r"discontinued operations|group|company|unaudited|audited)$",
    re.I,
)
_QUARTER_NUM_RE = re.compile(r"Q?\s*([1-4])\b", re.I)


def _db():
    env = load_env()
    client = MongoClient(
        env.get("MONGO_URI", "mongodb://localhost:27017"),
        serverSelectionTimeoutMS=5000,
    )
    return client[env.get("MONGO_DB_NAME", "Research_Project")], client


def _company_name(db, slug: str) -> str:
    sample = db.financial_tables.find_one(
        {"company_slug": slug}, {"company_name": 1}
    )
    if sample and sample.get("company_name"):
        return str(sample["company_name"])
    return slug.replace("_", " ")


def _cells(row: Any) -> list[str]:
    raw = row.get("cells") if isinstance(row, dict) else row
    if not raw:
        return []
    return [str(c) if c is not None else "" for c in raw]


def _usable_label(label: str) -> bool:
    s = (label or "").strip()
    if not s or len(s) < 2:
        return False
    if _JUNK_LABEL.match(s):
        return False
    if parse_number(s) is not None:
        return False
    return True


def _row_labels(doc: dict | None) -> list[str]:
    if not doc:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in doc.get("rows") or []:
        cells = _cells(row)
        if not cells:
            continue
        label = cells[0].strip()
        if not _usable_label(label):
            continue
        key = norm_label(label)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _detect_unit(docs: list[dict]) -> str:
    blob = " ".join(
        " ".join(str(c) for c in (hrow or []))
        for doc in docs
        for hrow in (doc.get("header_rows") or [])[:4]
    ).lower()
    if "000" in blob or "'000" in blob or "rs.000" in blob:
        return "LKR '000 except per share data"
    return "LKR except per share data"


def _detect_period_label(docs: list[dict], *, quarterly: bool = False) -> str:
    blob = " ".join(
        " ".join(str(c) for c in (hrow or []))
        for doc in docs
        for hrow in (doc.get("header_rows") or [])[:4]
    ).lower()
    if quarterly:
        return "For the quarter ended"
    if "31 march" in blob or "march 31" in blob:
        return "For the year ended 31 March"
    if "31 december" in blob or "december 31" in blob:
        return "For the year ended December 31,"
    return "For the year ended"


def _normalize_quarter(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"Q1", "Q2", "Q3", "Q4"}:
        return text
    match = _QUARTER_NUM_RE.search(text)
    if match:
        return f"Q{match.group(1)}"
    return None


def _note_source_index(
    db, company_slug: str, years: list[int]
) -> dict[tuple[int, str], dict[str, Any]]:
    """(year, note_ref) -> capture metadata from note_* financial_tables docs."""
    out: dict[tuple[int, str], dict[str, Any]] = {}
    cursor = db.financial_tables.find(
        {
            "company_slug": company_slug,
            "year": {"$in": years},
            "report_type": "annual",
            "statement_key": {"$regex": "^note_"},
        },
        {
            "year": 1,
            "note_ref": 1,
            "statement_key": 1,
            "parent_label": 1,
            "source_pdf": 1,
            "source_page": 1,
            "source_pages": 1,
            "capture_files": 1,
        },
    )
    for doc in cursor:
        year = int(doc.get("year") or 0)
        ref = str(doc.get("note_ref") or "").strip()
        if not year or not ref:
            continue
        out[(year, ref)] = {
            "note_ref": ref,
            "source_pdf": doc.get("source_pdf"),
            "source_page": doc.get("source_page"),
            "source_pages": doc.get("source_pages") or [],
            "capture_files": doc.get("capture_files") or [],
            "statement_key": str(doc.get("statement_key") or ""),
        }
    return out


def _best_section_doc(
    ext: DataExtractor, year: int, keys: tuple[str, ...]
) -> dict | None:
    ranked: list[tuple[float, dict]] = []
    for doc in ext._docs_for_year(year):
        sk = str(doc.get("statement_key") or "")
        if any(sk == k or sk.startswith(k) for k in keys) or _matches_section_keys(
            doc, keys
        ):
            ranked.append((ext._doc_quality(doc, year), doc))
    if not ranked:
        return None
    ranked.sort(key=lambda t: t[0], reverse=True)
    return ranked[0][1]


def build_native_fs_preview(
    company_slug: str,
    *,
    years: list[int] | None = None,
) -> dict[str, Any]:
    db, client = _db()
    try:
        year_list = years or baseline_export_years()
        ext = DataExtractor(db, company_slug)
        # Warm caches (including comparative year+1 docs).
        for year in year_list:
            ext.index_for_year(year)

        section_docs: list[dict] = []
        preview_rows: list[dict[str, Any]] = []
        filled = missing = 0

        for section, keys in NATIVE_SECTIONS:
            ordered: list[str] = []
            seen: set[str] = set()
            for year in reversed(year_list):
                doc = _best_section_doc(ext, year, keys)
                if doc:
                    section_docs.append(doc)
                for label in _row_labels(doc):
                    key = norm_label(label)
                    if key in seen:
                        continue
                    seen.add(key)
                    ordered.append(label)
            if not ordered:
                continue
            preview_rows.append(
                {"label": section, "kind": "section", "values": {}}
            )
            for label in ordered:
                values: dict[str, float | None] = {}
                for year in year_list:
                    val = ext.lookup(year, label, section=keys[0])
                    values[str(year)] = val
                    if val is not None:
                        filled += 1
                    else:
                        missing += 1
                preview_rows.append(
                    {"label": label, "kind": "data", "values": values}
                )

        return {
            "view": "fs",
            "company_slug": company_slug,
            "company_name": _company_name(db, company_slug),
            "years": year_list,
            "unit": _detect_unit(section_docs),
            "period_label": _detect_period_label(section_docs),
            "rows": preview_rows,
            "cells_filled": filled,
            "cells_missing": missing,
            "quarterly_available": True,
        }
    finally:
        client.close()


def build_native_notes_preview(
    company_slug: str,
    *,
    years: list[int] | None = None,
) -> dict[str, Any]:
    """Same description rows as native FS, plus expandable note tables."""
    from comb_note_registry import build_label_note_index, _note_statement_key

    db, client = _db()
    try:
        year_list = years or baseline_export_years()
        ext = DataExtractor(db, company_slug)
        for year in year_list:
            ext.index_for_year(year)

        ui_note_tables = _ui_note_tables_index(db, company_slug, year_list)
        note_sources = _note_source_index(db, company_slug, year_list)
        note_by_year: dict[int, dict[str, dict[str, Any]]] = {}
        for year in year_list:
            note_by_year[year] = build_label_note_index(db, company_slug, year)

        section_docs: list[dict] = []
        preview_rows: list[dict[str, Any]] = []
        notes_extracted_years: set[int] = set()
        note_rows_with_data = 0
        filled = missing = 0

        for section, keys in NATIVE_SECTIONS:
            ordered: list[str] = []
            seen: set[str] = set()
            for year in reversed(year_list):
                doc = _best_section_doc(ext, year, keys)
                if doc:
                    section_docs.append(doc)
                for label in _row_labels(doc):
                    key = norm_label(label)
                    if key in seen:
                        continue
                    seen.add(key)
                    ordered.append(label)
            if not ordered:
                continue
            preview_rows.append(
                {"label": section, "kind": "section", "values": {}}
            )
            for label in ordered:
                values: dict[str, float | None] = {}
                values_bank: dict[str, float | None] = {}
                note_source_by_year: dict[str, dict[str, Any]] = {}
                row_has_notes = False
                want = norm_label(label)
                for year in year_list:
                    val = ext.lookup(year, label, section=keys[0])
                    bank_val = ext.lookup(
                        year, label, entity_column="bank", section=keys[0]
                    )
                    values[str(year)] = val
                    values_bank[str(year)] = bank_val
                    if val is not None:
                        filled += 1
                    else:
                        missing += 1
                    info = note_by_year.get(year, {}).get(want)
                    if not info or not info.get("note_ref"):
                        continue
                    row_has_notes = True
                    ref = str(info["note_ref"])
                    source = note_sources.get((year, ref))
                    if not source:
                        source = {
                            "note_ref": ref,
                            "statement_key": str(
                                info.get("statement_key")
                                or _note_statement_key(ref)
                            ),
                        }
                    note_source_by_year[str(year)] = source
                    notes_extracted_years.add(year)

                row_payload: dict[str, Any] = {
                    "label": label,
                    "kind": "data",
                    "values": values,
                    "values_bank": values_bank,
                }
                if row_has_notes:
                    row_payload["has_notes"] = True
                    if note_source_by_year:
                        row_payload["note_source_by_year"] = note_source_by_year
                        note_tables_by_year = _note_tables_for_sources(
                            ui_note_tables, note_source_by_year, label
                        )
                        if note_tables_by_year:
                            row_payload["note_tables_by_year"] = note_tables_by_year
                            note_rows_with_data += 1
                preview_rows.append(row_payload)

        return {
            "view": "notes",
            "company_slug": company_slug,
            "company_name": _company_name(db, company_slug),
            "years": year_list,
            "unit": _detect_unit(section_docs),
            "period_label": _detect_period_label(section_docs),
            "rows": preview_rows,
            "cells_filled": filled,
            "cells_missing": missing,
            "notes_extracted_years": sorted(notes_extracted_years, reverse=True),
            "note_line_items": sum(1 for r in preview_rows if r.get("has_notes")),
            "note_line_items_with_data": note_rows_with_data,
            "quarterly_available": True,
        }
    finally:
        client.close()


def _doc_title_blob(doc: dict) -> str:
    return " ".join(
        str(doc.get(k) or "")
        for k in ("statement_title", "statement_label", "caption", "statement_key")
    )


def _matches_section_keys(doc: dict, keys: tuple[str, ...]) -> bool:
    key = str(doc.get("statement_key") or "").lower()
    title = _doc_title_blob(doc).upper()
    if any(key == k or key.startswith(k) for k in keys):
        return True
    markers = {
        "income_statement": ("INCOME STATEMENT", "PROFIT OR LOSS", "PROFIT AND LOSS"),
        "consolidated_income_statement": ("INCOME STATEMENT", "PROFIT OR LOSS"),
        "profit_loss": ("PROFIT OR LOSS", "INCOME STATEMENT"),
        "oci": ("OTHER COMPREHENSIVE INCOME",),
        "comprehensive_income": ("OTHER COMPREHENSIVE INCOME",),
        "sofp": ("FINANCIAL POSITION", "BALANCE SHEET"),
        "financial_position": ("FINANCIAL POSITION", "BALANCE SHEET"),
        "cash_flows": ("CASH FLOW", "CASHFLOWS"),
        "cash_flow_statement": ("CASH FLOW", "CASHFLOWS"),
    }
    for k in keys:
        for marker in markers.get(k, ()):
            if marker in title:
                return True
    if keys[0].startswith("income") or "profit_loss" in keys:
        for row in (doc.get("rows") or [])[:10]:
            lab = (_cells(row)[0] if _cells(row) else "").lower()
            if lab.startswith("revenue") or "gross profit" in lab:
                return True
    return False


def _doc_quarter(doc: dict) -> str | None:
    return (
        _normalize_quarter(doc.get("quarter"))
        or _normalize_quarter(doc.get("report_group"))
        or _normalize_quarter(doc.get("report_key"))
        or _normalize_quarter(doc.get("period"))
    )


def _best_quarterly_section_doc(
    docs: list[dict], year: int, quarter: str, keys: tuple[str, ...]
) -> dict | None:
    want_q = _normalize_quarter(quarter) or quarter
    matched = [
        d
        for d in docs
        if int(d.get("year") or 0) == year
        and _doc_quarter(d) == want_q
        and _matches_section_keys(d, keys)
    ]
    if not matched:
        return None
    return max(matched, key=lambda d: len(d.get("rows") or []))


def _quarter_index(doc: dict, year: int) -> dict[str, float]:
    col = _find_quarterly_group_col(doc, year, entity_column="group")
    if col is None:
        col = _find_quarterly_group_col(doc, year, entity_column="bank")
    if col is None:
        # First numeric body column after the label / note slot.
        for row in doc.get("rows") or []:
            cells = _cells(row)
            start = 1
            if len(cells) > 2 and not parse_number(cells[1]):
                start = 1
            for i, cell in enumerate(cells[start:], start=start):
                val = parse_number(cell)
                if val is None:
                    continue
                # Skip tiny note/page numbers sitting next to the label.
                if abs(val) < 100 and i <= 2:
                    continue
                col = i
                break
            if col is not None:
                break
    if col is None:
        return {}
    index: dict[str, float] = {}
    for row in doc.get("rows") or []:
        cells = _cells(row)
        if not cells or col >= len(cells):
            continue
        label = cells[0].strip()
        if not _usable_label(label):
            continue
        val = parse_number(cells[col])
        if val is None:
            continue
        key = norm_label(label)
        if key and key not in index:
            index[key] = val
    return index


def build_native_quarterly_preview(
    company_slug: str,
    *,
    years: list[int] | None = None,
) -> dict[str, Any]:
    db, client = _db()
    try:
        q_docs = list(
            db.financial_tables.find(
                {
                    "company_slug": company_slug,
                    "report_type": {"$in": ["quarterly", "Quarterly"]},
                    "year": {"$ne": None},
                }
            )
        )
        stored = sorted(
            {
                int(d["year"])
                for d in q_docs
                if d.get("year") is not None
            }
        )
        baseline = baseline_export_years()
        year_list = years or (
            list(
                range(
                    min(baseline[0], stored[0] if stored else baseline[0]),
                    max(baseline[-1], stored[-1] if stored else baseline[-1]) + 1,
                )
            )
            if stored
            else baseline
        )

        columns = [
            {"key": col["key"], "label": col["label"]}
            for col in quarterly_column_defs(year_list)
        ]
        rows: list[dict[str, Any]] = []
        filled = missing = 0
        unit_docs: list[dict] = []

        for section, keys in NATIVE_SECTIONS:
            indexes: dict[tuple[int, str], dict[str, float]] = {}
            ordered_labels: list[str] = []
            seen: set[str] = set()
            for col_def in reversed(quarterly_column_defs(year_list)):
                year = int(col_def["year"])
                quarter = str(col_def["quarter"])
                doc = _best_quarterly_section_doc(q_docs, year, quarter, keys)
                if not doc:
                    continue
                unit_docs.append(doc)
                indexes[(year, quarter)] = _quarter_index(doc, year)
                for label in _row_labels(doc):
                    key = norm_label(label)
                    if key in seen:
                        continue
                    seen.add(key)
                    ordered_labels.append(label)
            if not ordered_labels:
                continue
            rows.append({"label": section, "kind": "section", "values": {}})
            for label in ordered_labels:
                values: dict[str, float | None] = {}
                statuses: dict[str, str] = {}
                want = norm_label(label)
                for col_def in quarterly_column_defs(year_list):
                    year = int(col_def["year"])
                    quarter = str(col_def["quarter"])
                    val = indexes.get((year, quarter), {}).get(want)
                    values[col_def["key"]] = val
                    statuses[col_def["key"]] = (
                        "filled" if val is not None else "pending"
                    )
                    if val is not None:
                        filled += 1
                    else:
                        missing += 1
                rows.append(
                    {
                        "label": label,
                        "kind": "data",
                        "values": values,
                        "statuses": statuses,
                    }
                )

        return {
            "view": "quarterly",
            "company_slug": company_slug,
            "company_name": _company_name(db, company_slug),
            "unit": _detect_unit(unit_docs),
            "period_label": _detect_period_label(unit_docs, quarterly=True),
            "columns": columns,
            "rows": rows,
            "cells_filled": filled,
            "cells_missing": missing,
            "years": year_list,
        }
    finally:
        client.close()
