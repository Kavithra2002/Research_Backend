import sys

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from comb_note_extractor import _join_wrapped_grid_rows, _words_table_rows, resolve_annual_pdf
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG
from _fill_note_ui_from_pdf import _crop_bbox

db, client = get_db()
pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, 2019)
doc = db.financial_tables.find_one(
    {
        "company_slug": COMMERCIAL_BANK_SLUG,
        "year": 2019,
        "statement_key": "note_13_1",
    }
)
print("pdf", pdf_path)
print("crops", doc.get("crop_regions"))
with pdfplumber.open(str(pdf_path)) as pdf:
    for i, crop in enumerate(doc.get("crop_regions") or []):
        page_num = int(crop.get("page") or 0)
        page = pdf.pages[page_num - 1]
        x0, y0, x1, y1 = _crop_bbox(crop)
        x0 = max(0.0, min(x0, float(page.width) - 1))
        x1 = max(x0 + 1.0, min(x1, float(page.width)))
        y0 = max(0.0, min(y0, float(page.height) - 1))
        y1 = max(y0 + 1.0, min(y1, float(page.height)))
        cropped = page.crop((x0, y0, x1, y1))
        raw = _words_table_rows(cropped)
        print(f"\n--- crop {i} page={page_num} bbox=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}) rows={len(raw)} ---")
        for row in raw:
            blob = " ".join(str(c) for c in row).lower()
            if any(
                key in blob
                for key in ("impaired", "accrued", "advances to other", "other interest", "total")
            ):
                print(row)
client.close()
