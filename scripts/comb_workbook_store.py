"""
MongoDB storage for COMB / DB-page workbook data (separate from financial_tables).

Collection: comb_workbook_data
One document per (company_slug, year, report_type, quarter, sheet, label).

Financial statement tables live in ``financial_tables`` (Demo_run / db_uploader).
This collection holds values mapped to the COMB model xlsx layout (FS, Drivers,
Ratios, Quarterly) plus optional note breakdowns for FS cells linked to Drivers.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
from pymongo import ASCENDING, MongoClient, ReplaceOne

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
TEMPLATE_UPDATED = BACKEND_DIR / "New_Updates" / "COMB model - updated.xlsx"
TEMPLATE_LEGACY = BACKEND_DIR / "New_Updates" / "COMB model.xlsx"

from comb_cell_status import (
    is_workbook_year,
    STATUS_CONFIRMED_ABSENT,
    STATUS_FILLED,
    STATUS_PENDING,
)

COMB_COLLECTION = "comb_workbook_data"
LABEL_COL = 2
BASE_EXPORT_YEAR_START = 2017
BASE_EXPORT_YEAR_END = 2025  # template ships with columns through this year


def baseline_export_years() -> list[int]:
    """Default year columns shipped in the COMB template workbook."""
    return list(range(BASE_EXPORT_YEAR_START, BASE_EXPORT_YEAR_END + 1))


def year_auto_extend_enabled() -> bool:
    """
    When false, DB tables stay on the template baseline (2017–2025) even if
    newer years exist in MongoDB. Set COMB_YEAR_AUTO_EXTEND=1 in backend/.env
    after running a new-year report to reveal extended columns.
    """
    raw = load_env().get("COMB_YEAR_AUTO_EXTEND", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def query_stored_years(
    db,
    company_slug: str,
    *,
    report_type: str | None = None,
    sheet: str | None = None,
) -> list[int]:
    """Distinct years present in comb_workbook_data for a company."""
    filt: dict[str, Any] = {"company_slug": company_slug}
    if report_type:
        filt["report_type"] = report_type
    if sheet:
        filt["sheet"] = sheet
    years: set[int] = set()
    for doc in db[COMB_COLLECTION].find(filt, {"year": 1}):
        y = doc.get("year")
        if y is not None:
            years.add(int(y))
    return sorted(years)


def resolve_export_years(
    db,
    company_slug: str,
    *,
    years: list[int] | None = None,
    report_type: str | None = None,
    sheet: str | None = None,
) -> list[int]:
    """
    Continuous year columns from template start through the latest stored year.

    When ``years`` is omitted, extends past the template baseline (2017–2025)
    automatically when newer report data exists in MongoDB.
    """
    if years is not None:
        return sorted({int(y) for y in years})
    baseline = baseline_export_years()
    if not year_auto_extend_enabled():
        return baseline
    stored = query_stored_years(
        db, company_slug, report_type=report_type, sheet=sheet
    )
    end_year = max(baseline) if not stored else max(max(baseline), max(stored))
    return list(range(BASE_EXPORT_YEAR_START, end_year + 1))


def resolve_template() -> Path:
    if TEMPLATE_UPDATED.exists():
        return TEMPLATE_UPDATED
    return TEMPLATE_LEGACY


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = BACKEND_DIR / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def get_db(client: MongoClient | None = None):
    env = load_env()
    if client is None:
        client = MongoClient(
            env.get("MONGO_URI", "mongodb://localhost:27017"),
            serverSelectionTimeoutMS=5000,
        )
    return client[env.get("MONGO_DB_NAME", "Research_Project")], client


def ensure_indexes(db) -> None:
    coll = db[COMB_COLLECTION]
    try:
        coll.drop_index("comb_workbook_unique")
    except Exception:
        pass
    coll.create_index(
        [
            ("company_slug", ASCENDING),
            ("year", ASCENDING),
            ("report_type", ASCENDING),
            ("quarter", ASCENDING),
            ("sheet", ASCENDING),
            ("label", ASCENDING),
            ("drivers_row", ASCENDING),
            ("template_row", ASCENDING),
        ],
        unique=True,
        name="comb_workbook_unique",
    )


def extract_fs_driver_links(ws) -> dict[str, int]:
    """FS labels whose formulas reference the Drivers sheet (note-backed cells)."""
    links: dict[str, int] = {}
    for r in range(5, ws.max_row + 1):
        label = ws.cell(r, LABEL_COL).value
        if not label:
            continue
        label_s = str(label).strip()
        for c in range(3, ws.max_column + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "Drivers!" in v:
                m = re.search(r"Drivers!([A-Z]+)(\d+)", v, re.IGNORECASE)
                if m:
                    links[label_s] = int(m.group(2))
                break
    return links


def extract_template_rows(ws) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in range(5, ws.max_row + 1):
        label = ws.cell(r, LABEL_COL).value
        if label is None:
            continue
        label_s = str(label).strip()
        if not label_s:
            continue
        kind = "data"
        upper = label_s.upper()
        if upper in {"INCOME STATEMENT", "BALANCE SHEET", "CASH FLOW STATEMENT", "OCI"}:
            kind = "section"
        elif label_s == "IS check":
            continue
        elif label_s in {
            "Assets",
            "Liabilities",
            "Equity",
            "Memorandum information",
            "Cash flows from investing activities",
            "Cash flows from financing activities",
            "Adjustments for:",
            "Less: Expenses",
        }:
            kind = "subsection"
        elif "check" in label_s.lower():
            kind = "check"
        rows.append({"row": r, "label": label_s, "kind": kind})
    return rows


def read_drivers_notes_for_row(ws, drivers_row: int, year_col: int) -> list[dict[str, Any]]:
    """Collect Drivers sub-lines near ``drivers_row`` (note breakdown)."""
    notes: list[dict[str, Any]] = []
    # Walk upward then downward from anchor row for indented note lines.
    start = max(3, drivers_row - 8)
    end = min(ws.max_row, drivers_row + 25)
    for r in range(start, end + 1):
        label = ws.cell(r, LABEL_COL).value
        if not label:
            continue
        label_s = str(label).strip()
        if not label_s or label_s.upper() in {"DRIVERS", "NONE"}:
            continue
        val = ws.cell(r, year_col).value
        if val is None or (isinstance(val, str) and val.startswith("=")):
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        notes.append({"label": label_s, "value": num, "row": r})
    return notes


def year_column_index(year: int, base_year: int = 2017, base_col: int = 3) -> int:
    return base_col + (year - base_year)


def build_note_metadata(template_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Map FS label -> {drivers_row, ...} from template formulas."""
    path = template_path or resolve_template()
    wb = openpyxl.load_workbook(path, data_only=False)
    fs_links = extract_fs_driver_links(wb["FS"])
    wb.close()
    return {label: {"drivers_row": row} for label, row in fs_links.items()}


