"""
annual_financial_extractor.py
=============================
Annual financial statement extraction for Run Annual.

When OPENAI_API_KEY is set (env / backend/.env / --apikey):
  1. Find statement pages in the PDF
  2. Capture those pages as images
  3. Send captures to OpenAI vision for verbatim table extract

Falls back to local printed_statements extract when no API key is available.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from runner_common import (
    DEMO_DATA_DIR,
    configure_stdio,
    emit,
    emit_log,
    filter_items_by_kind,
    parse_items_payload,
    resolve_report_year,
)

configure_stdio()


def _sanitize_company_key(name: str) -> str:
    import re

    s = name.strip()
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", "_", s)
    return s.strip("._") or "company"


def _resolve_src(source_root: Path, entry: dict[str, str]) -> Path:
    if entry.get("rel_path"):
        return (source_root / Path(entry["rel_path"])).resolve()
    if entry.get("group"):
        return (
            source_root
            / entry["company"]
            / entry["report_type"]
            / entry["group"]
            / entry["file_name"]
        )
    return (
        source_root / entry["company"] / entry["report_type"] / entry["file_name"]
    )


def _extract_one(
    *,
    pdf_path: Path,
    company: str,
    company_slug: str,
    year: int,
    dry_run: bool,
    no_upload: bool,
    api_key: str | None,
    model: str,
    option: str,
    dpi: int,
    work_dir: Path,
    include_notes: bool = True,
) -> dict[str, Any]:
    import Data_retrive as data_retrive
    import db_uploader

    t0 = time.perf_counter()
    resolved_key = data_retrive._resolve_api_key(api_key)
    mode_label = (
        f"OpenAI vision ({model})"
        if resolved_key
        else "local printed-statements (no API key)"
    )
    emit_log(
        f"Annual extract {company} {year} — {mode_label} "
        f"(income / P&L, OCI, SoFP, cash flows"
        f"{', notes' if include_notes else ''})…"
    )

    res = data_retrive.process_company(
        company=company_slug,
        pdf_path=pdf_path,
        testing_dir=work_dir,
        api_key=resolved_key,
        option=option,
        model=model,
        dry_run=dry_run,
        dpi=dpi,
        force=True,
    )
    status = res.get("status", "error") if isinstance(res, dict) else "error"
    ok = status in ("ok", "skipped_existing")

    tables = 0
    filled = missing = 0
    company_out = work_dir / company_slug
    results_path = company_out / f"{company_slug}_results.json"
    pages_json = company_out / f"{company_slug}_pages.json"

    if not dry_run and not no_upload and results_path.exists():
        try:
            doc = json.loads(results_path.read_text(encoding="utf-8"))
            summary = db_uploader.upload_results_data(
                doc,
                "annual",
                company_slug=company_slug,
                company_name=company,
                report_group=f"Annual Report {year}",
                source_pdf=str(pdf_path),
                quiet=True,
            )
            tables = int(summary.get("tables") or 0)
            filled = int(summary.get("cells_filled") or tables)
            missing = int(summary.get("cells_missing") or 0)
            keys = summary.get("statement_keys") or []
            emit_log(
                f"  DB upload: {tables} table(s) "
                f"(status={summary.get('ok')}; keys={keys})"
            )
            dropped = summary.get("dropped_ok_keys") or []
            if dropped:
                emit_log(
                    f"  DB upload dropped OK statements: {dropped}",
                    level="error",
                )
                ok = False
            errs = summary.get("errors") or []
            if errs:
                for e in errs:
                    emit_log(f"  DB upload issue: {e}", level="error")
                if not summary.get("ok", True):
                    ok = False

            # Same OpenAI payload → DB page (Group/Company + note refs).
            try:
                import db_page_sync as sync

                accuracy = {}
                acc_path = company_out / "accuracy_report.json"
                if acc_path.exists():
                    try:
                        accuracy = json.loads(
                            acc_path.read_text(encoding="utf-8")
                        )
                    except Exception:
                        accuracy = {}
                payload, printed_path = sync.sync_openai_annual_to_db_page(
                    doc,
                    company_slug=company_slug,
                    company_name=company,
                    year=year,
                    source_pdf=pdf_path,
                    pages_json=pages_json if pages_json.exists() else None,
                    include_notes=include_notes,
                    accuracy=accuracy if isinstance(accuracy, dict) else None,
                )
                note_n = len(payload.get("notes") or {})
                emit_log(
                    f"  DB page updated: {printed_path.name}"
                    f" (statements={len(payload.get('statements') or [])}"
                    f", notes={note_n})"
                )
            except Exception as exc:
                emit_log(f"  DB page sync skipped: {exc}", level="warn")
        except Exception as exc:
            emit_log(f"  DB upload failed: {exc}", level="error")
            ok = False
            status = "error"

    elapsed = time.perf_counter() - t0
    emit_log(
        f"Done {company} {year} in {elapsed:.1f}s "
        f"(status={status}, tables={tables})."
    )
    return {
        "ok": ok and (tables > 0 or dry_run or no_upload),
        "year": year,
        "tables": tables,
        "cells_filled": filled,
        "cells_missing": missing,
        "elapsed_s": round(elapsed, 2),
        "source_pdf": str(pdf_path),
        "status": status,
        "error": res.get("error") if isinstance(res, dict) else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items-stdin", action="store_true", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--model", default="gpt-5", help="OpenAI vision model.")
    ap.add_argument("--option", choices=["1", "2"], default="1")
    ap.add_argument(
        "--apikey",
        default=None,
        help="OpenAI API key (else OPENAI_API_KEY / backend/.env).",
    )
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--mongo-uri", default=None)
    ap.add_argument("--db-name", default=None)
    ap.add_argument(
        "--include-notes",
        action="store_true",
        default=True,
        help="Also extract note tables for DB page note links (default on).",
    )
    ap.add_argument(
        "--no-notes",
        action="store_true",
        help="Skip note-table extract (faster; note links empty on DB page).",
    )
    ap.add_argument(
        "--source",
        type=Path,
        default=DEMO_DATA_DIR,
        help=f"Demo data root (default: {DEMO_DATA_DIR}).",
    )
    args = ap.parse_args(argv)
    include_notes = bool(args.include_notes) and not bool(args.no_notes)

    try:
        raw = json.loads(sys.stdin.read())
        items = filter_items_by_kind(parse_items_payload(raw), "annual")
    except Exception as exc:
        emit({"type": "error", "message": f"Failed to parse items: {exc!r}"})
        return 2

    if not items:
        emit({"type": "error", "message": "No annual reports selected."})
        return 2

    import Data_retrive as data_retrive

    source_root = args.source.resolve()
    api_key = data_retrive._resolve_api_key(args.apikey)
    by_company: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for it in items:
        company = it["company"]
        if company not in by_company:
            by_company[company] = []
            order.append(company)
        by_company[company].append(it)

    total_files = sum(len(v) for v in by_company.values())
    emit(
        {
            "type": "start",
            "totalFiles": total_files,
            "totalCompanies": len(order),
            "sourceDir": str(source_root),
            "dryRun": bool(args.dry_run),
            "upload": not args.no_upload and not args.dry_run,
            "model": args.model if api_key else "printed-statements",
            "mode": "annual-openai" if api_key else "annual-local",
        }
    )
    if api_key:
        emit_log(
            f"Run Annual uses OpenAI vision on captured pages (model={args.model})."
        )
    else:
        emit_log(
            "OPENAI_API_KEY not set — Run Annual uses local printed extract.",
            level="warning",
        )

    import tempfile

    ok = failed = 0
    for company_idx, company in enumerate(order, 1):
        company_key = _sanitize_company_key(company)
        jobs = by_company[company]
        emit(
            {
                "type": "company-start",
                "index": company_idx,
                "total": len(order),
                "company": company,
                "companyKey": company_key,
                "annualCount": len(jobs),
                "quarterlyCount": 0,
            }
        )

        for entry in jobs:
            group = entry.get("group") or ""
            src = _resolve_src(source_root, entry)
            year = resolve_report_year(
                group,
                src.parent.name if src else None,
                src.name if src else None,
            )
            label = f"Annual {year}" if year else "Annual"

            emit(
                {
                    "type": "stage-start",
                    "company": company,
                    "companyKey": company_key,
                    "kind": label,
                    "group": group,
                }
            )

            if not src.exists():
                failed += 1
                emit(
                    {
                        "type": "stage-done",
                        "company": company,
                        "companyKey": company_key,
                        "kind": label,
                        "status": "missing",
                        "group": group,
                        "error": f"source not found: {src}",
                    }
                )
                continue
            if year is None:
                failed += 1
                emit(
                    {
                        "type": "stage-done",
                        "company": company,
                        "companyKey": company_key,
                        "kind": label,
                        "status": "error",
                        "group": group,
                        "error": "could not infer report year from path/group",
                    }
                )
                continue

            try:
                with tempfile.TemporaryDirectory(
                    prefix=f"ambeon-annual-{company_key}-",
                ) as work_dir_str:
                    result = _extract_one(
                        pdf_path=src,
                        company=company,
                        company_slug=company_key,
                        year=year,
                        dry_run=bool(args.dry_run),
                        no_upload=bool(args.no_upload),
                        api_key=api_key,
                        model=args.model,
                        option=args.option,
                        dpi=args.dpi,
                        work_dir=Path(work_dir_str),
                        include_notes=include_notes,
                    )
                if result.get("ok"):
                    ok += 1
                    status = "ok"
                else:
                    failed += 1
                    status = "error"
                emit(
                    {
                        "type": "stage-done",
                        "company": company,
                        "companyKey": company_key,
                        "kind": label,
                        "status": status,
                        "group": group,
                        "error": result.get("error"),
                        "tables": result.get("tables"),
                        "cells_filled": result.get("cells_filled"),
                        "cells_missing": result.get("cells_missing"),
                    }
                )
                if not args.dry_run and not args.no_upload:
                    emit(
                        {
                            "type": "db-upload",
                            "company": company,
                            "companyKey": company_key,
                            "reportType": "annual",
                            "group": group,
                            "status": "ok" if result.get("ok") else "error",
                            "tables": result.get("tables", 0),
                            "year": year,
                            "cells_filled": result.get("cells_filled"),
                        }
                    )
            except Exception as exc:
                failed += 1
                emit_log(traceback.format_exc(), level="error")
                emit(
                    {
                        "type": "stage-done",
                        "company": company,
                        "companyKey": company_key,
                        "kind": label,
                        "status": "error",
                        "group": group,
                        "error": str(exc),
                    }
                )

        emit(
            {
                "type": "company-done",
                "company": company,
                "companyKey": company_key,
                "status": "ok" if failed == 0 else "partial",
            }
        )

    emit({"type": "done", "ok": ok, "failed": failed})
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
