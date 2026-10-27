"""
Demo_run.py
===========
End-to-end driver for the **"Test here"** page.

Given a list of demo report PDFs the user ticked in the UI (each one living
under ``backend/Demo_Data/<COMPANY>/<reportType>/<file>.pdf``), this script,
per company:

  1. Copies the selected PDF(s) into ``testing/<company_key>/``.
  2. Runs the matching extraction pipeline:
        • Annual    -> Data_retrive (step1 find → step2 capture → step3 OpenAI)
        • Quarterly -> Q_data_extraction (capture pages → OpenAI vision)
  3. Immediately UPLOADS OpenAI-extracted tables into MongoDB.

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
    python Demo_run.py --items selections.json --model gpt-5 --no-upload
"""

from __future__ import annotations

import argparse
import json
import re
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


import capture_store
from runner_common import resolve_report_year as _year_from_text

SCRIPT_DIR      = Path(__file__).resolve().parent
BACKEND_DIR     = SCRIPT_DIR.parent
DEMO_DEFAULT     = BACKEND_DIR / "Demo_Data"
DEMO_CAPTURES_OUT = capture_store.DEMO_CAPTURES_OUT


def _persist_captures(
    *,
    company: str,
    company_key: str,
    report_type: str,
    group: str,
    src: Path,
    work_dir: Path,
) -> int:
    """Replace this report year's page images in the fixed capture folder.

    Layout (stable link the Comparison page reads after a restart)::

        Demo_Data_captures/<company>/<Annual|Quarterly>/<year>/<stmt>/page_*.png
        Demo_Data_captures/<company>/<Annual|Quarterly>/<year>/index.json

    A rerun deletes the previous main-statement images for that year and
    writes the new captures in their place.
    """
    period = "Quarterly" if report_type == "quarterly" else "Annual"
    captures_name = (
        "captures_quarterly" if report_type == "quarterly" else "captures"
    )
    src_caps = work_dir / company_key / captures_name
    if not src_caps.is_dir():
        return 0

    year = _year_from_text(
        group,
        src.parent.name if src else None,
        src.name if src else None,
    )
    if year is None:
        return 0

    dest_root = capture_store.year_capture_dir(company, period, year)
    copied = capture_store.install_capture_tree(dest_root, src_caps)
    if copied:
        capture_store.write_capture_index(
            dest_root, company=company, period=period, year=year,
        )
    return copied


def _persist_printed_captures(
    *,
    company: str,
    year: int,
    pdf_path: Path,
    statements: list,
    dpi: int = 150,
) -> int:
    """Render the printed statement pages into the same fixed year folder."""
    import step2_capture_pages as step2

    grouped: dict[str, list[tuple[str, bytes]]] = {}
    for stmt in statements or []:
        if not isinstance(stmt, dict):
            continue
        key = str(stmt.get("key") or "").strip()
        if not key or key.lower().startswith("note_"):
            continue
        pages: list[int] = []
        for raw in stmt.get("pages") or []:
            try:
                page_no = int(raw)
            except (TypeError, ValueError):
                continue
            if page_no >= 1 and page_no not in pages:
                pages.append(page_no)
        blobs: list[tuple[str, bytes]] = []
        for page_no in pages:
            png = step2.render_full_page(str(pdf_path), page_no - 1, dpi=dpi)
            if png:
                blobs.append((f"page_{page_no:04d}.png", png))
        if blobs:
            grouped[key] = blobs
    if not grouped:
        return 0

    dest_root = capture_store.year_capture_dir(company, "Annual", year)
    written = capture_store.install_rendered_statements(dest_root, grouped)
    if written:
        capture_store.write_capture_index(
            dest_root, company=company, period="Annual", year=year,
        )
    return written


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
                    help="OpenAI API key (else OPENAI_API_KEY env / backend/.env).")
    ap.add_argument("--option", choices=["1", "2"], default="1",
                    help="1=core statements only  2=core+notes.")
    ap.add_argument("--model", default="gpt-5",
                    help="OpenAI vision model (default: gpt-5).")
    ap.add_argument("--dpi", type=int, default=150, help="Render DPI (default 150).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Locate statements but skip MongoDB upload.")
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
    if api_key:
        emit_log(f"OpenAI enabled — model {args.model} (captured pages → vision extract).")
    else:
        emit_log(
            "OPENAI_API_KEY not set — falling back to local PDF extractors.",
            level="warning",
        )

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


