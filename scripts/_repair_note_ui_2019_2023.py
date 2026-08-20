"""Repair wrapped/split note UI tables and re-extract plumber years 2019-2023."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from comb_note_extractor import resolve_annual_pdf
from comb_note_registry import build_note_capture_plan
from comb_workbook_store import get_db
from extract_comb_note_ui_tables import repair_and_align_ui_tables, repair_note_ui_tables
from generate_comb_model import COMMERCIAL_BANK_SLUG
from _fill_note_ui_from_pdf import tables_from_doc

PLUMBER_YEARS = {2021, 2023}


def main() -> int:
    db, client = get_db()
    now = datetime.now(timezone.utc)
    try:
        for year in (2019, 2020, 2021, 2022, 2023):
            pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
            plan = build_note_capture_plan(
                db, COMMERCIAL_BANK_SLUG, year, pdf_path=pdf_path
            )
            keys = [
                str(item.get("note_statement_key"))
                for item in plan
                if item.get("has_note_table") and item.get("note_statement_key")
            ]
            docs = list(
                db.financial_tables.find(
                    {
                        "company_slug": COMMERCIAL_BANK_SLUG,
                        "year": year,
                        "report_type": "annual",
                        "statement_key": {"$in": keys},
                    }
                )
            )
            refill = year in PLUMBER_YEARS
            print(
                f"[{year}] docs={len(docs)} refill_from_pdf={refill}",
                flush=True,
            )
            pdf_obj = None
            if refill and pdf_path and pdf_path.exists():
                pdf_obj = pdfplumber.open(str(pdf_path))
            try:
                updated = 0
                failed = 0
                for doc in docs:
                    tables = None
                    if refill and pdf_obj is not None:
                        tables = tables_from_doc(pdf_obj, doc)
                    if not tables:
                        tables = repair_note_ui_tables(
                            list(doc.get("ui_extracted_tables") or [])
                        )
                    if not tables:
                        failed += 1
                        print(
                            f"  FAIL {doc.get('statement_key')} {doc.get('parent_label')}",
                            flush=True,
                        )
                        continue
                    payload = {
                        "ui_extracted_tables": tables,
                        "ui_extracted_at": now,
                    }
                    if refill:
                        payload["ui_extraction_method"] = "pdfplumber_crop"
                    db.financial_tables.update_one(
                        {"_id": doc["_id"]},
                        {"$set": payload},
                    )
                    updated += 1
                    if doc.get("statement_key") in {
                        "note_13_1",
                        "note_13_2",
                        "note_12",
                        "note_23",
                    }:
                        rows = tables[0].get("rows") or []
                        print(
                            f"  {doc.get('statement_key')} rows={len(rows)} "
                            f"{[str((r.get('cells') or [''])[0])[:70] for r in rows[:8]]}",
                            flush=True,
                        )
                print(f"[{year}] updated={updated} failed={failed}", flush=True)
            finally:
                if pdf_obj is not None:
                    pdf_obj.close()
        aligned = repair_and_align_ui_tables(db, [2019, 2020, 2021, 2022, 2023])
        print(f"[align] {aligned}", flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
