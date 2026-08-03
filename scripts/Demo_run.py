"""
Demo_run.py
===========
End-to-end driver for the **"Test here"** page.

Given a list of demo report PDFs the user ticked in the UI (each one living
under ``backend/Demo_Data/<COMPANY>/<reportType>/<file>.pdf``), this script,
per company:

  1. Copies the selected PDF(s) into ``testing/<company_key>/``.
  2. Runs the matching extraction pipeline:
        • Annual    -> Data_retrive.process_company           (step1/2/3 + OpenAI)
        • Quarterly -> Q_data_extraction.process_quarterly_for_company
  3. Immediately UPLOADS the resulting ``*_results.json`` into MongoDB,
     split table-by-table, via db_uploader.

Progress is streamed to stdout as NDJSON so the Next.js API route
(``/api/demo/run``) can relay it live to the browser — same protocol the
existing ``Extract_selected_reports.py`` uses.

Input
-----
A JSON file (``--items``) shaped like::

    {"items": [ {"company": "...", "report_type": "Annual",    "file_name": "a.pdf"},
                {"company": "...", "report_type": "Quarterly", "file_name": "q.pdf"} ]}

Usage
-----
    python Demo_run.py --items selections.json
    python Demo_run.py --items selections.json --dry-run
    python Demo_run.py --items selections.json --model gpt-4o-mini --no-upload
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import Data_retrive as data_retrive
import db_uploader

try:
    import Q_data_extraction as q_extraction
    _Q_AVAILABLE = True
except Exception as _q_err:  # pragma: no cover
    q_extraction = None  # type: ignore
    _Q_AVAILABLE = False
    _Q_IMPORT_ERROR = str(_q_err)


SCRIPT_DIR      = Path(__file__).resolve().parent
BACKEND_DIR     = SCRIPT_DIR.parent
DEMO_DEFAULT     = BACKEND_DIR / "Demo_Data"
DEMO_CAPTURES_OUT = BACKEND_DIR / "Demo_Data_captures"

_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _year_from_text(*sources: Any) -> int | None:
    """Most-recent 4-digit reporting year found in any of the given strings."""
    years: list[int] = []
    for s in sources:
        if not s:
            continue
        years += [int(m.group(0)) for m in _YEAR_RE.finditer(str(s))]
    years = [y for y in years if 1990 <= y <= 2100]
    return max(years) if years else None


def _persist_captures(
    *,
    company: str,
    company_key: str,
    report_type: str,
    group: str,
    src: Path,
    work_dir: Path,
) -> int:
    """Copy rendered statement page images out of the throwaway work dir into
    the persistent ``Demo_Data_captures`` tree so the Comparison page can show
    this report's capture pages.

    Layout written (matches the capture API):
        Demo_Data_captures/<company>/<Annual|Quarterly>/<year>/<stmt>/page_*.png
    """
    period = "Quarterly" if report_type == "quarterly" else "Annual"
    captures_name = (
        "captures_quarterly" if report_type == "quarterly" else "captures"
    )
    src_caps = work_dir / company_key / captures_name
    if not src_caps.is_dir():
        return 0

    year = _year_from_text(group, src.parent.name if src else None)
    if year is None:
        return 0

    dest_root = DEMO_CAPTURES_OUT / company / period / str(year)
    copied = 0
    for stmt_dir in src_caps.iterdir():
        if not stmt_dir.is_dir():
            continue
        pngs = sorted(stmt_dir.glob("*.png"))
        if not pngs:
            continue
        dest_dir = dest_root / stmt_dir.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        for png in pngs:
            try:
                shutil.copy2(png, dest_dir / png.name)
                copied += 1
            except Exception:
                pass
    return copied


# ─────────────────────────────────────────────────────────────────────────────
# NDJSON helpers
# ─────────────────────────────────────────────────────────────────────────────

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
        # rel_path is the path of the PDF relative to the Demo_Data root.
        # It removes any ambiguity from the variable folder nesting
        # (Company / Annual / "Annual Report 2024" / file.pdf).
        rel_path = str(entry.get("rel_path") or "").strip()
        group = str(entry.get("group") or "").strip()
        if not (company and report_type and file_name):
            continue
        out.append({
            "company": company,
            "report_type": report_type,
            "file_name": file_name,
            "rel_path": rel_path,
            "group": group,
        })
    return out


def _load_items(items_path: Path) -> list[dict[str, str]]:
    return _parse_items_payload(json.loads(items_path.read_text(encoding="utf-8")))


def _group_by_company(items: list[dict[str, str]]):
    grouped: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for it in items:
        if it["company"] not in grouped:
            grouped[it["company"]] = []
            order.append(it["company"])
        grouped[it["company"]].append(it)
    return grouped, order


def _is_quarterly_type(report_type: str) -> bool:
    t = (report_type or "").strip().lower()
    return any(tok in t for tok in (
        "quarter", "interim", "q1", "q2", "q3", "q4",
        "half year", "halfyear", "half-year",
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items", type=Path, default=None,
                    help="JSON file with the selected demo items.")
    ap.add_argument("--items-stdin", action="store_true",
                    help="Read items JSON from stdin instead of --items file.")
    ap.add_argument("--source", type=Path, default=DEMO_DEFAULT,
                    help=f"Demo data root (default: {DEMO_DEFAULT}).")
    ap.add_argument("--apikey", default=None,
                    help="OpenAI API key (else OPENAI_API_KEY env / .env).")
    ap.add_argument("--option", choices=["1", "2"], default="1",
                    help="1 = core statements  2 = core + Notes (default: 1).")
    ap.add_argument("--model", default="gpt-4o",
                    help="OpenAI model name (default: gpt-4o).")
    ap.add_argument("--dpi", type=int, default=150, help="Render DPI (default 150).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render images but DON'T call OpenAI (no DB upload).")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even if results already exist.")
    ap.add_argument("--no-upload", action="store_true",
                    help="Run extraction but skip the MongoDB upload step.")
    ap.add_argument("--mongo-uri", default=None, help="Override MongoDB URI.")
    ap.add_argument("--db-name", default=None, help="Override MongoDB database.")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="Max attempts per report before giving up "
                         "(default 3: 1 initial + 2 retries).")
    ap.add_argument("--comb-extract", action="store_true",
                    help="After financial_tables upload, populate comb_workbook_data "
                         "for the DB page (Commercial Bank pilot).")
    ap.add_argument("--retry-delay", type=float, default=3.0,
                    help="Base seconds to wait between retries (default 3).")
    args = ap.parse_args(argv)

    if args.items_stdin:
        try:
            items = _parse_items_payload(json.loads(sys.stdin.read()))
        except Exception as ex:
            emit({"type": "error", "message": f"Failed to parse items from stdin: {ex!r}"})
            return 2
    elif args.items:
        items_path: Path = args.items
        if not items_path.exists():
            emit({"type": "error", "message": f"Items file not found: {items_path}"})
            return 2
        try:
            items = _load_items(items_path)
        except Exception as ex:
            emit({"type": "error", "message": f"Failed to parse items file: {ex!r}"})
            return 2
    else:
        emit({"type": "error", "message": "Provide --items <file> or --items-stdin."})
        return 2
    if not items:
        emit({"type": "error", "message": "No selected items provided."})
        return 2

    source_root: Path = args.source.resolve()

    grouped, order = _group_by_company(items)
    total_files     = sum(len(v) for v in grouped.values())
    total_companies = len(grouped)

    do_upload = not args.no_upload and not args.dry_run

    emit({
        "type": "start",
        "totalFiles": total_files,
        "totalCompanies": total_companies,
        "sourceDir": str(source_root),
        "dryRun": bool(args.dry_run),
        "upload": do_upload,
        "model": args.model,
    })

    api_key = data_retrive._resolve_api_key(args.apikey)
    if not args.dry_run and not api_key:
        emit({"type": "error", "message": (
            "OpenAI API key not found. Pass --apikey, set OPENAI_API_KEY, "
            "or add it to backend/.env."
        )})
        return 1

    if not args.dry_run and not do_upload:
        emit({"type": "error", "message": (
            "Refusing to run: uploads are disabled (--no-upload) and this "
            "pipeline never persists results locally, so there would be "
            "nowhere to store the extracted tables."
        )})
        return 1

    # ── Shared MongoDB connection (reused across all uploads) ───────────────
    uploader = None
    if do_upload:
        try:
            r_uri, r_db = db_uploader.resolve_mongo_config(args.mongo_uri, args.db_name)
            uploader = db_uploader.MongoUploader(r_uri, r_db)
            emit({"type": "log", "level": "info",
                  "message": f"Connected to MongoDB ({r_db})."})
        except Exception as ex:
            emit({"type": "error", "message": (
                f"MongoDB connection failed ({ex}). Nothing was processed "
                "because results are only ever stored in the database."
            )})
            return 1

    if not _Q_AVAILABLE:
        emit({"type": "log", "level": "warning", "message": (
            f"Q_data_extraction not importable ({_Q_IMPORT_ERROR}); "
            "Quarterly selections will be skipped."
        )})

    # ── EXTRACT (to a temp dir) → UPLOAD → DELETE temp dir ──────────────────
    ok = failed = 0
    company_idx = 0

    try:
        for company in order:
            company_idx += 1
            company_key = data_retrive._sanitize_company_key(company)

            jobs = []  # (label, report_type, src_path, group)
            for entry in grouped[company]:
                src = _resolve_src(source_root, entry)
                is_q = _is_quarterly_type(entry["report_type"])
                jobs.append((
                    "Quarterly" if is_q else "Annual",
                    "quarterly" if is_q else "annual",
                    src,
                    entry.get("group") or "",
                ))

            emit({
                "type": "company-start", "index": company_idx,
                "total": total_companies, "company": company,
                "companyKey": company_key,
                "annualCount": sum(1 for j in jobs if j[1] == "annual"),
                "quarterlyCount": sum(1 for j in jobs if j[1] == "quarterly"),
            })

            stages: list[tuple[str, str, str | None]] = []

            for label, rtype, src, group in jobs:
                if not src or not src.exists():
                    stages.append((label, "missing", f"source not found: {src}"))
                    emit({"type": "stage-done", "company": company,
                          "companyKey": company_key, "kind": label,
                          "status": "missing", "group": group,
                          "error": f"source not found: {src}"})
                    continue

                status, err = _process_one(
                    company=company, company_key=company_key, label=label,
                    report_type=rtype, src=src, group=group, args=args,
                    api_key=api_key, uploader=uploader, do_upload=do_upload,
                )
                stages.append((label, status, err))

            if stages and all(s == "ok" for _, s, _ in stages):
                company_status, company_err = "ok", None
                ok += 1
            else:
                company_status = next(
                    (s for _, s, _ in stages if s != "ok"), "ok")
                company_err = next(
                    (e for _, s, e in stages if s != "ok" and e), None)
                failed += 1

            emit({"type": "company-done", "index": company_idx,
                  "total": total_companies, "company": company,
                  "companyKey": company_key, "status": company_status,
                  "error": company_err,
                  "stages": [{"kind": k, "status": s, "error": e}
                             for k, s, e in stages]})

            if (
                do_upload
                and getattr(args, "comb_extract", False)
                and company_key == "Commercial_Bank_of_Ceylon_PLC"
                and any(s == "ok" for _, s, _ in stages)
            ):
                try:
                    from extract_comb_data import extract_comb_pilot_2022

                    emit({"type": "comb-extract-start", "companyKey": company_key})
                    db_ref = uploader.db if uploader is not None else None
                    if db_ref is not None:
                        comb_result = extract_comb_pilot_2022(db_ref, company_key)
                        emit({"type": "comb-extract-done", **comb_result})
                        emit_log(
                            f"COMB DB-page data: {comb_result.get('cells_filled', 0)} "
                            f"cells filled, {comb_result.get('cells_missing', 0)} missing.",
                            level="info",
                        )
                except Exception as ex:
                    emit({"type": "comb-extract-done", "ok": False, "error": str(ex)})
                    emit_log(f"COMB extract failed: {ex}", level="warning")

            if company_status != "ok" and not getattr(args, "comb_extract", False):
                emit_log(
                    f"Stopping batch — {company} did not pass validation. "
                    "Fix gaps and re-run before processing the next company.",
                    level="error",
                )
                break
    finally:
        if uploader is not None:
            uploader.close()

    emit({"type": "done", "totalFiles": total_files,
          "totalCompanies": total_companies, "ok": ok, "failed": failed,
          "uploaded": do_upload})
    return 0


def _resolve_src(source_root: Path, entry: dict[str, str]) -> Path:
    """Locate the source PDF inside Demo_Data for one selection item."""
    if entry.get("rel_path"):
        return (source_root / Path(entry["rel_path"])).resolve()
    if entry.get("group"):
        return (source_root / entry["company"] / entry["report_type"]
                / entry["group"] / entry["file_name"])
    return (source_root / entry["company"] / entry["report_type"]
            / entry["file_name"])


# Extraction statuses that are worth retrying (transient / detection hiccups).
_RETRYABLE_STATUSES = {
    "crashed", "step1_failed", "step2_failed", "step3_failed",
    "no_statements_found", "missing_statements",
}


def _attempt_one(
    *, company: str, company_key: str, label: str, report_type: str,
    src: Path, group: str, args, api_key: str | None, uploader, do_upload: bool,
) -> dict:
    """Run extraction + upload ONCE in a throwaway temp dir.

    Returns a dict describing the outcome:
        {status, error, retryable, db_event}
    where `db_event` (if present) is the payload to emit on the FINAL
    attempt so the UI only ever sees one db-upload per report.
    """
    with tempfile.TemporaryDirectory(
        prefix=f"ambeon-demo-{company_key}-",
    ) as work_dir_str:
        work_dir = Path(work_dir_str)
        # ── Extraction (writes captures + results JSON inside work_dir) ────
        try:
            if report_type == "quarterly":
                if not _Q_AVAILABLE:
                    return {"status": "skipped", "retryable": False,
                            "error": "Q_data_extraction module not available"}
                res = q_extraction.process_quarterly_for_company(
                    company_key=company_key, pdf_path=src,
                    testing_dir=work_dir, api_key=api_key,
                    model=args.model, dry_run=bool(args.dry_run), force=True,
                )
                results = work_dir / company_key / f"{company_key}_quarterly_results.json"
            else:
                res = data_retrive.process_company(
                    company=company_key, pdf_path=src, testing_dir=work_dir,
                    api_key=api_key, option=args.option, model=args.model,
                    dry_run=bool(args.dry_run), dpi=args.dpi, force=True,
                    comb_mode=bool(getattr(args, "comb_extract", False)),
                )
                results = work_dir / company_key / f"{company_key}_results.json"
            status = res.get("status", "ok") if isinstance(res, dict) else "ok"
            err = res.get("error") if isinstance(res, dict) else None
            validation = (
                res.get("validation") if isinstance(res, dict) else None
            )
            gaps = res.get("gaps") if isinstance(res, dict) else None
            if status == "missing_statements":
                gap_list = gaps or (
                    (validation or {}).get("missing_manifest", [])
                    + (validation or {}).get("failed_extraction", [])
                )
                err = (
                    f"missing statements: {', '.join(gap_list)}"
                    if gap_list else "incomplete statement extraction"
                )
                emit_log(f"Validation gaps for {label}: {err}", level="warning")
                if validation:
                    emit({"type": "validation", "company": company,
                          "companyKey": company_key, "kind": label,
                          "group": group, "report": validation})
        except Exception as ex:
            traceback.print_exc(file=sys.stderr)
            status, err = "crashed", str(ex)

        if status not in ("ok", "skipped_existing"):
            return {"status": status, "error": err,
                    "retryable": status in _RETRYABLE_STATUSES}
        if status == "missing_statements":
            return {
                "status": "missing_statements",
                "error": err,
                "retryable": True,
            }

        # Persist the rendered statement page images before the temp work dir is
        # deleted so the Comparison page can show this report's captures.
        if not args.dry_run:
            try:
                n_caps = _persist_captures(
                    company=company, company_key=company_key,
                    report_type=report_type, group=group, src=src,
                    work_dir=work_dir,
                )
                if n_caps:
                    emit_log(f"Saved {n_caps} capture image(s) for {label}.")
            except Exception as ex:
                emit_log(
                    f"Capture persist failed for {label}: {ex}",
                    level="warning",
                )

        if args.dry_run or not do_upload:
            return {"status": "ok", "error": None, "retryable": False}

        # ── Upload the JSON result to MongoDB ──────────────────────────────
        if not results.exists():
            return {
                "status": "ok", "error": "results file not produced",
                "retryable": True,
                "db_event": {
                    "type": "db-upload", "company": company,
                    "companyKey": company_key, "reportType": report_type,
                    "group": group, "status": "missing",
                    "error": f"results file not produced: {results.name}"},
            }

        try:
            doc = json.loads(results.read_text(encoding="utf-8"))
            summary = db_uploader.upload_results_data(
                doc,
                report_type,
                company_slug=company_key,
                company_name=company,
                report_group=group,
                source_pdf=str(src),
                uploader=uploader,
                quiet=True,
            )
        except Exception as ex:
            return {
                "status": "ok", "error": f"db upload failed: {ex}",
                "retryable": True,
                "db_event": {
                    "type": "db-upload", "company": company,
                    "companyKey": company_key, "reportType": report_type,
                    "group": group, "status": "error", "error": str(ex)},
            }

        tables = summary.get("tables", 0) or 0
        db_event = {
            "type": "db-upload", "company": company, "companyKey": company_key,
            "reportType": report_type, "group": group,
            "status": "ok" if summary.get("ok") else "partial",
            "tables": tables,
            "attempted": summary.get("attempted", 0),
            "year": summary.get("year"),
            "quarter": summary.get("quarter"),
            "error": (summary.get("error") or (summary.get("errors") or [None])[0]),
        }
        # 0 tables means the extraction silently produced nothing useful —
        # treat it as retryable so a transient miss gets another chance.
        retryable = tables == 0
        return {
            "status": "ok",
            "error": (None if tables else "0 tables extracted"),
            "retryable": retryable,
            "db_event": db_event,
        }


def _process_one(
    *, company: str, company_key: str, label: str, report_type: str,
    src: Path, group: str, args, api_key: str | None, uploader, do_upload: bool,
) -> tuple[str, str | None]:
    """Extract ONE report (with automatic retries) and upload it to MongoDB.

    Each attempt runs in a throwaway temp dir.  On a retryable failure we
    wait with exponential backoff and try again, up to ``--max-attempts``.
    Only the final attempt's stage-done / db-upload events are emitted so
    the UI never double-counts a report.

    Returns (status, error)."""
    emit({"type": "stage-start", "company": company,
          "companyKey": company_key, "kind": label, "group": group,
          "fileName": src.name})

    attempts   = max(1, int(getattr(args, "max_attempts", 1) or 1))
    base_delay = float(getattr(args, "retry_delay", 3.0) or 0.0)

    outcome: dict = {"status": "crashed", "error": None, "retryable": True}
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            wait = round(base_delay * (2 ** (attempt - 2)), 1)
            emit_log(
                f"Retry {attempt}/{attempts} for {label} · {src.name} "
                f"in {wait}s (previous: {outcome.get('status')}"
                f"{' — ' + str(outcome.get('error')) if outcome.get('error') else ''}).",
                level="warning",
            )
            if wait > 0:
                time.sleep(wait)

        outcome = _attempt_one(
            company=company, company_key=company_key, label=label,
            report_type=report_type, src=src, group=group, args=args,
            api_key=api_key, uploader=uploader, do_upload=do_upload,
        )
        if not outcome.get("retryable"):
            break

    status = outcome.get("status", "ok")
    err    = outcome.get("error")

    emit({"type": "stage-done", "company": company,
          "companyKey": company_key, "kind": label, "group": group,
          "status": status, "error": err})

    db_event = outcome.get("db_event")
    if db_event is not None:
        emit(db_event)

    if status not in ("ok", "skipped_existing"):
        return (status, err)
    return ("ok", err)


if __name__ == "__main__":
    raise SystemExit(main())
