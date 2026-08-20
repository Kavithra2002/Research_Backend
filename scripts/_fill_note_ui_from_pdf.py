"""Fill COMB note UI tables from cropped PDF regions (no OpenAI)."""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from comb_note_capture import _raw_to_financial_table
from comb_note_extractor import _words_table_rows, resolve_annual_pdf
from comb_note_registry import build_note_capture_plan
from comb_workbook_store import get_db
from extract_comb_note_ui_tables import (
    _merge_segment_tables,
    _merge_year_unit_header_rows,
    note_table_missing_amounts,
    repair_and_align_ui_tables,
    repair_note_ui_tables,
)
from generate_comb_model import COMMERCIAL_BANK_SLUG


def _style_for_label(label: str) -> str:
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    if re.fullmatch(r"(?i)totals?", text) or re.match(r"(?i)^total\b", text):
        return "total"
    return ""


def _table_from_raw(raw: list[list[str]], page_num: int) -> dict[str, Any] | None:
    if not raw:
        return None
    payload = _raw_to_financial_table(
        raw, page_num=page_num, note_ref="", title=""
    )
    header_rows = _merge_year_unit_header_rows(
        [[str(c) for c in row] for row in (payload.get("header_rows") or [])]
    )
    rows: list[dict[str, Any]] = []
    for row in payload.get("rows") or []:
        cells = [str(c) for c in (row.get("cells") or [])]
        if not any(c.strip() for c in cells):
            continue
        label = cells[0] if cells else ""
        rows.append({"cells": cells, "style": _style_for_label(label)})
    if not rows:
        return None
    return {"caption": None, "header_rows": header_rows, "rows": rows}


def _crop_bbox(crop: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(crop["x0"]),
        float(crop["y0"]),
        float(crop["x1"]),
        float(crop["y1"]),
    )


def tables_from_doc(pdf, doc: dict[str, Any]) -> list[dict[str, Any]]:
    crops = list(doc.get("crop_regions") or [])
    pages = [int(p) for p in (doc.get("source_pages") or []) if p]
    if not pages and doc.get("source_page"):
        pages = [int(doc["source_page"])]
    segments: list[dict[str, Any]] = []

    if crops:
        for crop in crops:
            page_num = int(crop.get("page") or 0)
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            x0, y0, x1, y1 = _crop_bbox(crop)
            page = pdf.pages[page_num - 1]
            x0 = max(0.0, min(x0, float(page.width) - 1))
            x1 = max(x0 + 1.0, min(x1, float(page.width)))
            y0 = max(0.0, min(y0, float(page.height) - 1))
            y1 = max(y0 + 1.0, min(y1, float(page.height)))
            try:
                cropped = page.crop((x0, y0, x1, y1))
            except Exception:
                cropped = page
            table = _table_from_raw(_words_table_rows(cropped), page_num)
            if table:
                segments.append(table)
    elif pages:
        for page_num in pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            table = _table_from_raw(
                _words_table_rows(pdf.pages[page_num - 1]), page_num
            )
            if table:
                segments.append(table)

    return repair_note_ui_tables(_merge_segment_tables(segments))


def fill_note_ui_for_company(
    db,
    company_slug: str,
    years: list[int],
    *,
    force: bool = False,
    pdf_path=None,
) -> dict[str, Any]:
    """Fill ui_extracted_tables from cropped PDF regions for any company."""
    now = datetime.now(timezone.utc)
    filled_total = 0
    failed_total = 0
    for year in years:
        year_pdf = pdf_path or resolve_annual_pdf(db, company_slug, year)
        plan = build_note_capture_plan(
            db, company_slug, year, pdf_path=year_pdf
        )
        keys = [
            str(item.get("note_statement_key"))
            for item in plan
            if item.get("has_note_table") and item.get("note_statement_key")
        ]
        if not keys:
            continue
        docs = list(
            db.financial_tables.find(
                {
                    "company_slug": company_slug,
                    "year": year,
                    "report_type": "annual",
                    "statement_key": {"$in": keys},
                }
            )
        )
        targets = [
            doc
            for doc in docs
            if force or not (doc.get("ui_extracted_tables") or [])
        ]
        print(
            f"[local] {company_slug} {year} queued={len(targets)}/{len(keys)} "
            f"pdf={year_pdf}",
            flush=True,
        )
        if not targets or not year_pdf or not Path(year_pdf).exists():
            continue
        filled = 0
        failed = 0
        with pdfplumber.open(str(year_pdf)) as pdf:
            for doc in targets:
                tables = tables_from_doc(pdf, doc)
                sk = doc.get("statement_key")
                label = doc.get("parent_label") or doc.get("note_ref")
                if not tables:
                    failed += 1
                    print(f"  FAIL {year} {sk} {label}", flush=True)
                    continue
                db.financial_tables.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "ui_extracted_tables": tables,
                            "ui_extraction_method": "pdfplumber_crop",
                            "ui_extraction_model": None,
                            "ui_extracted_at": now,
                        }
                    },
                )
                filled += 1
                rows = sum(len(t.get("rows") or []) for t in tables)
                gap = None
                for table in tables:
                    gap = note_table_missing_amounts(table)
                    if gap:
                        break
                extra = f" gap={gap}" if gap else ""
                print(
                    f"  {year} {sk} {label} rows={rows}{extra}",
                    flush=True,
                )
        filled_total += filled
        failed_total += failed
        print(
            f"[local] {company_slug} {year} filled={filled} failed={failed}",
            flush=True,
        )

    aligned = repair_and_align_ui_tables(db, years, company_slug=company_slug)
    print(
        f"[local] repaired {aligned.get('repair_docs', 0)} doc(s); "
        f"aligned {aligned['rows']} row(s) on {aligned['docs']} doc(s)",
        flush=True,
    )
    return {
        "filled": filled_total,
        "failed": failed_total,
        "aligned": aligned,
    }


def run(
    *,
    years: list[int],
    force: bool = False,
    company_slug: str = COMMERCIAL_BANK_SLUG,
) -> dict[str, Any]:
    db, client = get_db()
    try:
        return fill_note_ui_for_company(
            db, company_slug, years, force=force
        )
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", default="2019,2020,2021,2022,2023")
    parser.add_argument(
        "--company-slug",
        default=COMMERCIAL_BANK_SLUG,
        help="Company slug (default: Commercial Bank pilot).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even when ui_extracted_tables already exists.",
    )
    args = parser.parse_args()
    years = [int(value.strip()) for value in args.years.split(",") if value.strip()]
    result = run(
        years=years, force=args.force, company_slug=args.company_slug.strip()
    )
    print(result, flush=True)
    return 0 if result.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
