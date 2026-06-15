"""
non_financial_data_run.py
=========================
End-to-end driver for the **"Test here" → Non-financial data** button.

Given a list of report PDFs the user ticked in the UI (each living under
``backend/Demo_Data/<COMPANY>/<reportType>/<file>.pdf``), this script, per
company:

  1. Locates the selected PDF inside Demo_Data.
  2. Extracts the STRUCTURED non-financial data points via OpenAI
     (``non_financial_data_script.extract_non_financial_data``).
  3. Immediately UPLOADS the result into MongoDB, company-wise, via
     ``non_financial_db_uploader``.

Progress is streamed to stdout as NDJSON so the Next.js API route
(``/api/demo/run-non-financial``) can relay it live to the browser — same
protocol ``Demo_run.py`` uses.

Input (``--items-stdin``)::

    {"items": [ {"company": "...", "report_type": "Annual", "file_name": "a.pdf",
                 "rel_path": "...", "group": "..."} ]}
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import Data_retrive as data_retrive
import non_financial_script as nfs
import non_financial_data_script as nf_data
import non_financial_db_uploader as nf_db


SCRIPT_DIR   = Path(__file__).resolve().parent
BACKEND_DIR  = SCRIPT_DIR.parent
DEMO_DEFAULT = BACKEND_DIR / "Demo_Data"


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_log(text: str, level: str = "info") -> None:
    for raw in str(text).splitlines():
        line = raw.rstrip()
        if line.strip():
            emit({"type": "log", "level": level, "message": line})


# ─────────────────────────────────────────────────────────────────────────────
# Input handling
# ─────────────────────────────────────────────────────────────────────────────

def _parse_items_payload(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("items") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        company = str(entry.get("company") or "").strip()
        report_type = str(entry.get("report_type") or "").strip()
        file_name = str(entry.get("file_name") or "").strip()
        rel_path = str(entry.get("rel_path") or "").strip()
        group = str(entry.get("group") or "").strip()
        if not (company and report_type and file_name):
            continue
        out.append({
            "company": company, "report_type": report_type,
            "file_name": file_name, "rel_path": rel_path, "group": group,
        })
    return out


def _group_by_company(items: list[dict[str, str]]):
    grouped: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for it in items:
        if it["company"] not in grouped:
            grouped[it["company"]] = []
            order.append(it["company"])
        grouped[it["company"]].append(it)
    return grouped, order


def _resolve_src(source_root: Path, entry: dict[str, str]) -> Path:
    if entry.get("rel_path"):
        return (source_root / Path(entry["rel_path"])).resolve()
    if entry.get("group"):
        return (source_root / entry["company"] / entry["report_type"]
                / entry["group"] / entry["file_name"])
    return (source_root / entry["company"] / entry["report_type"]
            / entry["file_name"])


def _report_label(entry: dict[str, str]) -> str:
    """Human label for one report (its group folder, else file name)."""
    return (entry.get("group") or entry.get("file_name") or "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items", type=Path, default=None)
    ap.add_argument("--items-stdin", action="store_true")
    ap.add_argument("--source", type=Path, default=DEMO_DEFAULT)
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-chars", type=int, default=200_000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--mongo-uri", default=None)
    ap.add_argument("--db-name", default=None)
    args = ap.parse_args(argv)

    if args.items_stdin:
        try:
            items = _parse_items_payload(json.loads(sys.stdin.read()))
        except Exception as ex:
            emit({"type": "error", "message": f"Failed to parse items from stdin: {ex!r}"})
            return 2
    elif args.items:
        if not args.items.exists():
            emit({"type": "error", "message": f"Items file not found: {args.items}"})
            return 2
        items = _parse_items_payload(json.loads(args.items.read_text(encoding="utf-8")))
    else:
        emit({"type": "error", "message": "Provide --items <file> or --items-stdin."})
        return 2

    if not items:
        emit({"type": "error", "message": "No selected items provided."})
        return 2

    source_root: Path = args.source.resolve()
    grouped, order = _group_by_company(items)
    total_companies = len(grouped)
    total_reports = sum(len(v) for v in grouped.values())
    model = nfs.resolve_model(args.model)
    do_upload = not args.no_upload and not args.dry_run

    emit({
        "type": "start", "totalFiles": total_reports,
        "totalReports": total_reports,
        "totalCompanies": total_companies, "sourceDir": str(source_root),
        "dryRun": bool(args.dry_run), "upload": do_upload, "model": model,
    })

    api_key = nfs.resolve_api_key(args.apikey)
    if not args.dry_run and not api_key:
        emit({"type": "error", "message": (
            "OpenAI API key not found. Pass --apikey, set OPENAI_API_KEY, "
            "or add it to backend/.env.")})
        return 1

    # Shared MongoDB connection reused across all companies.
    uploader = None
    if do_upload:
        try:
            r_uri, r_db = nf_db.resolve_mongo_config(args.mongo_uri, args.db_name)
            uploader = nf_db.NonFinancialUploader(r_uri, r_db)
            emit({"type": "log", "level": "info",
                  "message": f"Connected to MongoDB ({r_db})."})
        except Exception as ex:
            emit({"type": "error", "message": (
                f"MongoDB connection failed ({ex}). Nothing was processed.")})
            return 1

    ok = failed = 0
    company_idx = 0
    report_idx = 0
    try:
        for company in order:
            company_idx += 1
            company_key = data_retrive._sanitize_company_key(company)
            entries = grouped[company]

            emit({"type": "company-start", "index": company_idx,
                  "total": total_companies, "company": company,
                  "companyKey": company_key, "reportCount": len(entries)})

            company_ok = company_failed = 0

            # Process EVERY selected report so each annual year becomes its own
            # separate non-financial record (report-wise), keyed by its report
            # group / year — never collapsed into a single per-company doc.
            for entry in entries:
                report_idx += 1
                src = _resolve_src(source_root, entry)
                group = _report_label(entry)

                emit({"type": "stage-start", "company": company,
                      "companyKey": company_key, "kind": "Non-financial",
                      "index": report_idx, "total": total_reports,
                      "group": group, "fileName": src.name})

                if not src or not src.exists():
                    company_failed += 1
                    emit({"type": "stage-done", "company": company,
                          "companyKey": company_key, "kind": "Non-financial",
                          "group": group, "status": "missing",
                          "error": f"source not found: {src}"})
                    continue

                try:
                    payload = nf_data.extract_non_financial_data(
                        company=company, pdf_path=src, api_key=api_key or "",
                        model=model, max_chars=args.max_chars,
                        dry_run=bool(args.dry_run),
                    )
                    found = payload.get("found_count", 0)
                    total = payload.get("total_count", 0)
                    emit({"type": "stage-done", "company": company,
                          "companyKey": company_key, "kind": "Non-financial",
                          "group": group, "status": "ok",
                          "found": found, "total": total})
                except Exception as ex:
                    traceback.print_exc(file=sys.stderr)
                    company_failed += 1
                    emit({"type": "stage-done", "company": company,
                          "companyKey": company_key, "kind": "Non-financial",
                          "group": group, "status": "error", "error": str(ex)})
                    continue

                report_status = "ok"
                report_err = None

                if do_upload:
                    try:
                        summary = nf_db.upload_non_financial_data(
                            payload, company_slug=company_key,
                            company_name=company, report_group=group,
                            source_pdf=str(src), uploader=uploader,
                        )
                        emit({"type": "db-upload", "company": company,
                              "companyKey": company_key, "group": group,
                              **summary})
                        if summary.get("status") != "ok":
                            report_status = "partial"
                            report_err = summary.get("error")
                    except Exception as ex:
                        report_status = "partial"
                        report_err = str(ex)
                        emit({"type": "db-upload", "company": company,
                              "companyKey": company_key, "group": group,
                              "status": "error", "error": str(ex)})

                emit({"type": "result", "company": company,
                      "group": group, "data": payload})

                if report_status == "ok":
                    company_ok += 1
                    ok += 1
                else:
                    company_failed += 1
                    failed += 1

            emit({"type": "company-done", "company": company,
                  "status": "ok" if company_failed == 0 else "partial",
                  "ok": company_ok, "failed": company_failed})
    finally:
        if uploader is not None:
            uploader.close()

    emit({"type": "done", "totalFiles": total_reports,
          "totalReports": total_reports, "totalCompanies": total_companies,
          "ok": ok, "failed": failed, "uploaded": do_upload})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
