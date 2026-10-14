"""
annual_financial_extractor.py
=============================
Fast annual financial statement extraction for Run Annual.

Extracts only the main printed FS tables (no OpenAI, no note tables):
  • Income statement / Statement of profit or loss
  • Statement of comprehensive income
  • Statement of financial position
  • Statement of cash flows

Target: about one minute per annual report on a local PDF.
"""
from __future__ import annotations

import argparse
import json
import re
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
) -> dict[str, Any]:
    from annual_db_extractor import _store_printed
    from comb_workbook_store import ensure_indexes, get_db
    from printed_statements import default_output_path, extract_printed_report

    t0 = time.perf_counter()
    emit_log(
        f"Fast annual extract {company} {year} "
        f"(income, OCI, SoFP, cash flows — no notes, no OpenAI)…"
    )
    payload = extract_printed_report(
        pdf_path,
        company_slug=company_slug,
        company_name=company,
        year=year,
        include_notes=False,
    )
    ok_stmts = [s for s in (payload.get("statements") or []) if s.get("ok")]
    for statement in payload.get("statements") or []:
        emit_log(
            f"  {statement.get('title')}: "
            f"{len(statement.get('rows') or [])} rows "
            f"pages {statement.get('pages') or []} "
            f"ok={statement.get('ok')}"
        )

    out = default_output_path(company_slug, year)
    if not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    filled = missing = 0
    if not dry_run and not no_upload:
        db, client = get_db()
        try:
            ensure_indexes(db)
            filled, missing = _store_printed(db, payload, include_notes=False)
        finally:
            client.close()

    elapsed = time.perf_counter() - t0
    emit_log(
        f"Done {company} {year} in {elapsed:.1f}s "
        f"({len(ok_stmts)} statements, {filled} data rows)."
    )
    return {
        "ok": len(ok_stmts) > 0,
        "year": year,
        "tables": len(ok_stmts),
        "cells_filled": filled,
        "cells_missing": missing,
        "elapsed_s": round(elapsed, 2),
        "source_pdf": str(pdf_path),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items-stdin", action="store_true", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--model", default="gpt-5", help="Ignored (local PDF extract).")
    ap.add_argument("--option", choices=["1", "2"], default="1", help="Ignored.")
    ap.add_argument("--apikey", default=None, help="Ignored (no OpenAI).")
    ap.add_argument("--mongo-uri", default=None)
    ap.add_argument("--db-name", default=None)
    ap.add_argument(
        "--source",
        type=Path,
        default=DEMO_DATA_DIR,
        help=f"Demo data root (default: {DEMO_DATA_DIR}).",
    )
    args = ap.parse_args(argv)

    try:
        raw = json.loads(sys.stdin.read())
        items = filter_items_by_kind(parse_items_payload(raw), "annual")
    except Exception as exc:
        emit({"type": "error", "message": f"Failed to parse items: {exc!r}"})
        return 2

    if not items:
        emit({"type": "error", "message": "No annual reports selected."})
        return 2

    source_root = args.source.resolve()
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
            "model": "printed-statements",
            "mode": "annual-fast",
        }
    )
    emit_log(
        "Run Annual uses local printed-statement extract "
        "(main FS only — no OpenAI vision, no notes)."
    )

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
            # Group / folder first, filename last (avoid timestamp IDs → fake years).
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
                result = _extract_one(
                    pdf_path=src,
                    company=company,
                    company_slug=company_key,
                    year=year,
                    dry_run=bool(args.dry_run),
                    no_upload=bool(args.no_upload),
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
                        "error": None if result.get("ok") else "no main FS tables found",
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
    emit({"type": "exit", "code": 0 if failed == 0 else 1})
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
