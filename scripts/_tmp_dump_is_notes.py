import sys

sys.stdout.reconfigure(encoding="utf-8")

from comb_fs_pdf_extract import find_statement_pages
from comb_note_extractor import _words_table_rows, resolve_annual_pdf
from comb_note_registry import (
    _detect_note_page_columns,
    _parse_note_and_page,
    build_note_capture_plan,
)
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG

import pdfplumber

db, client = get_db()
pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, 2023)
pages = find_statement_pages(pdf_path, "income_statement", max_pages=4)
print("IS pages", pages)
with pdfplumber.open(str(pdf_path)) as pdf:
    page_num = pages[1] if len(pages) > 1 else pages[0]
    rows = _words_table_rows(pdf.pages[page_num - 1])
    note_ci, page_ci = _detect_note_page_columns(rows)
    print(f"page {page_num} note_ci={note_ci} page_ci={page_ci}")
    for row in rows[:30]:
        label = str(row[0]).strip()[:60] if row else ""
        note = row[note_ci] if note_ci is not None and note_ci < len(row) else ""
        parsed, pno = _parse_note_and_page(str(note), None)
        print(f"  {label:60} raw={str(note)[:20]!r} parsed={parsed}")

print("\n=== 2023 plan IS-like ===")
plan = build_note_capture_plan(db, COMMERCIAL_BANK_SLUG, 2023, pdf_path=pdf_path)
for p in plan:
    if not p.get("has_note_table"):
        continue
    fs = str(p.get("fs_label") or "")
    if any(
        x in fs.lower()
        for x in (
            "gross",
            "interest",
            "fee",
            "trading",
            "personnel",
            "impairment",
            "depreciation",
            "operating",
            "tax",
        )
    ):
        print(f"  ref={p.get('note_ref')} sk={p.get('note_statement_key')} fs={fs}")

client.close()
