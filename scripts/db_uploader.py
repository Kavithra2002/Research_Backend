"""
db_uploader.py
==============
Push the table data produced by the extraction pipelines into MongoDB,
split **table-by-table** so the data can be navigated as:

    Company  ->  Year  ->  (Annual | Quarterly)  ->  list of statement tables

This module is the bridge between the Python extractors and the same
MongoDB the Node/Express backend already uses (``Research_Project`` by
default, connection read from ``backend/.env``).

Two source JSON shapes are supported and normalised into ONE document
shape before insertion:

1. ANNUAL  (Data_retrive.py / step3_send_to_openai.py)
   Top-level keys ARE the statement keys:

       { "income_statement": { "status": "ok",
                               "data": { "statement_title": ...,
                                         "tables": [ {header_rows, rows:[{cells,style}]} ] } },
         "sofp": { ... }, ... }

2. QUARTERLY  (Q_data_extraction.py)
   A wrapper with metadata and a ``statements`` map.  Each statement is
   EITHER the same step3 shape (``data.tables``) when OpenAI ran, OR the
   local rule-based shape (``columns`` + ``rows:[{label, values}]``):

       { "company": "ACL PLASTICS PLC", "period": "31st March 2016",
         "statements": { "consolidated_income_statement": { ... }, ... } }

Storage model (flat — one document per extracted table)
-------------------------------------------------------
collection ``companies``         : one doc per company  (registry)
collection ``financial_tables``  : one doc per table    (the real data)

The tree is expressed via indexed fields, not nested documents, and a
unique compound index makes re-runs *upsert* instead of duplicate:

    (company_slug, year, report_type, statement_key, table_index)

CLI
---
    # Upload one results file (report type is required for the wrapper-less
    # annual shape; auto-detected for quarterly)
    python db_uploader.py --results ../testing/ACME/ACME_results.json --type annual
    python db_uploader.py --results ../testing/ACME/ACME_quarterly_results.json --type quarterly

    # Upload everything found under a company folder
    python db_uploader.py --company-dir ../testing/ACME --company-key ACME

    # Walk a whole testing/ tree
    python db_uploader.py --testing-dir ../testing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
BACKEND_ENV = BACKEND_DIR / ".env"

COMPANIES_COLLECTION = "companies"
TABLES_COLLECTION     = "financial_tables"

REPORT_TYPES = ("annual", "quarterly")

# Main FS keys that must never be deleted just because a later upload
# omitted them (e.g. partial re-run / nested OCI under income).
CORE_ANNUAL_STATEMENT_KEYS = (
    "income_statement",
    "oci",
    "sofp",
    "cash_flows",
)
CORE_QUARTERLY_FAMILIES = (
    "income_statement",
    "oci",
    "sofp",
    "cash_flows",
    "equity",
)


# ─────────────────────────────────────────────────────────────────────────────
# .env / connection
# ─────────────────────────────────────────────────────────────────────────────

def _load_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser (no python-dotenv dependency)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env[k] = v
    except Exception:
        pass
    return env


def resolve_mongo_config(
    uri: str | None = None,
    db_name: str | None = None,
) -> tuple[str, str]:
    """CLI flag -> environment -> backend/.env -> sane localhost default."""
    env = _load_env_file(BACKEND_ENV)
    resolved_uri = (
        uri
        or os.environ.get("MONGO_URI")
        or env.get("MONGO_URI")
        or "mongodb://localhost:27017"
    )
    resolved_db = (
        db_name
        or os.environ.get("MONGO_DB_NAME")
        or env.get("MONGO_DB_NAME")
        or "Research_Project"
    )
    return resolved_uri, resolved_db


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation helpers
# ─────────────────────────────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_QUARTER_RE = re.compile(r"\bQ\s*([1-4])\b", re.IGNORECASE)


def _years_in(text: Any) -> list[int]:
    if text is None:
        return []
    return [int(m.group(0)) for m in _YEAR_RE.finditer(str(text))]


def _quarter_from(*sources: Any) -> str | None:
    """Pull a 'Q1'..'Q4' label out of a report group name or period string."""
    for s in sources:
        if not s:
            continue
        m = _QUARTER_RE.search(str(s))
        if m:
            return f"Q{m.group(1)}"
    return None


def _year_from_report_name(*sources: Any) -> int | None:
    """Reporting year encoded in a report folder / period string.

    Unlike ``_derive_year``, this ignores table header columns so comparative
    years (e.g. 2022 beside 2023) cannot mislabel the report period.
    """
    for s in sources:
        if not s:
            continue
        years = _years_in(s)
        years = [y for y in years if 1990 <= y <= 2100]
        if years:
            return years[0]
    return None


def _reporting_year(
    *,
    report_type: str,
    report_group: str | None,
    period: str | None,
    header_rows: list[list[str]] | None,
    columns: list[str] | None,
    preamble: str | None,
    fallback: int | None,
) -> int | None:
    """Canonical reporting year for a stored table row."""
    if report_type == "quarterly":
        from_name = _year_from_report_name(report_group, period)
        if from_name is not None:
            return from_name
        return fallback
    # Annual: prefer the report folder's year (e.g. "Annual report 2025") so a
    # statement whose printed header only shows a comparative column (e.g. an
    # "Investor Information" table headed 2024 inside the 2025 report) cannot
    # create a phantom year node. Fall back to header-derived year only when the
    # folder name carries no year.
    from_name = _year_from_report_name(report_group)
    if from_name is not None:
        return from_name
    return _derive_year(
        period=period,
        header_rows=header_rows,
        columns=columns,
        preamble=preamble,
        fallback=fallback,
    )


def _derive_year(
    *,
    period: str | None,
    header_rows: list[list[str]] | None,
    columns: list[str] | None,
    preamble: str | None,
    fallback: int | None,
) -> int | None:
    """Best-effort reporting year.

    Quarterly reports carry an explicit ``period`` ("31st March 2016").
    Annual statements expose the year inside the column headers
    ("2025", "2024") — we take the most recent one printed.
    """
    candidates: list[int] = []
    candidates += _years_in(period)
    for hr in header_rows or []:
        for cell in hr:
            candidates += _years_in(cell)
    for c in columns or []:
        candidates += _years_in(c)
    candidates += _years_in(preamble)

    # Keep only plausible report years.
    candidates = [y for y in candidates if 1990 <= y <= 2100]
    if candidates:
        return max(candidates)
    return fallback


def _rows_from_step3_table(table: dict) -> tuple[list[list[str]], list[dict]]:
    """A step3/OpenAI table already stores header_rows + rows[{cells,style}]."""
    header_rows = table.get("header_rows") or []
    norm_rows: list[dict] = []
    for r in table.get("rows") or []:
        if not isinstance(r, dict):
            continue
        cells = r.get("cells")
        if not isinstance(cells, list):
            continue
        norm_rows.append({
            "cells": [("" if c is None else str(c)) for c in cells],
            "style": str(r.get("style") or "data"),
        })
    return header_rows, norm_rows


_OCI_CAPTION_HINT = re.compile(
    r"other\s+comprehensive\s+income|"
    r"profit\s+or\s+loss\s+and\s+other\s+comprehensive",
    re.I,
)
_OCI_SECTION_HINT = re.compile(
    r"items\s+that\s+(?:will\s+never|are\s+or\s+may)\s+be\s+reclassified|"
    r"total\s+comprehensive\s+income|"
    r"other\s+comprehensive\s+income",
    re.I,
)
_PL_HINT = re.compile(
    r"gross\s+income|interest\s+income|net\s+interest|"
    r"operating\s+income|profit\s+before\s+tax|"
    r"income\s+statement",
    re.I,
)


def _table_looks_like_oci(
    rows: list[dict],
    *,
    caption: str | None = None,
    statement_title: str | None = None,
) -> bool:
    """True when a *nested* table under income_statement is really the OCI block.

    Must be strict: a normal P&L can mention OCI once and must NOT be reclassified.
    """
    caption_blob = f"{caption or ''} {statement_title or ''}"
    labels: list[str] = []
    for r in rows[:30]:
        if not isinstance(r, dict):
            continue
        cells = r.get("cells") or []
        if cells:
            labels.append(str(cells[0] or ""))
    label_blob = " | ".join(labels)

    # Strong P&L signal → never treat as OCI.
    pl_hits = len(_PL_HINT.findall(label_blob + " " + caption_blob))
    oci_hits = len(_OCI_SECTION_HINT.findall(label_blob))
    if pl_hits >= 2 and oci_hits <= 1:
        return False

    if _OCI_CAPTION_HINT.search(caption or ""):
        return True
    # Need at least two OCI-specific row labels (not a single stray phrase).
    return oci_hits >= 2


def _rows_from_local_statement(stmt: dict) -> tuple[list[list[str]], list[dict]]:
    """Convert the local rule-based shape (columns + rows[{label, values}])
    into the unified header_rows + rows[{cells, style}] form."""
    columns: list[str] = stmt.get("columns") or []
    header_rows: list[list[str]] = stmt.get("header_rows") or (
        [columns] if columns else []
    )
    value_cols = columns[1:] if len(columns) > 1 else []

    norm_rows: list[dict] = []
    for r in stmt.get("rows") or []:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or "")
        values = r.get("values") or {}
        cells = [label] + [str(values.get(col, "")) for col in value_cols]
        norm_rows.append({"cells": cells, "style": "data"})
    return header_rows, norm_rows


def _statements_map(doc: dict) -> tuple[dict, dict]:
    """Return (statements_map, wrapper_meta).

    Quarterly docs wrap statements under a ``statements`` key and add
    company/period metadata.  Annual docs ARE the statements map.
    """
    if isinstance(doc.get("statements"), dict):
        meta = {
            "company": doc.get("company"),
            "period": doc.get("period"),
            "source_pdf": doc.get("source_pdf"),
            "generated_at": doc.get("generated_at"),
            "model": doc.get("model"),
        }
        return doc["statements"], meta
    # Annual: the whole document is the statement map.
    return doc, {}


def iter_table_documents(
    doc: dict,
    *,
    company_slug: str,
    report_type: str,
    company_name: str | None = None,
    fallback_year: int | None = None,
    report_group: str | None = None,
    source_pdf: str | None = None,
) -> Iterable[dict]:
    """Yield one normalised ``financial_tables`` document per extracted table.

    ``report_group`` is the originating report folder (e.g.
    "Annual Report 2024" or "Quarterly Report 2024 Q1").  It is the primary
    discriminator that keeps the four quarters of one year from colliding and
    makes a re-run of the SAME report upsert in place.

    ``source_pdf`` is the path of the originating report PDF; when provided it
    overrides any value found inside the results document so every table row
    records where it came from.
    """
    statements, meta = _statements_map(doc)

    resolved_company_name = (
        company_name
        or meta.get("company")
        or doc.get("company")
        or company_slug
    )
    period = meta.get("period") or doc.get("period")
    source_pdf = source_pdf or meta.get("source_pdf") or doc.get("source_pdf")
    model = meta.get("model") or doc.get("model")
    generated_at = meta.get("generated_at") or doc.get("generated_at")
    report_group = (report_group or "").strip() or None
    quarter = (
        _quarter_from(report_group, period)
        if report_type == "quarterly" else None
    )

    oci_stmt = statements.get("oci") if isinstance(statements, dict) else None
    oci_already_ok = (
        isinstance(oci_stmt, dict)
        and (oci_stmt.get("status") in (None, "ok"))
        and (
            isinstance((oci_stmt.get("data") or {}).get("tables"), list)
            and (oci_stmt.get("data") or {}).get("tables")
            or isinstance(oci_stmt.get("rows"), list)
            and oci_stmt.get("rows")
        )
    )
    income_table_i = 0
    oci_table_i = 0

    for statement_key, stmt in statements.items():
        if not isinstance(stmt, dict):
            continue
        status = stmt.get("status")
        # Skip statements the extractor flagged as failed (only when an
        # explicit status field exists — local shape has none).
        if status is not None and status != "ok":
            continue

        data = stmt.get("data")
        statement_title = ""
        # The outer human-readable label (e.g. "Income Statement / Statement
        # of Profit or Loss"), kept separate from data.statement_title so the
        # original results JSON can be reconstructed byte-for-byte from Mongo.
        statement_label = stmt.get("title") or None
        preamble = ""
        footnotes = ""
        tables: list[tuple[list[list[str]], list[dict], str | None]] = []

        if isinstance(data, dict) and isinstance(data.get("tables"), list):
            # step3 / OpenAI shape
            statement_title = (
                data.get("statement_title")
                or stmt.get("title")
                or statement_key.replace("_", " ").title()
            )
            preamble = data.get("preamble") or ""
            footnotes = data.get("footnotes") or ""
            for table in data["tables"]:
                if not isinstance(table, dict):
                    continue
                header_rows, rows = _rows_from_step3_table(table)
                tables.append((header_rows, rows, table.get("caption")))
        elif isinstance(stmt.get("rows"), list):
            # local rule-based shape
            statement_title = (
                stmt.get("title")
                or statement_key.replace("_", " ").title()
            )
            header_rows, rows = _rows_from_local_statement(stmt)
            tables.append((header_rows, rows, None))
        else:
            continue

        for table_index, (header_rows, rows, caption) in enumerate(tables):
            if not rows and not header_rows:
                continue
            year = _reporting_year(
                report_type=report_type,
                report_group=report_group,
                period=period,
                header_rows=header_rows,
                columns=stmt.get("columns"),
                preamble=preamble,
                fallback=fallback_year,
            )
            # The per-report discriminator used in the unique key. Prefer the
            # source report folder, then the printed period, then the year.
            report_key = (
                report_group
                or (period.strip() if isinstance(period, str) and period.strip() else None)
                or (str(year) if year is not None else None)
                or "default"
            )
            # Trust the extractor key for the primary table.
            # Only nested extras under income_statement (tables[1+]) may be
            # reclassified as OCI — never drop income_statement itself.
            out_key = statement_key
            out_title = statement_title
            out_label = statement_label
            out_index = table_index
            looks_oci = (
                statement_key == "income_statement"
                and table_index >= 1
                and _table_looks_like_oci(
                    rows, caption=caption, statement_title=statement_title
                )
            )
            if looks_oci and oci_already_ok:
                # Extra nested OCI duplicate — skip only this nested table.
                continue
            if looks_oci:
                out_key = "oci"
                out_title = "Statement of Other Comprehensive Income (OCI)"
                out_label = out_title
                out_index = oci_table_i
                oci_table_i += 1
            elif statement_key == "income_statement":
                out_index = income_table_i
                income_table_i += 1
            elif statement_key == "oci":
                out_index = oci_table_i
                oci_table_i += 1

            yield {
                "company_slug": company_slug,
                "company_name": resolved_company_name,
                "year": year,
                "report_type": report_type,
                "report_group": report_group,
                "report_key": report_key,
                "quarter": quarter,
                "period_label": period,
                "statement_key": out_key,
                "statement_title": out_title,
                "statement_label": out_label,
                "table_index": out_index,
                "caption": caption,
                "preamble": preamble,
                "footnotes": footnotes,
                "header_rows": header_rows,
                "rows": rows,
                "row_count": len(rows),
                "source_pdf": source_pdf,
                "extraction_model": model,
                "extraction_status": status or "ok",
                "extracted_at": generated_at,
                "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB writer
# ─────────────────────────────────────────────────────────────────────────────

class MongoUploader:
    """Thin pymongo wrapper that upserts companies + financial tables."""

    def __init__(self, uri: str, db_name: str):
        from pymongo import MongoClient  # imported lazily so --help works w/o pymongo

        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        # Force a round-trip so connection errors surface immediately.
        self.client.admin.command("ping")
        self.db = self.client[db_name]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.db[COMPANIES_COLLECTION].create_index("slug", unique=True)

        # Drop the legacy unique index whose key omitted the report group —
        # it incorrectly made every quarter of a year collide.
        try:
            existing = self.db[TABLES_COLLECTION].index_information()
            if "uniq_company_year_type_statement_table" in existing:
                self.db[TABLES_COLLECTION].drop_index(
                    "uniq_company_year_type_statement_table")
        except Exception:
            pass

        self.db[TABLES_COLLECTION].create_index(
            [
                ("company_slug", 1),
                ("report_type", 1),
                ("report_key", 1),
                ("statement_key", 1),
                ("table_index", 1),
            ],
            unique=True,
            name="uniq_company_type_reportkey_statement_table",
        )
        self.db[TABLES_COLLECTION].create_index(
            [("company_slug", 1), ("report_type", 1), ("year", 1)],
            name="lookup_company_type_year",
        )

    def upsert_company(self, slug: str, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db[COMPANIES_COLLECTION].update_one(
            {"slug": slug},
            {
                "$set": {"name": name, "updated_at": now},
                "$setOnInsert": {"slug": slug, "created_at": now},
            },
            upsert=True,
        )

    def replace_report_scope(
        self,
        *,
        company_slug: str,
        report_type: str,
        report_key: str,
        scopes: "set[tuple[int | None, str | None]]",
        statement_keys: "set[str] | None" = None,
    ) -> int:
        """Delete stale tables before re-inserting a report.

        **Surgical by default:** only removes ``statement_key`` values that are
        present in the new upload.  Core FS keys (income / OCI / SoFP / cash
        flows) that are *not* in this upload are preserved so a partial re-run
        cannot wipe Cash Flows (or any other core statement) that was already
        stored for the same report.

        Legacy rows with null/empty ``report_key`` in the same (year, quarter)
        scopes are still cleaned for the same statement keys only.
        """
        keys = {str(k) for k in (statement_keys or ()) if k}
        core = set(
            CORE_ANNUAL_STATEMENT_KEYS
            if report_type == "annual"
            else CORE_QUARTERLY_FAMILIES
        )

        # Always replace keys we are about to write.  Non-core keys under the
        # same report_key that vanished from the new extract are also dropped.
        # Core keys NOT in this upload are intentionally kept.
        if keys:
            delete_keys = set(keys)
            # Drop obsolete non-core extras for this report_key only.
            extra_filter = {
                "company_slug": company_slug,
                "report_type": report_type,
                "report_key": report_key,
                "statement_key": {"$nin": list(core | keys)},
            }
        else:
            # Backward-compat: no key list → old full report_key wipe
            # (should be rare; callers should pass statement_keys).
            delete_keys = None
            extra_filter = None

        deleted = 0
        if delete_keys is None:
            or_clauses: list[dict] = [{"report_key": report_key}]
            for year, quarter in scopes:
                or_clauses.append({
                    "year": year,
                    "quarter": quarter,
                    "report_key": {"$in": [None, ""]},
                })
            result = self.db[TABLES_COLLECTION].delete_many(
                {
                    "company_slug": company_slug,
                    "report_type": report_type,
                    "$or": or_clauses,
                }
            )
            deleted += int(getattr(result, "deleted_count", 0) or 0)
        else:
            key_list = sorted(delete_keys)
            # Same report_key + statement keys being rewritten.
            result = self.db[TABLES_COLLECTION].delete_many(
                {
                    "company_slug": company_slug,
                    "report_type": report_type,
                    "report_key": report_key,
                    "statement_key": {"$in": key_list},
                }
            )
            deleted += int(getattr(result, "deleted_count", 0) or 0)
            # Legacy null report_key rows for the same keys / year scopes.
            for year, quarter in scopes:
                result = self.db[TABLES_COLLECTION].delete_many(
                    {
                        "company_slug": company_slug,
                        "report_type": report_type,
                        "year": year,
                        "quarter": quarter,
                        "report_key": {"$in": [None, ""]},
                        "statement_key": {"$in": key_list},
                    }
                )
                deleted += int(getattr(result, "deleted_count", 0) or 0)
            if extra_filter is not None:
                result = self.db[TABLES_COLLECTION].delete_many(extra_filter)
                deleted += int(getattr(result, "deleted_count", 0) or 0)

        return deleted

    def upsert_table(self, table_doc: dict) -> bool:
        """Upsert a single table document by its unique compound key."""
        key = {
            "company_slug": table_doc["company_slug"],
            "report_type": table_doc["report_type"],
            "report_key": table_doc["report_key"],
            "statement_key": table_doc["statement_key"],
            "table_index": table_doc["table_index"],
        }
        self.db[TABLES_COLLECTION].update_one(
            key,
            {
                "$set": table_doc,
                "$unset": {"source_results_path": ""},
            },
            upsert=True,
        )
        return True

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# High-level upload entry points
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(msg, flush=True)


def _ok_statement_keys_in_doc(doc: dict) -> set[str]:
    """Statement keys that extracted successfully and carry at least one table/row."""
    statements, _meta = _statements_map(doc)
    out: set[str] = set()
    for key, stmt in statements.items():
        if not isinstance(stmt, dict):
            continue
        status = stmt.get("status")
        if status is not None and status != "ok":
            continue
        data = stmt.get("data")
        if isinstance(data, dict) and isinstance(data.get("tables"), list):
            for table in data["tables"]:
                if not isinstance(table, dict):
                    continue
                rows = table.get("rows") or []
                headers = table.get("header_rows") or []
                if rows or headers:
                    out.add(str(key))
                    break
        elif isinstance(stmt.get("rows"), list) and stmt.get("rows"):
            out.add(str(key))
    return out


def upload_results_data(
    doc: dict,
    report_type: str,
    *,
    company_slug: str,
    company_name: str | None = None,
    report_group: str | None = None,
    source_pdf: str | None = None,
    uri: str | None = None,
    db_name: str | None = None,
    uploader: "MongoUploader | None" = None,
    quiet: bool = False,
) -> dict:
    """Upsert every table from an in-memory results document (no disk path stored)."""
    report_type = (report_type or "").strip().lower()
    if report_type not in REPORT_TYPES:
        return {
            "ok": False,
            "error": f"invalid report_type: {report_type!r}",
            "tables": 0,
        }
    if not isinstance(doc, dict):
        return {"ok": False, "error": "results JSON is not an object", "tables": 0}

    slug = company_slug
    # Fallback year for tables that don't print a year of their own (e.g. a
    # Statement of Changes in Equity whose header row is just
    # "Stated capital / Retained earnings / Total").  Prefer the year encoded
    # in the source report folder ("Annual report 2022" -> 2022).  We must NOT
    # fall back to ``generated_at`` (the extraction timestamp, e.g. 2026) —
    # that mislabels the table and makes equity rows from every report collide
    # on the same bogus year.
    fallback_year = None
    rg_years = _years_in(report_group)
    if rg_years:
        fallback_year = max(rg_years)

    # Canonical report folder label so re-runs share one report_key.
    if report_group and fallback_year and report_type == "annual":
        report_group = f"Annual Report {fallback_year}"

    ok_keys_in_doc = _ok_statement_keys_in_doc(doc)

    table_docs = list(
        iter_table_documents(
            doc,
            company_slug=slug,
            report_type=report_type,
            company_name=company_name,
            fallback_year=fallback_year,
            report_group=report_group,
            source_pdf=source_pdf,
        )
    )

    # Force every table onto the folder year so UI year tabs never miss a
    # statement due to a comparative header year.
    if fallback_year is not None:
        for td in table_docs:
            td["year"] = fallback_year
            if report_type == "annual":
                td["report_key"] = report_group or td.get("report_key") or "default"

    written_keys = {str(td.get("statement_key")) for td in table_docs}
    dropped_ok = sorted(ok_keys_in_doc - written_keys)
    if dropped_ok:
        _log(
            f"  [db] WARNING: extracted OK but not mapped to upload docs: "
            f"{', '.join(dropped_ok)}",
            quiet=quiet,
        )

    if not table_docs:
        return {
            "ok": True,
            "tables": 0,
            "company_slug": slug,
            "note": "no usable tables found in results",
            "dropped_ok_keys": dropped_ok,
        }

    resolved_name = company_name or table_docs[0].get("company_name") or slug

    own_uploader = uploader is None
    try:
        if uploader is None:
            r_uri, r_db = resolve_mongo_config(uri, db_name)
            uploader = MongoUploader(r_uri, r_db)
    except Exception as ex:
        return {
            "ok": False,
            "error": f"MongoDB connection failed: {ex}",
            "tables": 0,
            "company_slug": slug,
        }

    written = 0
    removed = 0
    errors: list[str] = []
    try:
        uploader.upsert_company(slug, resolved_name)

        # Surgical cleanup: only replace statement_keys present in THIS upload.
        # Core FS keys omitted from this payload (e.g. cash_flows) are kept.
        report_key = table_docs[0].get("report_key") or "default"
        scopes = {
            (td.get("year"), td.get("quarter")) for td in table_docs
        }
        try:
            removed = uploader.replace_report_scope(
                company_slug=slug,
                report_type=report_type,
                report_key=report_key,
                scopes=scopes,
                statement_keys=written_keys,
            )
        except Exception as ex:
            errors.append(f"cleanup failed: {ex}")

        for td in table_docs:
            try:
                uploader.upsert_table(td)
                written += 1
            except Exception as ex:
                errors.append(
                    f"{td.get('statement_key')}#{td.get('table_index')}: {ex}"
                )

        # Post-write verify: every OK core key from the results must exist now.
        core = (
            CORE_ANNUAL_STATEMENT_KEYS
            if report_type == "annual"
            else CORE_QUARTERLY_FAMILIES
        )
        expected_core = sorted(k for k in ok_keys_in_doc if k in core)
        if expected_core and fallback_year is not None:
            found = set(
                uploader.db[TABLES_COLLECTION].distinct(
                    "statement_key",
                    {
                        "company_slug": slug,
                        "report_type": report_type,
                        "year": fallback_year,
                        "statement_key": {"$in": expected_core},
                    },
                )
            )
            missing_core = [k for k in expected_core if k not in found]
            if missing_core:
                msg = (
                    "core statements extracted OK but missing after upload: "
                    + ", ".join(missing_core)
                )
                errors.append(msg)
                _log(f"  [db] ERROR: {msg}", quiet=quiet)
    finally:
        if own_uploader:
            uploader.close()

    _log(
        f"  [db] {slug} ({report_type}): upserted {written}/{len(table_docs)} "
        f"table(s), removed {removed} stale; keys={sorted(written_keys)}",
        quiet=quiet,
    )
    return {
        "ok": len(errors) == 0,
        "tables": written,
        "removed": removed,
        "attempted": len(table_docs),
        "company_slug": slug,
        "company_name": resolved_name,
        "report_type": report_type,
        "report_group": report_group,
        "year": table_docs[0].get("year"),
        "quarter": table_docs[0].get("quarter"),
        "statement_keys": sorted(written_keys),
        "dropped_ok_keys": dropped_ok,
        "errors": errors,
    }


def upload_results_file(
    results_path: str | Path,
    report_type: str,
    *,
    company_slug: str | None = None,
    company_name: str | None = None,
    report_group: str | None = None,
    source_pdf: str | None = None,
    uri: str | None = None,
    db_name: str | None = None,
    uploader: "MongoUploader | None" = None,
    quiet: bool = False,
) -> dict:
    """Load one ``*_results.json`` file and upsert every table it contains."""
    results_path = Path(results_path)
    if not results_path.exists():
        return {
            "ok": False,
            "error": f"results file not found: {results_path}",
            "tables": 0,
        }
    try:
        doc = json.loads(results_path.read_text(encoding="utf-8"))
    except Exception as ex:
        return {"ok": False, "error": f"could not parse JSON: {ex}", "tables": 0}

    slug = company_slug or results_path.parent.name
    return upload_results_data(
        doc,
        report_type,
        company_slug=slug,
        company_name=company_name,
        report_group=report_group,
        source_pdf=source_pdf,
        uri=uri,
        db_name=db_name,
        uploader=uploader,
        quiet=quiet,
    )


def upload_company_dir(
    company_dir: str | Path,
    *,
    company_key: str | None = None,
    uri: str | None = None,
    db_name: str | None = None,
    uploader: "MongoUploader | None" = None,
    quiet: bool = False,
) -> list[dict]:
    """Find annual + quarterly results JSON inside one company folder and
    upload each.  File-name convention (matches the extractors):

        <key>_results.json            -> annual
        <key>_quarterly_results.json  -> quarterly
    """
    company_dir = Path(company_dir)
    slug = company_key or company_dir.name
    summaries: list[dict] = []

    annual = company_dir / f"{slug}_results.json"
    quarterly = company_dir / f"{slug}_quarterly_results.json"

    # Fall back to globbing when the names don't match the slug exactly.
    if not annual.exists():
        cands = [
            p for p in company_dir.glob("*_results.json")
            if "quarterly" not in p.name.lower()
        ]
        annual = cands[0] if cands else annual
    if not quarterly.exists():
        cands = list(company_dir.glob("*_quarterly_results.json"))
        quarterly = cands[0] if cands else quarterly

    if annual.exists():
        summaries.append(
            upload_results_file(
                annual, "annual", company_slug=slug,
                uri=uri, db_name=db_name, uploader=uploader, quiet=quiet,
            )
        )
    if quarterly.exists():
        summaries.append(
            upload_results_file(
                quarterly, "quarterly", company_slug=slug,
                uri=uri, db_name=db_name, uploader=uploader, quiet=quiet,
            )
        )
    return summaries


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Upload extracted financial tables into MongoDB "
                    "(one document per table)."
    )
    ap.add_argument("--results", type=Path,
                    help="Path to a single *_results.json file.")
    ap.add_argument("--type", choices=list(REPORT_TYPES),
                    help="Report type for --results (annual | quarterly).")
    ap.add_argument("--company-dir", type=Path,
                    help="Company folder containing results JSON file(s).")
    ap.add_argument("--company-key", default=None,
                    help="Override the company slug (defaults to folder name).")
    ap.add_argument("--testing-dir", type=Path,
                    help="Walk every company sub-folder under this directory.")
    ap.add_argument("--mongo-uri", default=None,
                    help="MongoDB URI (else MONGO_URI env / backend/.env).")
    ap.add_argument("--db-name", default=None,
                    help="MongoDB database (else MONGO_DB_NAME env / backend/.env).")
    ap.add_argument("--quiet", action="store_true", help="Reduce logging.")
    args = ap.parse_args(argv)

    r_uri, r_db = resolve_mongo_config(args.mongo_uri, args.db_name)

    try:
        uploader = MongoUploader(r_uri, r_db)
    except Exception as ex:
        print(f"ERROR: cannot connect to MongoDB at {r_uri} ({ex})", file=sys.stderr)
        return 1

    print(f"[db] connected: {r_uri}  db={r_db}", flush=True)

    total_tables = 0
    total_files = 0
    try:
        if args.results:
            if not args.type:
                print("ERROR: --type is required with --results", file=sys.stderr)
                return 2
            s = upload_results_file(
                args.results, args.type,
                company_slug=args.company_key,
                uploader=uploader, quiet=args.quiet,
            )
            total_files += 1
            total_tables += s.get("tables", 0)
            if not s.get("ok"):
                print(f"  [warn] {s.get('error') or s.get('errors')}", flush=True)

        elif args.company_dir:
            for s in upload_company_dir(
                args.company_dir, company_key=args.company_key,
                uploader=uploader, quiet=args.quiet,
            ):
                total_files += 1
                total_tables += s.get("tables", 0)

        elif args.testing_dir:
            tdir = args.testing_dir
            if not tdir.is_dir():
                print(f"ERROR: not a directory: {tdir}", file=sys.stderr)
                return 2
            for child in sorted(p for p in tdir.iterdir() if p.is_dir()):
                for s in upload_company_dir(
                    child, uploader=uploader, quiet=args.quiet,
                ):
                    total_files += 1
                    total_tables += s.get("tables", 0)
        else:
            print("ERROR: pass --results, --company-dir, or --testing-dir",
                  file=sys.stderr)
            return 2
    finally:
        uploader.close()

    print(f"[db] done — {total_tables} table(s) upserted from "
          f"{total_files} file(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
