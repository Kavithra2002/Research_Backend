"""Recapture notes that were cropped from the income statement, then refill UI tables."""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from comb_note_capture import capture_note_images_for_plan
from comb_note_extractor import resolve_annual_pdf
from comb_note_registry import build_note_capture_plan
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG
from _fill_note_ui_from_pdf import tables_from_doc
from datetime import datetime, timezone

IS_PAGES = {2021: 189, 2023: 271}


def main() -> int:
    db, client = get_db()
    now = datetime.now(timezone.utc)
    try:
        for year, bad_page in IS_PAGES.items():
            bad_docs = list(
                db.financial_tables.find(
                    {
                        "company_slug": COMMERCIAL_BANK_SLUG,
                        "year": year,
                        "report_type": "annual",
                        "statement_key": {"$regex": "^note_"},
                        "source_page": bad_page,
                    },
                    {"note_ref": 1, "statement_key": 1, "parent_label": 1},
                )
            )
            bad_refs = {
                str(d.get("note_ref"))
                for d in bad_docs
                if d.get("note_ref")
            }
            print(
                f"[recapture] {year} IS-page notes={len(bad_refs)} {sorted(bad_refs)}",
                flush=True,
            )
            if not bad_refs:
                continue
            pdf = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
            plan = [
                item
                for item in build_note_capture_plan(
                    db, COMMERCIAL_BANK_SLUG, year, pdf_path=pdf
                )
                if item.get("has_note_table")
                and str(item.get("note_ref")) in bad_refs
            ]
            results = capture_note_images_for_plan(
                db,
                COMMERCIAL_BANK_SLUG,
                year,
                plan,
                pdf_path=pdf,
                force=True,
            )
            ok = sum(1 for r in results if r.get("ok"))
            print(f"[recapture] {year} done {ok}/{len(results)}", flush=True)
            for item in results:
                if not item.get("ok"):
                    print(f"  FAIL {item}", flush=True)

            sks = [str(d.get("statement_key")) for d in bad_docs]
            docs = list(
                db.financial_tables.find(
                    {
                        "company_slug": COMMERCIAL_BANK_SLUG,
                        "year": year,
                        "statement_key": {"$in": sks},
                    }
                )
            )
            filled = 0
            with pdfplumber.open(str(pdf)) as pdf_obj:
                for doc in docs:
                    if int(doc.get("source_page") or 0) == bad_page:
                        print(
                            f"  SKIP fill {doc.get('statement_key')} still on IS page {bad_page}",
                            flush=True,
                        )
                        continue
                    tables = tables_from_doc(pdf_obj, doc)
                    if not tables:
                        print(
                            f"  FAIL fill {doc.get('statement_key')} "
                            f"page={doc.get('source_page')}",
                            flush=True,
                        )
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
                    print(
                        f"  fill {year} {doc.get('statement_key')} "
                        f"page={doc.get('source_page')} rows={rows}",
                        flush=True,
                    )
            print(f"[fill] {year} filled={filled}/{len(docs)}", flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
