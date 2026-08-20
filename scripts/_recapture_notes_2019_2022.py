"""Clear COMB note UI tables, recapture by note heading, then extract 2019-2022."""
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

YEARS = (2019, 2020, 2021, 2022, 2023)


def _clear_ui(db, years: tuple[int, ...]) -> int:
    result = db.financial_tables.update_many(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": {"$in": list(years)},
            "report_type": "annual",
            "statement_key": {"$regex": r"^note_"},
        },
        {
            "$unset": {
                "ui_extracted_tables": "",
                "ui_extraction_method": "",
                "ui_extraction_model": "",
                "ui_extracted_at": "",
            }
        },
    )
    return int(result.modified_count)


def _fill_missing_from_pdf(db, years: tuple[int, ...]) -> None:
    now = datetime.now(timezone.utc)
    for year in years:
        pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
        if not pdf_path or not pdf_path.exists():
            continue
        docs = list(
            db.financial_tables.find(
                {
                    "company_slug": COMMERCIAL_BANK_SLUG,
                    "year": year,
                    "report_type": "annual",
                    "statement_key": {"$regex": r"^note_"},
                    "$or": [
                        {"ui_extracted_tables": {"$exists": False}},
                        {"ui_extracted_tables": []},
                    ],
                    "crop_regions.0": {"$exists": True},
                }
            )
        )
        if not docs:
            continue
        print(f"[plumber-fill] {year} missing={len(docs)}", flush=True)
        with pdfplumber.open(str(pdf_path)) as pdf:
            for doc in docs:
                tables = tables_from_doc(pdf, doc)
                if not tables:
                    print(
                        f"  FAIL {doc.get('statement_key')} {doc.get('parent_label')}",
                        flush=True,
                    )
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
                rows = tables[0].get("rows") or []
                print(
                    f"  {doc.get('statement_key')} {doc.get('parent_label')} "
                    f"rows={len(rows)} {(rows[0].get('cells') or [''])[0][:50] if rows else ''}",
                    flush=True,
                )


def main() -> int:
    db, client = get_db()
    try:
        cleared = _clear_ui(db, YEARS)
        print(f"cleared ui tables on {cleared} note docs", flush=True)
        clear_extraction_caches()
        for year in YEARS:
            pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
            print(f"\n[capture] {year} pdf={pdf_path}", flush=True)
            summary = capture_note_images_for_company(
                db,
                COMMERCIAL_BANK_SLUG,
                year,
                pdf_path=pdf_path,
                force=True,
            )
            results = summary.get("capture_results") or []
            ok = sum(1 for item in results if item.get("ok"))
            print(
                f"[capture] {year} ok={ok}/{len(results)} notes={summary.get('groups_with_note_ref')}",
                flush=True,
            )
            nci = [
                item
                for item in results
                if "non-controlling" in str(item.get("parent_label") or "").lower()
            ]
            for item in nci:
                print("  NCI", item, flush=True)

        print("\n[openai] skipped — API credits exhausted; filling from captured table crops", flush=True)

        _fill_missing_from_pdf(db, YEARS)

        print("\n=== NCI sample ===", flush=True)
        for year in YEARS:
            docs = list(
                db.financial_tables.find(
                    {
                        "company_slug": COMMERCIAL_BANK_SLUG,
                        "year": year,
                        "parent_label": {"$regex": r"non-controlling", "$options": "i"},
                    }
                )
            )
            for doc in docs:
                tables = doc.get("ui_extracted_tables") or []
                rows = (tables[0].get("rows") if tables else []) or []
                print(
                    f"{year} {doc.get('statement_key')} page={doc.get('source_pages')} "
                    f"method={doc.get('ui_extraction_method')} rows={len(rows)}",
                    flush=True,
                )
                for row in rows[:10]:
                    print("   ", (row.get("cells") or [""])[0][:80], flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
