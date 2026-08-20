import sys

sys.stdout.reconfigure(encoding="utf-8")

from comb_note_registry import build_note_capture_plan
from comb_workbook_store import COMB_COLLECTION, get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG

IS_LABELS = [
    "Gross income",
    "Interest income",
    "Less: Interest expense",
    "Net interest income",
    "Fee and commission income",
    "Less: Fee and commission expense",
    "Net fee and commission income",
    "Net gains/(losses) from trading",
    "Net gains/(losses) from derecognition of financial assets",
    "Net other operating income",
    "Less: Impairment charges and other losses",
    "Personnel expenses",
    "Depreciation and amortisation",
    "Other operating expenses",
    "Less: Taxes on financial services",
    "Less: Income tax expense",
    "Less: Income tax expense/(reversal)",
]

db, client = get_db()
for year in (2019, 2020, 2021, 2022, 2023):
    print(f"\n======== {year} ========")
    plan = build_note_capture_plan(db, COMMERCIAL_BANK_SLUG, year)
    plan_n = sum(1 for p in plan if p.get("has_note_table"))
    ui_n = db.financial_tables.count_documents(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": year,
            "ui_extracted_tables.0": {"$exists": True},
        }
    )
    print(f"plan_notes={plan_n} ui_tables={ui_n}")
    for label in IS_LABELS:
        cell = db[COMB_COLLECTION].find_one(
            {
                "company_slug": COMMERCIAL_BANK_SLUG,
                "year": year,
                "sheet": "FS",
                "label": label,
                "template_row": {"$ne": None},
            },
            {"value": 1, "note_ref": 1, "note_source": 1, "template_row": 1},
        )
        if not cell:
            cell = db[COMB_COLLECTION].find_one(
                {
                    "company_slug": COMMERCIAL_BANK_SLUG,
                    "year": year,
                    "sheet": "FS",
                    "label": label,
                },
                {"value": 1, "note_ref": 1, "note_source": 1, "template_row": 1},
            )
        if not cell:
            print(f"  {label:55} NO_CELL")
            continue
        ns = cell.get("note_source") or {}
        sk = ns.get("statement_key")
        doc = (
            db.financial_tables.find_one(
                {
                    "company_slug": COMMERCIAL_BANK_SLUG,
                    "year": year,
                    "statement_key": sk,
                },
                {
                    "ui_extracted_tables": 1,
                    "capture_verify": 1,
                    "parent_label": 1,
                    "capture_files": 1,
                },
            )
            if sk
            else None
        )
        ui = (doc or {}).get("ui_extracted_tables") or []
        rows = sum(len(t.get("rows") or []) for t in ui)
        v = ((doc or {}).get("capture_verify") or {}).get("value_verified")
        print(
            f"  {label:55} val={cell.get('value')} note={cell.get('note_ref')} "
            f"sk={sk} parent={None if not doc else doc.get('parent_label')!r} "
            f"verified={v} ui_rows={rows} files={None if not doc else doc.get('capture_files')}"
        )

client.close()