def clear_workbook_sheets(
    db,
    company_slug: str,
    year: int,
    sheets: list[str],
    *,
    report_type: str = "annual",
) -> int:
    """Delete comb_workbook_data rows for the given sheets and year."""
    if not sheets:
        return 0
    result = db[COMB_COLLECTION].delete_many(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": report_type,
            "sheet": {"$in": list(sheets)},
        }
    )
    return int(result.deleted_count)


def upsert_cells(db, docs: list[dict[str, Any]]) -> int:
    if not docs:
        return 0
    ensure_indexes(db)
    now = datetime.now(timezone.utc)
    ops: list[ReplaceOne] = []
    for doc in docs:
        doc = {**doc, "updated_at": now}
        filt = {
            "company_slug": doc["company_slug"],
            "year": doc["year"],
            "report_type": doc["report_type"],
            "quarter": doc.get("quarter"),
            "sheet": doc["sheet"],
            "label": doc["label"],
        }
        if doc.get("sheet") == "Drivers" and doc.get("drivers_row") is not None:
            filt["drivers_row"] = doc["drivers_row"]
        if doc.get("sheet") == "FS" and doc.get("template_row") is not None:
            filt["template_row"] = doc["template_row"]
        ops.append(ReplaceOne(filt, doc, upsert=True))
    if ops:
        db[COMB_COLLECTION].bulk_write(ops, ordered=False)
    return len(ops)


def fetch_cell(
    db,
    company_slug: str,
    year: int,
    sheet: str,
    label: str,
    *,
    report_type: str = "annual",
    quarter: str | None = None,
) -> dict[str, Any] | None:
    return db[COMB_COLLECTION].find_one(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": report_type,
            "quarter": quarter,
            "sheet": sheet,
            "label": label,
        }
    )


