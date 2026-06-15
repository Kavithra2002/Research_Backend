"""
Extract_selected_reports.py
===========================

Driver that takes a list of "selected" report PDFs (the ones the user ticked
on the *Newly uploaded reports* panel) and, for each one:

1.  Ensures the source PDF exists locally (downloads from R2
    ``updated_reports/`` when ``STORAGE_DRIVER=r2``), then copies from

        newly_uploaded_report/<company>/<reportType>/<file>.pdf

    into the company sub-folder of ``testing/``:

        testing/<company>/<file>.pdf

2.  Runs the per-company Data_retrive pipeline (STEP 1 -> STEP 2 -> STEP 3)
    so the extracted page captures and OpenAI JSON results land **inside the
    same** ``testing/<company>/`` folder, then uploads that folder to R2
    ``testing/<company>/`` when using R2 storage.

The script emits NDJSON progress lines to stdout so the Next.js API route
(``/api/system/update``) can stream live progress back to the browser.

Input
-----
A JSON file (path passed via ``--items``) containing either::

    {"items": [ { "company": "...", "report_type": "...", "file_name": "..." }, ... ]}

or just the raw list::

    [ { "company": "...", "report_type": "...", "file_name": "..." }, ... ]

Each item identifies a single PDF inside ``newly_uploaded_report/``.

Usage
-----
    python Extract_selected_reports.py --items selections.json
    python Extract_selected_reports.py --items selections.json --dry-run
    python Extract_selected_reports.py --items selections.json --model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import json
import shutil
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
import r2_storage

# Quarterly pipeline (per-PDF OpenAI extraction with Sri-Lankan
# CSE-quarterly-specific verbatim prompts).
try:
    import Q_data_extraction as q_extraction
    _Q_EXTRACTION_AVAILABLE = True
except Exception as _q_err:
    q_extraction = None  # type: ignore
    _Q_EXTRACTION_AVAILABLE = False
    _Q_EXTRACTION_IMPORT_ERROR = str(_q_err)


SCRIPT_DIR     = Path(__file__).resolve().parent
BACKEND_DIR    = SCRIPT_DIR.parent
SOURCE_DEFAULT = BACKEND_DIR / "newly_uploaded_report"
TESTING_DEFAULT = BACKEND_DIR / "testing"


# ─────────────────────────────────────────────────────────────────────────────
# NDJSON helpers
# ─────────────────────────────────────────────────────────────────────────────

def emit(obj: dict[str, Any]) -> None:
    """Write a single NDJSON record and flush so the parent sees it live."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_log(text: str, level: str = "info") -> None:
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip():
            emit({"type": "log", "level": level, "message": line})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_items(items_path: Path) -> list[dict[str, str]]:
    """Read the selections JSON and normalise it to a flat list of dicts."""
    raw = json.loads(items_path.read_text(encoding="utf-8"))
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
        if not (company and report_type and file_name):
            continue
        out.append({
            "company": company,
            "report_type": report_type,
            "file_name": file_name,
        })
    return out


