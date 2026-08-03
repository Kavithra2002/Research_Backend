"""Force re-capture 2019 notes with progress, then verify vs FS."""
from __future__ import annotations

import time
from pathlib import Path

from comb_note_capture import (
    capture_note_images_for_company,
    clear_extraction_caches,
    verify_note_table_crops,
)
from comb_note_registry import build_note_capture_plan, parent_value_from_statements
from comb_note_extractor import resolve_annual_pdf
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG


def main() -> None:
    clear_extraction_caches()
    db, client = get_db()
    year = 2019
    pdf = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)

    years = [2019, 2020, 2021, 2022]
    cleared = db.comb_workbook_data.update_many(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": {"$in": years},
            "report_type": "annual",
        },
        {"$set": {"value": None, "status": "pending"}},
    )
    print(f"Cleared {cleared.modified_count} annual cells for {years}", flush=True)

    t0 = time.time()
    print("Starting force capture...", flush=True)
    cap = capture_note_images_for_company(
        db, COMMERCIAL_BANK_SLUG, year, pdf_path=pdf, force=True
    )
    results = cap.get("capture_results") or []
    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    verified = [r for r in ok if r.get("value_verified")]
    print(
        f"\n2019 capture: {len(ok)} ok, {len(fail)} failed, "
        f"{len(verified)} value-verified  ({time.time()-t0:.0f}s)",
        flush=True,
    )
    if fail:
        print("Failed:", flush=True)
        for r in fail[:15]:
            print(f"  note {r.get('note_ref')}: {r.get('reason')} {r.get('issues')}", flush=True)

    plan = build_note_capture_plan(db, COMMERCIAL_BANK_SLUG, year, pdf_path=pdf)
    mismatches = []
    unchecked = []
    key_ok = []
    key_refs = {"12", "13", "13.1", "13.2", "14.2", "17", "20", "29", "30", "34"}
    for item in plan:
        if not item.get("has_note_table"):
            continue
        ref = str(item.get("note_ref") or "")
        label = str(item.get("fs_label") or item.get("parent_label") or "")
        sk = item.get("note_statement_key")
        doc = db.financial_tables.find_one(
            {"company_slug": COMMERCIAL_BANK_SLUG, "year": year, "statement_key": sk}
        )
        if not doc or not doc.get("crop_regions"):
            unchecked.append(ref)
            continue
        exp_g = parent_value_from_statements(
            db, COMMERCIAL_BANK_SLUG, year, label, entity_column="group"
        )
        exp_b = parent_value_from_statements(
            db, COMMERCIAL_BANK_SLUG, year, label, entity_column="bank"
        )
        check = verify_note_table_crops(
            pdf,
            doc["crop_regions"],
            year=year,
            expected_group=exp_g,
            expected_bank=exp_b,
        )
        if exp_g is None and exp_b is None:
            unchecked.append(ref)
            continue
        row = {
            "note_ref": ref,
            "label": label[:40],
            "expected": exp_g,
            "captured": check.get("captured_group"),
            "page": doc.get("source_page"),
            "ok": bool(check.get("value_verified")),
        }
        if ref in key_refs:
            key_ok.append(row)
        if not check.get("value_verified"):
            mismatches.append(row)

    print(f"\nValue check: {len(plan)} notes in plan", flush=True)
    print(f"  Mismatches: {len(mismatches)}", flush=True)
    print(f"  No FS anchor: {len(unchecked)}", flush=True)
    print("\nKEY NOTES:", flush=True)
    for m in key_ok:
        flag = "OK" if m["ok"] else "FAIL"
        print(
            f"  [{flag}] note {m['note_ref']:<5} FS={m['expected']} "
            f"cap={m['captured']} page={m['page']} :: {m['label']}",
            flush=True,
        )
    if mismatches:
        print("\nMismatched notes:", flush=True)
        for m in mismatches[:25]:
            print(
                f"  note {m['note_ref']:<6} FS={m['expected']} cap={m['captured']} "
                f"page={m['page']} :: {m['label']}",
                flush=True,
            )
    client.close()


if __name__ == "__main__":
    main()
