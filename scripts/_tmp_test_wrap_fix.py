import sys

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from comb_note_extractor import resolve_annual_pdf
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG
from _fill_note_ui_from_pdf import tables_from_doc

db, client = get_db()
for year in (2019, 2020, 2021, 2022, 2023):
    pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
    doc = db.financial_tables.find_one(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": year,
            "statement_key": "note_13_1",
        }
    )
    crops = len((doc or {}).get("crop_regions") or [])
    print(f"\n==== {year} crops={crops} ====")
    if not doc or not pdf_path:
        continue
    with pdfplumber.open(str(pdf_path)) as pdf:
        tables = tables_from_doc(pdf, doc)
    for table in tables:
        for row in table.get("rows") or []:
            cells = row.get("cells") or []
            label = str(cells[0] if cells else "")
            if "impaired" in label.lower() or label.lower() == "total":
                print(" ", row.get("style"), cells)
client.close()
