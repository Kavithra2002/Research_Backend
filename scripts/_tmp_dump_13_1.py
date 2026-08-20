import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG

db, client = get_db()
for year in (2019, 2021, 2022, 2023):
    doc = db.financial_tables.find_one(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": year,
            "statement_key": "note_13_1",
        },
        {"ui_extracted_tables": 1, "ui_extraction_method": 1, "source_page": 1},
    )
    print(f"\n==== {year} method={doc.get('ui_extraction_method') if doc else None} page={doc.get('source_page') if doc else None} ====")
    tables = (doc or {}).get("ui_extracted_tables") or []
    for ti, t in enumerate(tables):
        print(" headers:")
        for h in t.get("header_rows") or []:
            print("  ", h)
        print(" rows:")
        for row in t.get("rows") or []:
            print("  ", row.get("style"), row.get("cells"))
client.close()
