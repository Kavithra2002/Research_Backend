"""Cross-check DB 2022 FS values vs COMB model - updated.xlsx."""
from __future__ import annotations

import json
import re
from pathlib import Path

import openpyxl

from comb_workbook_store import COMB_COLLECTION, get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG, norm_label, parse_number

XLSX = Path(r"E:\AMBEON\script\backend\New_Updates\COMB model - updated.xlsx")
OUT = Path(r"E:\AMBEON\script\backend\scripts\_crosscheck_2022.json")
YEAR = 2022


def _cell_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def load_excel_fs_2022(path: Path) -> dict[str, float | None]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["FS"] if "FS" in wb.sheetnames else wb.active

    # Find year header row/col
    year_col = None
    header_row = None
    for r in range(1, 15):
        for c in range(1, 20):
            v = ws.cell(r, c).value
            if v == YEAR or str(v).strip() == str(YEAR) or str(v).strip().lower() == f"{YEAR}f":
                # Prefer exact 2022 over 2022f
                if v == YEAR or str(v).strip() == str(YEAR):
                    year_col = c
                    header_row = r
                    break
        if year_col:
            break
    if not year_col:
        # fallback: scan for 2022 anywhere in first 10 rows
        for r in range(1, 12):
            for c in range(1, 20):
                v = ws.cell(r, c).value
                if v == YEAR:
                    year_col = c
                    header_row = r
                    break
            if year_col:
                break
    if not year_col:
        raise RuntimeError("Could not find 2022 column in Excel FS sheet")

    # Label column: usually A or B
    label_col = 1
    sample = _cell_str(ws.cell(header_row + 2, 2).value)
    if sample and len(sample) > 3:
        label_col = 2

    out: dict[str, float | None] = {}
    for r in range(header_row + 1, ws.max_row + 1):
        label = _cell_str(ws.cell(r, label_col).value)
        if not label:
            # try other col
            label = _cell_str(ws.cell(r, 1).value) or _cell_str(ws.cell(r, 2).value)
        if not label:
            continue
        # skip section headers / checks
        upper = label.upper()
        if upper in {
            "INCOME STATEMENT",
            "OCI",
            "BALANCE SHEET",
            "ASSETS",
            "LIABILITIES",
            "EQUITY",
            "MEMORANDUM INFORMATION",
            "CASH FLOW STATEMENT",
            "ADJUSTMENTS FOR",
        } or upper.endswith(" CHECK") or upper in {"IS CHECK", "BS CHECK"}:
            continue
        raw = ws.cell(r, year_col).value
        if raw is None or _cell_str(raw) in {"", "-", "—"}:
            out[label] = None
        else:
            try:
                if isinstance(raw, (int, float)):
                    out[label] = float(raw)
                else:
                    out[label] = parse_number(raw)
            except Exception:
                out[label] = None
    wb.close()
    return out, year_col, header_row, label_col


def values_match(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    diff = abs(float(a) - float(b))
    scale = max(abs(float(a)), abs(float(b)), 1.0)
    if scale < 10:
        return diff <= 0.05
    if scale < 10_000:
        return diff <= 0.5
    return diff <= max(1.0, scale * 1e-6)


def main() -> None:
    excel, year_col, header_row, label_col = load_excel_fs_2022(XLSX)
    db, client = get_db()
    db_map: dict[str, float | None] = {}
    status_map: dict[str, str] = {}
    for doc in db[COMB_COLLECTION].find(
        {
            "company_slug": COMMERCIAL_BANK_SLUG,
            "year": YEAR,
            "sheet": "FS",
        },
        {"label": 1, "value": 1, "status": 1},
    ):
        db_map[doc["label"]] = doc.get("value")
        status_map[doc["label"]] = doc.get("status") or ""
    client.close()

    # Match by normalized label
    excel_by_norm = {norm_label(k): (k, v) for k, v in excel.items()}
    db_by_norm = {norm_label(k): (k, v) for k, v in db_map.items()}

    rows = []
    matched_norms = set()
    for nl, (elabel, eval_) in sorted(excel_by_norm.items(), key=lambda x: x[1][0].lower()):
        if nl in db_by_norm:
            dlabel, dval = db_by_norm[nl]
            matched_norms.add(nl)
            ok = values_match(eval_, dval)
            rows.append(
                {
                    "label": dlabel,
                    "excel_label": elabel,
                    "excel": eval_,
                    "db": dval,
                    "status": status_map.get(dlabel, ""),
                    "match": ok,
                    "diff": None
                    if eval_ is None or dval is None
                    else float(dval) - float(eval_),
                }
            )
        else:
            rows.append(
                {
                    "label": elabel,
                    "excel_label": elabel,
                    "excel": eval_,
                    "db": None,
                    "status": "missing_in_db",
                    "match": eval_ is None,
                    "diff": None,
                }
            )

    for nl, (dlabel, dval) in db_by_norm.items():
        if nl in matched_norms:
            continue
        rows.append(
            {
                "label": dlabel,
                "excel_label": None,
                "excel": None,
                "db": dval,
                "status": status_map.get(dlabel, ""),
                "match": dval is None,
                "diff": None,
                "only_in_db": True,
            }
        )

    mismatches = [r for r in rows if not r["match"] and not r.get("only_in_db")]
    both_filled_mismatch = [
        r
        for r in mismatches
        if r["excel"] is not None and r["db"] is not None
    ]
    excel_has_db_missing = [
        r for r in mismatches if r["excel"] is not None and r["db"] is None
    ]
    db_has_excel_empty = [
        r for r in mismatches if r["excel"] is None and r["db"] is not None
    ]

    report = {
        "excel_path": str(XLSX),
        "year": YEAR,
        "excel_meta": {
            "year_col": year_col,
            "header_row": header_row,
            "label_col": label_col,
            "excel_labels": len(excel),
            "db_labels": len(db_map),
        },
        "summary": {
            "compared": len([r for r in rows if not r.get("only_in_db")]),
            "matches": len([r for r in rows if r["match"] and not r.get("only_in_db")]),
            "mismatches": len(mismatches),
            "both_filled_mismatch": len(both_filled_mismatch),
            "excel_has_db_missing": len(excel_has_db_missing),
            "db_has_excel_empty": len(db_has_excel_empty),
            "only_in_db": len([r for r in rows if r.get("only_in_db")]),
        },
        "both_filled_mismatch": both_filled_mismatch,
        "excel_has_db_missing": excel_has_db_missing,
        "db_has_excel_empty": db_has_excel_empty,
        "all_rows": rows,
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"\nWrote {OUT}")
    print(f"\nBoth-filled mismatches ({len(both_filled_mismatch)}):")
    for r in both_filled_mismatch[:40]:
        print(
            f"  {r['label']!r}: excel={r['excel']} db={r['db']} diff={r['diff']}"
        )


if __name__ == "__main__":
    main()
