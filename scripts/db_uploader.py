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
            yield {
                "company_slug": company_slug,
                "company_name": resolved_company_name,
                "year": year,
                "report_type": report_type,
                "report_group": report_group,
                "report_key": report_key,
                "quarter": quarter,
                "period_label": period,
                "statement_key": statement_key,
                "statement_title": statement_title,
                "statement_label": statement_label,
                "table_index": table_index,
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
    ) -> int:
        """Delete stale tables before re-inserting a report.

        Guarantees a single canonical set of tables per
        ``(company_slug, report_type, year, quarter)`` so re-uploading the
        same company/year — even under a *different* ``report_key`` (e.g. a
        renamed report folder) — never leaves duplicate-year documents
        behind. Also clears the same ``report_key`` to drop tables that no
        longer exist in the new extraction (e.g. fewer statements).
        """
        # Always clear the SAME report_key (re-running a report replaces its
        # own tables).  The (year, quarter) scope deletion is only meant to
        # clean LEGACY duplicates that predate report_key — it must NEVER
        # touch a sibling report that has its own distinct report_key, or a
        # table whose derived year happens to match (e.g. a comparative-year
        # column or a fallback year) would silently wipe another report's
        # data.  We therefore restrict year-scope deletes to rows whose
        # report_key is null/empty/missing.  ({"$in": [None, ""]} also matches
        # documents that lack the field entirely.)
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
        return int(getattr(result, "deleted_count", 0) or 0)

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

    if not table_docs:
        return {
            "ok": True,
            "tables": 0,
            "company_slug": slug,
            "note": "no usable tables found in results",
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

        # Drop any stale tables for this report's (year, quarter) scope so the
        # same company/year can never end up stored twice (e.g. when the
        # source report folder was renamed, producing a new report_key).
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
    finally:
        if own_uploader:
            uploader.close()

    _log(
        f"  [db] {slug} ({report_type}): upserted {written}/{len(table_docs)} "
        f"table(s), removed {removed} stale",
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
