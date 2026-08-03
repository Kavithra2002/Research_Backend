"""
Selected_report_extractor.py
============================
Download user-selected annual and quarterly reports from the CSE into the
Demo_Data folder layout used by the Development page.

Modes
-----
    --list-companies          Print JSON list of CSE-listed companies.
    --list-reports NAME       Print JSON year/report catalog for one company.
    --items-stdin             Download selections from stdin JSON.

Stdin payload::

    {
      "company": "Commercial Bank of Ceylon PLC",
      "reports": [
        {"report_type": "Annual", "year": 2022},
        {"report_type": "Quarterly", "year": 2022, "quarter": 1}
      ]
    }

Or batch mode::

    {"items": [ {"company": "...", "reports": [...]}, ... ]}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import urllib.error
from pathlib import Path
from typing import Any

from cse_catalog import (
    demo_folder_for_report,
    list_company_reports,
    list_cse_companies,
    match_report_selection,
    resolve_company,
)
from Demo_data_download_script import _download_entry
from get_report import fetch_financials, safe_dir_name
from runner_common import DEMO_DATA_DIR, configure_stdio, emit, emit_log

configure_stdio()


def _entry_from_catalog(company: str, selection: dict[str, Any]) -> dict[str, Any] | None:
    matched = match_report_selection(company, selection)
    if not matched:
        return None
    resolved = resolve_company(company)
    if not resolved.get("ok"):
        return None
    symbol = str(resolved["symbol"])
    fin = fetch_financials(symbol)
    annual = fin.get("infoAnnualData") or []
    quarterly = fin.get("infoQuarterlyData") or []
    pool: list[dict[str, Any]] = []
    if isinstance(annual, list):
        pool.extend(annual)
    if isinstance(quarterly, list):
        pool.extend(quarterly)
    entry_id = matched.get("entry_id")
    file_name = str(matched.get("file_name") or "")
    for entry in pool:
        if entry_id is not None and entry.get("id") == entry_id:
            return entry
        from Demo_data_download_script import _entry_basename

        if file_name and _entry_basename(entry) == file_name:
            return entry
    return None


def download_selections(
    company: str,
    reports: list[dict[str, Any]],
    *,
    root: Path,
    pause_s: float = 0.35,
    dry_run: bool = False,
) -> dict[str, Any]:
    resolved = resolve_company(company)
    if not resolved.get("ok"):
        return {"company": company, "status": "unresolved", "error": resolved.get("error")}

    official = str(resolved["company"])
    company_dir = root / safe_dir_name(company)
    saved = exists = failed = skipped = 0
    files: list[dict[str, Any]] = []

    for sel in reports:
        report_meta = match_report_selection(company, sel)
        if not report_meta:
            skipped += 1
            emit_log(f"  [skip] no CSE match for {sel}", level="warning")
            continue
        entry = _entry_from_catalog(company, sel)
        if not entry:
            skipped += 1
            emit_log(f"  [skip] CSE entry not found for {report_meta.get('group')}", level="warning")
            continue

        rel_folder, group = demo_folder_for_report(company, report_meta)
        dest_dir = root.joinpath(*rel_folder.split("/"))

        outcome = _download_entry(entry, dest_dir, pause_s, dry_run)
        file_name = str(report_meta.get("file_name") or "")
        rel_path = f"{rel_folder}/{file_name}".replace("\\", "/")
        files.append(
            {
                "company": company,
                "report_type": report_meta.get("report_type"),
                "file_name": file_name,
                "rel_path": rel_path,
                "group": group,
                "outcome": outcome,
            }
        )
        if outcome == "saved":
            saved += 1
        elif outcome == "exists":
            exists += 1
        elif outcome == "failed":
            failed += 1
        else:
            skipped += 1

        emit(
            {
                "type": "file-done",
                "company": company,
                "report_type": report_meta.get("report_type"),
                "group": group,
                "file_name": file_name,
                "rel_path": rel_path,
                "outcome": outcome,
            }
        )

    status = "ok" if failed == 0 else "partial"
    return {
        "company": company,
        "official": official,
        "status": status,
        "saved": saved,
        "exists": exists,
        "failed": failed,
        "skipped": skipped,
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-companies", action="store_true")
    ap.add_argument("--list-reports", default="")
    ap.add_argument("--items-stdin", action="store_true")
    ap.add_argument("--root", type=Path, default=DEMO_DATA_DIR)
    ap.add_argument("--pause", type=float, default=0.35)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.list_companies:
        print(json.dumps({"companies": list_cse_companies()}, ensure_ascii=False))
        return 0

    if args.list_reports.strip():
        payload = list_company_reports(args.list_reports.strip())
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload.get("ok", True) else 1

    if not args.items_stdin:
        emit({"type": "error", "message": "Provide --items-stdin, --list-companies, or --list-reports."})
        return 2

    try:
        raw = json.loads(sys.stdin.read())
    except Exception as exc:
        emit({"type": "error", "message": f"Invalid stdin JSON: {exc!r}"})
        return 2

    batches: list[dict[str, Any]] = []
    if isinstance(raw, dict) and raw.get("items"):
        batches = [b for b in raw["items"] if isinstance(b, dict)]
    elif isinstance(raw, dict) and raw.get("company"):
        batches = [raw]
    else:
        emit({"type": "error", "message": "Expected {company, reports} or {items: [...]}."})
        return 2

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    total_reports = sum(len(b.get("reports") or []) for b in batches)

    emit(
        {
            "type": "start",
            "totalCompanies": len(batches),
            "totalReports": total_reports,
            "root": str(root),
            "dryRun": bool(args.dry_run),
        }
    )

    ok = failed = 0
    for idx, batch in enumerate(batches, 1):
        company = str(batch.get("company") or "").strip()
        reports = batch.get("reports") or []
        if not company or not isinstance(reports, list):
            failed += 1
            continue
        emit(
            {
                "type": "company-start",
                "index": idx,
                "total": len(batches),
                "company": company,
                "reportCount": len(reports),
            }
        )
        try:
            result = download_selections(
                company,
                reports,
                root=root,
                pause_s=max(0.0, args.pause),
                dry_run=bool(args.dry_run),
            )
            if result.get("status") == "ok":
                ok += 1
            else:
                failed += 1
            emit({"type": "company-done", **result})
        except urllib.error.URLError as exc:
            failed += 1
            emit({"type": "company-done", "company": company, "status": "failed", "error": str(exc)})
            emit_log(traceback.format_exc(), level="error")
        except Exception as exc:
            failed += 1
            emit({"type": "company-done", "company": company, "status": "failed", "error": str(exc)})
            emit_log(traceback.format_exc(), level="error")
        if args.pause > 0:
            time.sleep(args.pause)

    emit({"type": "done", "ok": ok, "failed": failed, "totalCompanies": len(batches)})
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
