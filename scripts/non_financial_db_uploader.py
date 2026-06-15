"""
non_financial_db_uploader.py
============================
Persist the structured non-financial data produced by
``non_financial_data_script.py`` into MongoDB, **company-wise**, so it can be
navigated as:

    Company  ->  Report (year / group)  ->  Category  ->  Metric

Two collections are written (alongside the financial ``companies`` registry the
rest of the system already uses):

    ``companies``               : one doc per company (shared registry)
    ``non_financial_data``      : ONE doc per (company, report) — the canonical
                                  company-wise record, with nested categories.
    ``non_financial_metrics``   : ONE doc per (company, report, category,
                                  metric) — a flat, table-friendly projection
                                  for querying / comparing across companies.

Re-running the same report upserts in place (no duplicates) thanks to unique
compound indexes, and stale metrics from a previous run of the same report are
cleared first.

The MongoDB connection settings are shared with ``db_uploader.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Reuse the exact same connection resolution as the financial uploader.
from db_uploader import resolve_mongo_config


COMPANIES_COLLECTION = "companies"
NF_DATA_COLLECTION    = "non_financial_data"
NF_METRICS_COLLECTION = "non_financial_metrics"

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _years_in(text: Any) -> list[int]:
    if text is None:
        return []
    return [int(m.group(0)) for m in _YEAR_RE.finditer(str(text))]


def _derive_year(payload: dict[str, Any], report_group: str | None) -> int | None:
    """Reporting year of the report itself.

    Prefer the year(s) printed in the report ("reporting_year") and the source
    report group/folder name ("Annual Report 2025"). The ``generated_at``
    extraction timestamp is deliberately NOT used as a year candidate — it is
    "today" and would otherwise overwrite the real report year.
    """
    candidates: list[int] = []
    candidates += _years_in(payload.get("reporting_year"))
    candidates += _years_in(report_group)
    candidates = [y for y in candidates if 1990 <= y <= 2100]
    return max(candidates) if candidates else None


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB writer
# ─────────────────────────────────────────────────────────────────────────────


class NonFinancialUploader:
    """Thin pymongo wrapper that upserts companies + non-financial data."""

    def __init__(self, uri: str, db_name: str):
        from pymongo import MongoClient  # lazy import so --help works without pymongo

        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.client.admin.command("ping")
        self.db = self.client[db_name]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.db[COMPANIES_COLLECTION].create_index("slug", unique=True)
        self.db[NF_DATA_COLLECTION].create_index(
            [("company_slug", 1), ("report_key", 1)],
            unique=True,
            name="uniq_nf_company_reportkey",
        )
        self.db[NF_DATA_COLLECTION].create_index(
            [("company_slug", 1), ("year", 1)],
            name="lookup_nf_company_year",
        )
        self.db[NF_METRICS_COLLECTION].create_index(
            [("company_slug", 1), ("report_key", 1),
             ("category_key", 1), ("metric_key", 1)],
            unique=True,
            name="uniq_nfm_company_reportkey_cat_metric",
        )
        self.db[NF_METRICS_COLLECTION].create_index(
            [("category_key", 1), ("metric_key", 1), ("found", 1)],
            name="lookup_nfm_metric",
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

    def upsert_data(self, data_doc: dict[str, Any]) -> None:
        key = {
            "company_slug": data_doc["company_slug"],
            "report_key": data_doc["report_key"],
        }
        self.db[NF_DATA_COLLECTION].update_one(key, {"$set": data_doc}, upsert=True)

    def replace_metrics(
        self, *, company_slug: str, report_key: str, metric_docs: list[dict]
    ) -> int:
        """Clear this report's metrics then insert the fresh set."""
        self.db[NF_METRICS_COLLECTION].delete_many(
            {"company_slug": company_slug, "report_key": report_key}
        )
        if metric_docs:
            self.db[NF_METRICS_COLLECTION].insert_many(metric_docs, ordered=False)
        return len(metric_docs)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# High-level upload entry point
# ─────────────────────────────────────────────────────────────────────────────


