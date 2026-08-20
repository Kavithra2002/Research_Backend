import sys

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from comb_note_extractor import _VAL_TOKEN_RE, _is_val_token, resolve_annual_pdf
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
crop = (doc.get("crop_regions") or [])[1]
with pdfplumber.open(str(pdf_path)) as pdf:
    page = pdf.pages[int(crop["page"]) - 1]
    print("page size", page.width, page.height)
    x0, y0, x1, y1 = _crop_bbox(crop)
    cropped = page.crop((x0, y0, x1, y1))
    words = cropped.extract_words(
        x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False, use_text_flow=False
    )
    words.sort(key=lambda w: (round(w["top"], 0), w["x0"]))
    print(f"crop words={len(words)} bbox=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f})")
    for w in words[:80]:
        text = w["text"]
        print(
            f"  y={w['top']:6.1f} x0={w['x0']:6.1f} x1={w['x1']:6.1f} "
            f"val={str(_is_val_token(text)):5} {text!r}"
        )
client.close()
