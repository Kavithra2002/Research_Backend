"""
build_reports_index.py
======================
Write (or refresh) ``reports/<company>/index.json`` so the Reports UI can show
friendly names like ``Annual report 2025`` / ``Quarter 01 - 2016`` instead of
CDN basenames.

Uses the CSE financials catalog to map each on-disk PDF basename to year/quarter.
Filenames that are not in the CSE response keep any existing index entry, or a
best-effort parse from the filename itself.

Usage
-----
    python build_reports_index.py
    python build_reports_index.py --only ACL_PLASTICS_PLC
    python build_reports_index.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from Demo_data_download_script import quarter_label
from download_company_reports import (
    MANUAL_NAME_OVERRIDES,
    REPORTS_DIR,
    _entry_basename,
    _friendly_report_label,
    _load_report_index,
    _save_report_index,
    folder_to_query,
)
from get_report import (
    fetch_financials,
    fetch_trade_summary,
    report_year,
    resolve_symbol,
)

_DOTTED_PERIOD = re.compile(r"(?:^|[^\d])(\d{2})\.((?:19|20)\d{2})(?:[^\d]|$)")
_Q_TOKEN = re.compile(r"\bQ\s*([1-4])\b", re.I)
_YEAR_TOKEN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _quarter_from_month(month: int) -> int:
    if month <= 3:
        return 1
    if month <= 6:
        return 2
    if month <= 9:
        return 3
    return 4


def _parse_filename_meta(name: str, kind: str) -> dict[str, Any] | None:
    base = name[:-4] if name.lower().endswith(".pdf") else name
    dotted = _DOTTED_PERIOD.search(base)
    if dotted:
        month = int(dotted.group(1))
        year = int(dotted.group(2))
        if 1 <= month <= 12 and 1990 <= year <= 2100:
            q = _quarter_from_month(month)
            return {
                "kind": kind,
                "year": year,
                "quarter": q if kind == "quarterly" else None,
                "label": _friendly_report_label(
                    kind, year, q if kind == "quarterly" else None
                ),
                "file_text": "",
                "source": "filename",
            }
    q_m = _Q_TOKEN.search(base)
    y_m = _YEAR_TOKEN.search(base)
    if q_m and y_m and kind == "quarterly":
        year = int(y_m.group(1))
        q = int(q_m.group(1))
        return {
            "kind": kind,
            "year": year,
            "quarter": q,
            "label": _friendly_report_label(kind, year, q),
            "file_text": "",
            "source": "filename",
        }
    if y_m and kind == "annual":
        year = int(y_m.group(1))
        return {
            "kind": kind,
            "year": year,
            "quarter": None,
            "label": _friendly_report_label(kind, year),
            "file_text": "",
            "source": "filename",
        }
    return None


def _catalog_maps(fin: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    annual = fin.get("infoAnnualData") or []
    if isinstance(annual, list):
        for entry in annual:
            if not isinstance(entry, dict):
                continue
            fname = _entry_basename(entry)
            year = report_year(entry)
            out[fname] = {
                "kind": "annual",
                "year": year,
                "quarter": None,
                "label": _friendly_report_label("annual", year),
                "file_text": str(entry.get("fileText") or ""),
                "source": "cse",
            }
    quarterly = fin.get("infoQuarterlyData") or []
    if isinstance(quarterly, list):
        for entry in quarterly:
            if not isinstance(entry, dict):
                continue
            fname = _entry_basename(entry)
            ql = quarter_label(entry)
            year = ql[0] if ql else report_year(entry)
            quarter = ql[1] if ql else None
            if quarter == 0:
                quarter = None
            out[fname] = {
                "kind": "quarterly",
                "year": year,
                "quarter": quarter,
                "label": _friendly_report_label("quarterly", year, quarter),
                "file_text": str(entry.get("fileText") or ""),
                "source": "cse",
            }
    return out


def build_index_for_folder(
    folder: Path,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool,
    min_score: float = 0.78,
) -> dict[str, Any]:
    print(f"\n=== {folder.name} ===")
    override = MANUAL_NAME_OVERRIDES.get(folder.name) or MANUAL_NAME_OVERRIDES.get(
        folder.name.upper()
    )
    query = override or folder_to_query(folder.name)
    sym, official, score = resolve_symbol(query, rows)
    if not sym or score < min_score:
        print(
            f"  [skip] unresolved (best={official!r}, score={score:.2f})",
            file=sys.stderr,
        )
        return {"folder": folder.name, "status": "unresolved", "labelled": 0}

    print(f"  match : {official} ({sym}, score={score:.2f})")
    fin = fetch_financials(sym)
    if not isinstance(fin, dict):
        fin = {}
    catalog = _catalog_maps(fin)

    index = _load_report_index(folder)
    labelled = 0
    for kind_dir, kind in (("Annual", "annual"), ("Quarterly", "quarterly")):
        path = folder / kind_dir
        if not path.is_dir():
            continue
        for pdf in sorted(path.glob("*.pdf")):
            meta = catalog.get(pdf.name) or _parse_filename_meta(pdf.name, kind)
            if not meta:
                continue
            index["files"][pdf.name] = meta
            labelled += 1
            print(f"  {kind_dir:9} {meta['label']:28} <- {pdf.name}")

    if not dry_run:
        _save_report_index(folder, index)
        print(f"  wrote : index.json ({labelled} file(s))")
    else:
        print(f"  would : index.json ({labelled} file(s))")

    return {
        "folder": folder.name,
        "status": "ok",
        "labelled": labelled,
        "symbol": sym,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    ap.add_argument("--only", type=str, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    rows = fetch_trade_summary()
    folders = sorted(
        (p for p in args.reports_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name.casefold(),
    )
    if args.only:
        want = args.only.strip().casefold()
        folders = [p for p in folders if p.name.casefold() == want]
        if not folders:
            print(f"No folder matching --only {args.only!r}", file=sys.stderr)
            return 1

    ok = 0
    for folder in folders:
        result = build_index_for_folder(folder, rows, dry_run=args.dry_run)
        if result.get("status") == "ok":
            ok += 1
    print(f"\nDone. Indexed {ok}/{len(folders)} company folder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