def _attempt_annual_printed(
    *,
    company: str,
    company_key: str,
    src: Path,
    group: str,
    dry_run: bool,
    do_upload: bool,
) -> dict:
    """Fast annual path: local printed FS tables only (no OpenAI / notes)."""
    from annual_db_extractor import _store_printed
    from comb_workbook_store import ensure_indexes, get_db
    from printed_statements import default_output_path, extract_printed_report

    year = _year_from_text(group, src.parent.name if src else None, src.name)
    if year is None:
        return {
            "status": "crashed",
            "error": "could not infer report year from path/group",
            "retryable": False,
        }

    t0 = time.perf_counter()
    emit_log(
        f"Fast annual extract {company_key} {year} "
        f"(main FS only — printed PDF, no OpenAI)…"
    )
    try:
        payload = extract_printed_report(
            src,
            company_slug=company_key,
            company_name=company,
            year=year,
            include_notes=False,
        )
    except Exception as ex:
        traceback.print_exc(file=sys.stderr)
        return {"status": "crashed", "error": str(ex), "retryable": True}

    ok_stmts = [s for s in (payload.get("statements") or []) if s.get("ok")]
    for statement in payload.get("statements") or []:
        emit_log(
            f"  {statement.get('title')}: "
            f"{len(statement.get('rows') or [])} rows "
            f"pages {statement.get('pages') or []}"
        )

    if not dry_run:
        try:
            n_caps = _persist_printed_captures(
                company=company,
                year=year,
                pdf_path=src,
                statements=payload.get("statements") or [],
            )
            if n_caps:
                emit_log(
                    f"Saved {n_caps} capture image(s) for annual {year}."
                )
        except Exception as ex:
            emit_log(
                f"Capture persist failed for annual {year}: {ex}",
                level="warning",
            )

    if dry_run:
        elapsed = time.perf_counter() - t0
        emit_log(f"Dry-run annual {year} finished in {elapsed:.1f}s")
        return {"status": "ok", "error": None, "retryable": False}

    out = default_output_path(company_key, year)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if not do_upload:
        return {"status": "ok", "error": None, "retryable": False}

    try:
        db, client = get_db()
        try:
            ensure_indexes(db)
            filled, missing = _store_printed(db, payload, include_notes=False)
        finally:
            client.close()
    except Exception as ex:
        return {
            "status": "ok",
            "error": f"db upload failed: {ex}",
            "retryable": True,
            "db_event": {
                "type": "db-upload",
                "company": company,
                "companyKey": company_key,
                "reportType": "annual",
                "group": group,
                "status": "error",
                "error": str(ex),
            },
        }

    elapsed = time.perf_counter() - t0
    tables = len(ok_stmts)
    emit_log(
        f"Annual {year} stored in {elapsed:.1f}s "
        f"({tables} statements, {filled} data rows, {missing} missing)."
    )
    return {
        "status": "ok" if tables else "missing_statements",
        "error": None if tables else "no main FS tables found",
        # Printed extract is deterministic — retrying the same PDF won't help.
        "retryable": False,
        "db_event": {
            "type": "db-upload",
            "company": company,
            "companyKey": company_key,
            "reportType": "annual",
            "group": group,
            "status": "ok" if tables else "error",
            "tables": tables,
            "year": year,
            "cells_filled": filled,
            "cells_missing": missing,
        },
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
    # Annual without an API key keeps the fast local printed path.
    if report_type == "annual" and not api_key:
        return _attempt_annual_printed(
            company=company,
            company_key=company_key,
            src=src,
            group=group,
            dry_run=bool(args.dry_run),
            do_upload=do_upload,
        )

    with tempfile.TemporaryDirectory(
        prefix=f"ambeon-demo-{company_key}-",
    ) as work_dir_str:
        work_dir = Path(work_dir_str)
        # ── Extraction (writes captures + results JSON inside work_dir) ────
        try:
            if report_type == "annual":
                res = data_retrive.process_company(
                    company=company_key,
                    pdf_path=src,
                    testing_dir=work_dir,
                    api_key=api_key,
                    option=str(getattr(args, "option", "1") or "1"),
                    model=args.model,
                    dry_run=bool(args.dry_run),
                    dpi=int(getattr(args, "dpi", 150) or 150),
                    force=True,
                )
                results = work_dir / company_key / f"{company_key}_results.json"
            else:
                if not _Q_AVAILABLE:
                    return {"status": "skipped", "retryable": False,
                            "error": "Q_data_extraction module not available"}
                res = q_extraction.process_quarterly_for_company(
                    company_key=company_key, pdf_path=src,
                    testing_dir=work_dir, api_key=api_key,
                    model=args.model, dry_run=bool(args.dry_run), force=True,
                )
                results = work_dir / company_key / f"{company_key}_quarterly_results.json"
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
        if summary.get("dropped_ok_keys"):
            log.warning(
                "DB upload dropped OK statements for %s: %s",
                company_key,
                summary.get("dropped_ok_keys"),
            )
        if summary.get("errors"):
            for err in summary.get("errors") or []:
                log.warning("DB upload issue for %s: %s", company_key, err)
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

        if (
            tables
            and uploader is not None
            and summary.get("year")
            and summary.get("quarter")
        ):
            try:
                from extract_comb_data import extract_quarterly_comb

                year = int(summary["year"])
                quarter = str(summary["quarter"]).strip().upper()
                q_res = extract_quarterly_comb(
                    uploader.db, company_key, year, quarter
                )
                emit_log(
                    f"COMB quarterly {quarter} {year}: "
                    f"{q_res.get('cells_filled', 0)} cells filled, "
                    f"{q_res.get('cells_missing', 0)} missing.",
                    level="info",
                )
            except Exception as ex:
                emit_log(
                    f"COMB quarterly extract failed for {label}: {ex}",
                    level="warning",
                )
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
