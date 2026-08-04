"""Compare 2021 COMB FS DB values vs Excel model and annual PDF (GROUP)."""
from __future__ import annotations

import json
from pathlib import Path

from _extract_align_years import (
    TOPIC_NORMS,
    label_match_key,
    load_excel_year,
    values_match,
    verify_year,
)
from comb_annual_fs_pdf_verify import (
    annual_values_match,
    build_annual_fs_pdf_index,
    _pdf_lookup_with_fallback,
)
from comb_note_extractor import resolve_annual_pdf
from comb_workbook_store import COMB_COLLECTION, get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG, norm_label

YEAR = 2021


def main() -> int:
    db, _client = get_db()
    slug = COMMERCIAL_BANK_SLUG
    docs = list(
        db[COMB_COLLECTION].find(
            {"company_slug": slug, "year": YEAR, "sheet": "FS"},
            {"label": 1, "value": 1, "status": 1, "source": 1},
        )
    )
    db_vals: dict[str, float | None] = {}
    for d in docs:
        label = str(d.get("label") or "").strip()
        if not label:
            continue
        nl = norm_label(label)
        if nl in TOPIC_NORMS or "check" in nl:
            continue
        raw = d.get("value")
        if raw is None or str(raw).strip() in {"", "-", "—"}:
            db_vals[label] = None
        else:
            try:
                db_vals[label] = float(raw)
            except Exception:
                db_vals[label] = None

    excel = load_excel_year(YEAR)
    excel_verify = verify_year(YEAR)

    pdf_path = resolve_annual_pdf(db, slug, YEAR)
    pdf_compared = pdf_matches = pdf_missing = 0
    pdf_mismatches = []
    if pdf_path:
        labels = [lbl for lbl, v in db_vals.items() if v is not None]
        manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
        pdf_index = build_annual_fs_pdf_index(Path(pdf_path), manifest, YEAR)
        for label, dval in db_vals.items():
            if dval is None:
                continue
            pdf_val = _pdf_lookup_with_fallback(
                Path(pdf_path), pdf_index, label, YEAR, None
            )
            if pdf_val is None:
                pdf_missing += 1
                continue
            pdf_compared += 1
            if annual_values_match(dval, pdf_val):
                pdf_matches += 1
            else:
                pdf_mismatches.append(
                    {
                        "label": label,
                        "db": dval,
                        "pdf": float(pdf_val),
                        "diff": float(dval) - float(pdf_val),
                    }
                )

    excel_by_key = {label_match_key(k): (k, v) for k, v in excel.items()}
    missing_vs_excel = []
    for elabel, eval_ in excel.items():
        if eval_ is None:
            continue
        found = None
        for dl, dv in db_vals.items():
            if label_match_key(dl) == label_match_key(elabel):
                found = dv
                break
        if found is None:
            missing_vs_excel.append({"label": elabel, "excel": eval_, "db": None})

    empty_db = [
        lbl for lbl, v in db_vals.items() if v is None and norm_label(lbl) not in TOPIC_NORMS
    ]

    # Highlight key IS lines for the report comparison
    key_labels = [
        "Gross income",
        "Interest income",
        "Less: Interest expense",
        "Net interest income",
        "Profit for the year",
        "Total assets",
        "Total equity attributable to equity holders of the Bank",
    ]
    key_rows = []
    for kl in key_labels:
        dval = next(
            (v for l, v in db_vals.items() if label_match_key(l) == label_match_key(kl)),
            None,
        )
        eval_ = excel_by_key.get(label_match_key(kl), (kl, None))[1]
        pdf_val = None
        if pdf_path and dval is not None:
            pdf_val = _pdf_lookup_with_fallback(
                Path(pdf_path), pdf_index, kl, YEAR, None
            )
        key_rows.append(
            {
                "label": kl,
                "db": dval,
                "excel": eval_,
                "pdf": float(pdf_val) if pdf_val is not None else None,
                "db_vs_excel_ok": values_match(dval, eval_),
                "db_vs_pdf_ok": (
                    None
                    if pdf_val is None or dval is None
                    else annual_values_match(dval, pdf_val)
                ),
            }
        )

    report = {
        "year": YEAR,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "db_rows": len(db_vals),
        "excel_verify": {
            "compared": excel_verify["compared"],
            "matches": excel_verify["matches"],
            "mismatches": excel_verify["mismatches"],
            "match_pct": excel_verify["match_pct"],
        },
        "pdf_compare": {
            "compared": pdf_compared,
            "matches": pdf_matches,
            "mismatches": len(pdf_mismatches),
            "pdf_lookup_miss": pdf_missing,
            "match_pct": round(100.0 * pdf_matches / pdf_compared, 1)
            if pdf_compared
            else 0,
            "mismatch_rows": pdf_mismatches[:30],
        },
        "db_empty_cells": len(empty_db),
        "missing_vs_excel_filled": missing_vs_excel[:20],
        "key_income_statement_rows": key_rows,
    }
    out = Path("_compare_2021_db_excel_pdf.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