class CombWorkbookStore:
    """Read COMB workbook values from comb_workbook_data (not financial_tables)."""

    def __init__(self, db, company_slug: str):
        self.db = db
        self.company_slug = company_slug
        self._cache: dict[tuple[str, int, str, str | None], dict[str, Any]] = {}

    def _load_sheet(
        self,
        sheet: str,
        year: int,
        report_type: str = "annual",
        quarter: str | None = None,
    ) -> dict[str, Any]:
        key = (sheet, year, report_type, quarter)
        if key not in self._cache:
            index: dict[str, Any] = {}
            cursor = self.db[COMB_COLLECTION].find(
                {
                    "company_slug": self.company_slug,
                    "year": year,
                    "report_type": report_type,
                    "quarter": quarter,
                    "sheet": sheet,
                }
            )
            for doc in cursor:
                if sheet == "Drivers" and doc.get("drivers_row") is not None:
                    index[f"row:{doc['drivers_row']}"] = doc
                if sheet == "FS" and doc.get("template_row") is not None:
                    index[f"row:{doc['template_row']}"] = doc
                index[str(doc["label"])] = doc
            self._cache[key] = index
        return self._cache[key]

    def _resolve_doc(
        self,
        sheet: str,
        year: int,
        label: str,
        *,
        report_type: str = "annual",
        quarter: str | None = None,
        drivers_row: int | None = None,
        template_row: int | None = None,
    ) -> dict[str, Any] | None:
        index = self._load_sheet(sheet, year, report_type, quarter)
        if sheet == "Drivers" and drivers_row is not None:
            doc = index.get(f"row:{drivers_row}")
            if doc:
                return doc
        if sheet == "FS" and template_row is not None:
            doc = index.get(f"row:{template_row}")
            if doc:
                return doc
        return index.get(label)

    def lookup(
        self,
        year: int,
        label: str,
        sheet: str = "FS",
        *,
        report_type: str = "annual",
        quarter: str | None = None,
        drivers_row: int | None = None,
        template_row: int | None = None,
        entity: str = "group",
    ) -> float | None:
        doc = self._resolve_doc(
            sheet,
            year,
            label,
            report_type=report_type,
            quarter=quarter,
            drivers_row=drivers_row,
            template_row=template_row,
        )
        if not doc:
            return None
        if (entity or "group").lower() == "bank":
            val = doc.get("value_bank")
            if val is None:
                return None
            return float(val)
        # Prefer explicit group field when present; fall back to legacy value.
        val = doc.get("value_group", doc.get("value"))
        return float(val) if val is not None else None

    def lookup_status(
        self,
        year: int,
        label: str,
        sheet: str = "FS",
        *,
        report_type: str = "annual",
        quarter: str | None = None,
        drivers_row: int | None = None,
        template_row: int | None = None,
    ) -> str:
        if not is_workbook_year(year):
            return STATUS_PENDING
        doc = self._resolve_doc(
            sheet,
            year,
            label,
            report_type=report_type,
            quarter=quarter,
            drivers_row=drivers_row,
            template_row=template_row,
        )
        if not doc:
            return STATUS_PENDING
        return str(
            doc.get("status")
            or (STATUS_FILLED if doc.get("value") is not None else STATUS_PENDING)
        )

    def lookup_doc(
        self,
        year: int,
        label: str,
        sheet: str = "FS",
        *,
        report_type: str = "annual",
        quarter: str | None = None,
        drivers_row: int | None = None,
        template_row: int | None = None,
    ) -> dict[str, Any] | None:
        return self._resolve_doc(
            sheet,
            year,
            label,
            report_type=report_type,
            quarter=quarter,
            drivers_row=drivers_row,
            template_row=template_row,
        )

    def cell_meta(
        self,
        year: int,
        label: str,
        sheet: str = "FS",
        *,
        report_type: str = "annual",
        quarter: str | None = None,
        drivers_row: int | None = None,
        template_row: int | None = None,
    ) -> dict[str, Any] | None:
        return self.lookup_doc(
            year,
            label,
            sheet,
            report_type=report_type,
            quarter=quarter,
            drivers_row=drivers_row,
            template_row=template_row,
        )

    def notes_for(
        self,
        year: int,
        label: str,
        sheet: str = "FS",
    ) -> list[dict[str, Any]]:
        doc = self.cell_meta(year, label, sheet)
        if not doc:
            return []
        notes = doc.get("notes")
        return notes if isinstance(notes, list) else []

    def has_data_for_year(self, year: int, sheet: str = "FS") -> bool:
        return (
            self.db[COMB_COLLECTION].count_documents(
                {
                    "company_slug": self.company_slug,
                    "year": year,
                    "sheet": sheet,
                },
                limit=1,
            )
            > 0
        )