def _flatten_metrics(
    payload: dict[str, Any],
    *,
    company_slug: str,
    company_name: str,
    report_key: str,
    report_group: str | None,
    year: int | None,
    source_pdf: str | None,
    uploaded_at: str,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for cat in payload.get("categories") or []:
        ckey = cat.get("key")
        ctitle = cat.get("title")
        for m in cat.get("metrics") or []:
            docs.append(
                {
                    "company_slug": company_slug,
                    "company_name": company_name,
                    "report_key": report_key,
                    "report_group": report_group,
                    "year": year,
                    "category_key": ckey,
                    "category_title": ctitle,
                    "metric_key": m.get("key"),
                    "metric_title": m.get("title"),
                    "found": bool(m.get("found")),
                    "value": m.get("value") or "",
                    "detail": m.get("detail") or "",
                    "pages": m.get("pages") or [],
                    "source_pdf": source_pdf,
                    "uploaded_at": uploaded_at,
                }
            )
    return docs


def upload_non_financial_data(
    payload: dict[str, Any],
    *,
    company_slug: str,
    company_name: str | None = None,
    report_group: str | None = None,
    source_pdf: str | None = None,
    uri: str | None = None,
    db_name: str | None = None,
    uploader: "NonFinancialUploader | None" = None,
) -> dict[str, Any]:
    """Upsert one company's non-financial data document + flat metric rows."""
    if not isinstance(payload, dict):
        return {"status": "error", "error": "payload is not an object"}

    resolved_name = company_name or payload.get("company") or company_slug
    year = _derive_year(payload, report_group)
    report_group = (report_group or "").strip() or None
    report_key = (
        report_group
        or (str(year) if year is not None else None)
        or "default"
    )
    uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    categories = payload.get("categories") or []
    found_count = int(payload.get("found_count") or sum(
        1 for c in categories for m in (c.get("metrics") or []) if m.get("found")
    ))
    total_count = int(payload.get("total_count") or sum(
        len(c.get("metrics") or []) for c in categories
    ))

    data_doc = {
        "company_slug": company_slug,
        "company_name": resolved_name,
        "year": year,
        "report_key": report_key,
        "report_group": report_group,
        "reporting_year": payload.get("reporting_year"),
        "company_overview": payload.get("company_overview") or "",
        "categories": categories,
        "found_count": found_count,
        "total_count": total_count,
        "source_pdf": source_pdf or payload.get("pdf"),
        "extraction_model": payload.get("model"),
        "extracted_at": payload.get("generated_at"),
        "usage": payload.get("usage"),
        "uploaded_at": uploaded_at,
    }

    own = uploader is None
    try:
        if uploader is None:
            r_uri, r_db = resolve_mongo_config(uri, db_name)
            uploader = NonFinancialUploader(r_uri, r_db)
    except Exception as ex:
        return {"status": "error", "error": f"MongoDB connection failed: {ex}"}

    try:
        uploader.upsert_company(company_slug, resolved_name)
        uploader.upsert_data(data_doc)
        metric_docs = _flatten_metrics(
            payload,
            company_slug=company_slug,
            company_name=resolved_name,
            report_key=report_key,
            report_group=report_group,
            year=year,
            source_pdf=data_doc["source_pdf"],
            uploaded_at=uploaded_at,
        )
        written = uploader.replace_metrics(
            company_slug=company_slug, report_key=report_key,
            metric_docs=metric_docs,
        )
    except Exception as ex:
        return {"status": "error", "error": str(ex)}
    finally:
        if own:
            uploader.close()

    return {
        "status": "ok",
        "company_slug": company_slug,
        "company_name": resolved_name,
        "reportKey": report_key,
        "year": year,
        "categories": len(categories),
        "metrics": written,
        "found": found_count,
        "total": total_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Upload structured non-financial data into MongoDB.")
    ap.add_argument("--data", type=Path, required=True,
                    help="Path to a non_financial_data.json file.")
    ap.add_argument("--company-slug", default=None,
                    help="Company slug (defaults to parent folder name).")
    ap.add_argument("--company-name", default=None)
    ap.add_argument("--report-group", default=None)
    ap.add_argument("--source-pdf", default=None)
    ap.add_argument("--mongo-uri", default=None)
    ap.add_argument("--db-name", default=None)
    args = ap.parse_args(argv)

    if not args.data.exists():
        print(f"ERROR: data file not found: {args.data}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(args.data.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"ERROR: could not parse JSON: {ex}", file=sys.stderr)
        return 2

    slug = args.company_slug or args.data.parent.parent.name
    summary = upload_non_financial_data(
        payload,
        company_slug=slug,
        company_name=args.company_name,
        report_group=args.report_group,
        source_pdf=args.source_pdf,
        uri=args.mongo_uri,
        db_name=args.db_name,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