def _group_by_company(items: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for it in items:
        company = it["company"]
        if company not in grouped:
            grouped[company] = []
            order.append(company)
        grouped[company].append(it)
    return grouped, order


def _pick_driver_pdf(pdfs: list[Path]) -> Path | None:
    """Pick the PDF that should drive the extraction for a company.

    Preference: any file whose name hints at the annual report, otherwise the
    most recently modified file in the bunch.
    """
    if not pdfs:
        return None

    def priority(p: Path) -> tuple[int, float]:
        name = p.name.lower()
        if "annual" in name:
            score = 0
        elif "quarter" in name or "interim" in name:
            score = 1
        else:
            score = 2
        # Newest first within each tier.
        return (score, -p.stat().st_mtime)

    return sorted(pdfs, key=priority)[0]


def _is_quarterly_type(report_type: str) -> bool:
    """Treat any 'Quarterly' / 'Interim' / 'Q1..Q4' / 'half year' folder name
    as a quarterly report.  All other report types route to the existing
    annual pipeline."""
    if not report_type:
        return False
    t = report_type.strip().lower()
    if t in ("quarterly", "quarter", "interim", "interim_data"):
        return True
    if any(tok in t for tok in (
        "quarter", "interim", "q1", "q2", "q3", "q4",
        "half year", "halfyear", "half-year",
    )):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items", required=True, type=Path,
                    help="Path to a JSON file with the selected items.")
    ap.add_argument("--source", type=Path, default=SOURCE_DEFAULT,
                    help=f"Folder containing newly downloaded reports "
                         f"(default: {SOURCE_DEFAULT}).")
    ap.add_argument("--testing", type=Path, default=TESTING_DEFAULT,
                    help=f"Output folder (default: {TESTING_DEFAULT}).")
    ap.add_argument("--apikey", default=None,
                    help="OpenAI API key (else OPENAI_API_KEY env or .env).")
    ap.add_argument("--option", choices=["1", "2"], default="1",
                    help="1 = core statements  2 = core + Notes  (default: 1).")
    ap.add_argument("--model", default="gpt-4o",
                    help="OpenAI model name (default: gpt-4o).")
    ap.add_argument("--dpi", type=int, default=150,
                    help="Image render DPI (default: 150).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render images but DON'T call OpenAI.")
    ap.add_argument("--force", action="store_true",
                    help="Re-process companies even if results already exist.")
    args = ap.parse_args(argv)

    items_path: Path = args.items
    if not items_path.exists():
        emit({"type": "error", "message": f"Items file not found: {items_path}"})
        return 2

    try:
        items = _load_items(items_path)
    except Exception as ex:
        emit({"type": "error", "message": f"Failed to parse items file: {ex!r}"})
        return 2

    if not items:
        emit({"type": "error", "message": "No selected items provided."})
        return 2

    source_root: Path  = args.source.resolve()
    testing_dir: Path  = args.testing.resolve()
    testing_dir.mkdir(parents=True, exist_ok=True)

    grouped, order = _group_by_company(items)
    total_files     = sum(len(v) for v in grouped.values())
    total_companies = len(grouped)

    emit({
        "type": "start",
        "totalFiles": total_files,
        "totalCompanies": total_companies,
        "sourceDir": str(source_root),
        "testingDir": str(testing_dir),
        "dryRun": bool(args.dry_run),
        "option": args.option,
        "model": args.model,
    })

    # Resolve API key up-front; fail fast if missing.
    api_key = data_retrive._resolve_api_key(args.apikey)
    if not args.dry_run and not api_key:
        emit({
            "type": "error",
            "message": (
                "OpenAI API key not found. Pass --apikey, set OPENAI_API_KEY, "
                "or add it to backend/.env."
            ),
        })
        return 1

    # ── COPY PHASE ────────────────────────────────────────────────────────────
    emit({"type": "copy-start", "total": total_files})

    file_idx = 0
    # Per company, separate the copied PDFs into "annual" and "quarterly"
    # buckets so the extraction phase can route each report type to the
    # right pipeline.
    copied_annual:    dict[str, list[Path]] = {}
    copied_quarterly: dict[str, list[Path]] = {}

    for company in order:
        company_key = data_retrive._sanitize_company_key(company)
        dest_company = testing_dir / company_key
        dest_company.mkdir(parents=True, exist_ok=True)

        for entry in grouped[company]:
            file_idx += 1
            src = source_root / entry["company"] / entry["report_type"] / entry["file_name"]
            dest = dest_company / entry["file_name"]

            if r2_storage.is_r2_enabled():
                emit({
                    "type": "log",
                    "level": "info",
                    "message": (
                        f"Downloading {entry['file_name']} from R2 "
                        f"({entry['company']} / {entry['report_type']})..."
                    ),
                })
            resolved = r2_storage.ensure_updated_report_local(
                entry["company"],
                entry["report_type"],
                entry["file_name"],
                source_root,
            )
            if resolved is None:
                emit({
                    "type": "copy",
                    "index": file_idx,
                    "total": total_files,
                    "company": company,
                    "reportType": entry["report_type"],
                    "fileName": entry["file_name"],
                    "status": "missing",
                    "error": f"Source file not found locally or in R2: {src}",
                })
                continue
            src = resolved

            try:
                if dest.exists() and dest.stat().st_size == src.stat().st_size:
                    status = "skipped"
                else:
                    shutil.copy2(src, dest)
                    status = "copied"
            except Exception as ex:
                emit({
                    "type": "copy",
                    "index": file_idx,
                    "total": total_files,
                    "company": company,
                    "reportType": entry["report_type"],
                    "fileName": entry["file_name"],
                    "status": "error",
                    "error": str(ex),
                })
                continue

            bucket = (
                copied_quarterly if _is_quarterly_type(entry["report_type"])
                else copied_annual
            )
            bucket.setdefault(company, []).append(dest)

            emit({
                "type": "copy",
                "index": file_idx,
                "total": total_files,
                "company": company,
                "reportType": entry["report_type"],
                "fileName": entry["file_name"],
                "status": status,
                "destination": str(dest),
            })

    # ── EXTRACT PHASE ─────────────────────────────────────────────────────────
    ok = 0
    failed = 0
    company_idx = 0

    if not _Q_EXTRACTION_AVAILABLE:
        emit({
            "type": "log",
            "level": "warning",
            "message": (
                f"Q_data_extraction not importable ({_Q_EXTRACTION_IMPORT_ERROR}); "
                "Quarterly selections will be skipped."
            ),
        })

    for company in order:
        company_idx += 1
        company_key = data_retrive._sanitize_company_key(company)

        annual_pdfs    = copied_annual.get(company, [])
        quarterly_pdfs = copied_quarterly.get(company, [])

        if not annual_pdfs and not quarterly_pdfs:
            emit({
                "type": "company-done",
                "index": company_idx,
                "total": total_companies,
                "company": company,
                "companyKey": company_key,
                "status": "no_files_copied",
                "error": "no source files were available for this company",
            })
            failed += 1
            continue

        emit({
            "type": "company-start",
            "index": company_idx,
            "total": total_companies,
            "company": company,
            "companyKey": company_key,
            "pdfFile": ", ".join(
                p.name for p in (annual_pdfs + quarterly_pdfs)
            ),
            "annualCount":    len(annual_pdfs),
            "quarterlyCount": len(quarterly_pdfs),
        })

        statuses: list[tuple[str, str, str | None]] = []  # (kind, status, err)

        # ── Annual sub-run (existing Data_retrive pipeline) ───────────────
        if annual_pdfs:
            pdf_path = _pick_driver_pdf(annual_pdfs)
            try:
                res = data_retrive.process_company(
                    company     = company_key,
                    pdf_path    = pdf_path,
                    testing_dir = testing_dir,
                    api_key     = api_key,
                    option      = args.option,
                    model       = args.model,
                    dry_run     = bool(args.dry_run),
                    dpi         = args.dpi,
                    force       = bool(args.force),
                )
                a_status = res.get("status", "unknown") if isinstance(res, dict) else "unknown"
                a_err    = res.get("error") if isinstance(res, dict) else None
            except KeyboardInterrupt:
                emit({
                    "type": "company-done",
                    "index": company_idx,
                    "total": total_companies,
                    "company": company,
                    "companyKey": company_key,
                    "status": "interrupted",
                })
                failed += 1
                break
            except Exception as ex:
                traceback.print_exc(file=sys.stderr)
                a_status, a_err = "crashed", str(ex)
            statuses.append(("Annual", a_status, a_err))
            emit({
                "type":       "stage-done",
                "company":    company,
                "companyKey": company_key,
                "kind":       "Annual",
                "status":     a_status,
                "error":      a_err,
                "pdfFile":    pdf_path.name if pdf_path else "",
            })

        # ── Quarterly sub-run (new Q_data_extraction pipeline) ────────────
        if quarterly_pdfs and _Q_EXTRACTION_AVAILABLE:
            for q_pdf in quarterly_pdfs:
                try:
                    q_res = q_extraction.process_quarterly_for_company(
                        company_key = company_key,
                        pdf_path    = q_pdf,
                        testing_dir = testing_dir,
                        api_key     = api_key,
                        model       = args.model,
                        dry_run     = bool(args.dry_run),
                        force       = bool(args.force),
                    )
                    q_status = q_res.get("status", "ok") if isinstance(q_res, dict) else "unknown"
                    q_err    = q_res.get("error") if isinstance(q_res, dict) else None
                except KeyboardInterrupt:
                    emit({
                        "type":       "company-done",
                        "index":      company_idx,
                        "total":      total_companies,
                        "company":    company,
                        "companyKey": company_key,
                        "status":     "interrupted",
                    })
                    failed += 1
                    break
                except Exception as ex:
                    traceback.print_exc(file=sys.stderr)
                    q_status, q_err = "crashed", str(ex)
                statuses.append(("Quarterly", q_status, q_err))
                emit({
                    "type":       "stage-done",
                    "company":    company,
                    "companyKey": company_key,
                    "kind":       "Quarterly",
                    "status":     q_status,
                    "error":      q_err,
                    "pdfFile":    q_pdf.name,
                })
        elif quarterly_pdfs and not _Q_EXTRACTION_AVAILABLE:
            statuses.append(("Quarterly", "skipped",
                             "Q_data_extraction module not available"))

        # ── Aggregate per-company status ──────────────────────────────────
        if not statuses:
            company_status, company_err = "no_files_copied", None
        elif all(s in ("ok", "skipped_existing") for _, s, _ in statuses):
            company_status, company_err = "ok", None
        else:
            company_status = next(
                (s for _, s, _ in statuses if s not in ("ok", "skipped_existing")),
                "ok",
            )
            company_err = next(
                (e for _, s, e in statuses
                 if s not in ("ok", "skipped_existing") and e),
                None,
            )

        # Push extraction artefacts to R2 so the hosted frontend can read them.
        if company_status in ("ok", "skipped_existing") and r2_storage.is_r2_enabled():
            company_out = testing_dir / company_key
            if company_out.is_dir():
                try:
                    uploaded = r2_storage.upload_directory(
                        company_out,
                        r2_storage.object_key(r2_storage.TESTING_PREFIX, company_key),
                    )
                    emit({
                        "type": "log",
                        "level": "info",
                        "message": (
                            f"Uploaded {uploaded} file(s) to R2 "
                            f"testing/{company_key}/"
                        ),
                    })
                except Exception as ex:
                    emit({
                        "type": "log",
                        "level": "warning",
                        "message": f"R2 upload for {company_key} failed: {ex!r}",
                    })

        if company_status in ("ok", "skipped_existing"):
            ok += 1
        else:
            failed += 1

        emit({
            "type":       "company-done",
            "index":      company_idx,
            "total":      total_companies,
            "company":    company,
            "companyKey": company_key,
            "status":     company_status,
            "error":      company_err,
            "stages":     [
                {"kind": k, "status": s, "error": e}
                for k, s, e in statuses
            ],
        })

    emit({
        "type": "done",
        "totalFiles": total_files,
        "totalCompanies": total_companies,
        "ok": ok,
        "failed": failed,
        "testingDir": str(testing_dir),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
