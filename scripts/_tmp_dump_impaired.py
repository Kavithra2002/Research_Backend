import sys

sys.stdout.reconfigure(encoding="utf-8")

from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG

db, client = get_db()
keys = ("impaired", "accrued", "other interest", "total", "debt and other", "fair value through other")
for year in (2019, 2020, 2021, 2022, 2023):
    doc = db.financial_tables.find_one(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": year,
            "statement_key": "note_13_1",
        },
        {
            "ui_extracted_tables": 1,
            "ui_extraction_method": 1,
            "source_page": 1,
            "source_pages": 1,
            "crop_regions": 1,
        },
    )
    crops = len((doc or {}).get("crop_regions") or [])
    method = (doc or {}).get("ui_extraction_method")
    page = (doc or {}).get("source_page")
    pages = (doc or {}).get("source_pages")
    print(f"\n==== {year} method={method} page={page} pages={pages} crops={crops} ====")
    tables = (doc or {}).get("ui_extracted_tables") or []
    for table in tables:
        print(" headers:")
        for header in table.get("header_rows") or []:
            print("  ", header)
        print(" matching rows:")
        for row in table.get("rows") or []:
            cells = row.get("cells") or []
            label = str(cells[0] if cells else "").lower()
            if any(key in label for key in keys):
                print("  ", row.get("style"), cells)
client.close()
