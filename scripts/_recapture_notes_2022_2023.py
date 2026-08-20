"""Recapture 2022-2023 notes after NCI table-end fix, then fill UI tables."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

from comb_note_capture import capture_note_images_for_company, clear_extraction_caches
from comb_note_extractor import resolve_annual_pdf
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG
from _fill_note_ui_from_pdf import tables_from_doc
import pdfplumber

YEARS = (2022, 2023)


def main() -> int:
    db, client = get_db()
    try:
        clear_extraction_caches()
        for year in YEARS:
            pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
            print(f"[capture] {year}", flush=True)
            summary = capture_note_images_for_company(
                db,
                COMMERCIAL_BANK_SLUG,
                year,
                pdf_path=pdf_path,
                force=True,
            )
            results = summary.get("capture_results") or []
            ok = sum(1 for item in results if item.get("ok"))
            print(f"[capture] {year} ok={ok}/{len(results)}", flush=True)
            for item in results:
                if "non-controlling" in str(item.get("parent_label") or "").lower():
                    print("  NCI", item, flush=True)

        now = datetime.now(timezone.utc)
        for year in YEARS:
            pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
            docs = list(
                db.financial_tables.find(
                    {
                        "company_slug": COMMERCIAL_BANK_SLUG,
                        "year": year,
                        "report_type": "annual",
                        "statement_key": {"$regex": r"^note_"},
                        "crop_regions.0": {"$exists": True},
                    }
                )
            )
            print(f"[fill] {year} docs={len(docs)}", flush=True)
            with pdfplumber.open(str(pdf_path)) as pdf:
                for doc in docs:
                    tables = tables_from_doc(pdf, doc)
                    if not tables:
                        continue
                    db.financial_tables.update_one(
                        {"_id": doc["_id"]},
                        {
                            "$set": {
                                "ui_extracted_tables": tables,
                                "ui_extraction_method": "pdfplumber_crop",
                                "ui_extracted_at": now,
                            }
                        },
                    )
                    if "non-controlling" in str(doc.get("parent_label") or "").lower():
                        rows = tables[0].get("rows") or []
                        print(
                            f"  NCI {year} {doc.get('statement_key')} "
                            f"{[(r.get('cells') or [''])[0][:70] for r in rows]}",
                            flush=True,
                        )
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
