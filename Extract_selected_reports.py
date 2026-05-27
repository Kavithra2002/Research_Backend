"""
Extract_selected_reports.py
===========================

Driver that takes a list of "selected" report PDFs (the ones the user ticked
on the *Newly uploaded reports* panel) and, for each one:

1.  Copies the source PDF from

        newly_uploaded_report/<company>/<reportType>/<file>.pdf

    into the company sub-folder of ``testing/``:

        testing/<company>/<file>.pdf

2.  Runs the per-company Data_retrive pipeline (STEP 1 -> STEP 2 -> STEP 3)
    so the extracted page captures and OpenAI JSON results land **inside the
    same** ``testing/<company>/`` folder.

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


SCRIPT_DIR     = Path(__file__).resolve().parent
SOURCE_DEFAULT = SCRIPT_DIR / "newly_uploaded_report"
TESTING_DEFAULT = SCRIPT_DIR / "testing"


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
    copied: dict[str, list[Path]] = {}
    for company in order:
        company_key = data_retrive._sanitize_company_key(company)
        dest_company = testing_dir / company_key
        dest_company.mkdir(parents=True, exist_ok=True)

        for entry in grouped[company]:
            file_idx += 1
            src = source_root / entry["company"] / entry["report_type"] / entry["file_name"]
            dest = dest_company / entry["file_name"]

            if not src.exists():
                emit({
                    "type": "copy",
                    "index": file_idx,
                    "total": total_files,
                    "company": company,
                    "fileName": entry["file_name"],
                    "status": "missing",
                    "error": f"Source file not found: {src}",
                })
                continue

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
                    "fileName": entry["file_name"],
                    "status": "error",
                    "error": str(ex),
                })
                continue

            copied.setdefault(company, []).append(dest)
            emit({
                "type": "copy",
                "index": file_idx,
                "total": total_files,
                "company": company,
                "fileName": entry["file_name"],
                "status": status,
                "destination": str(dest),
            })

    # ── EXTRACT PHASE ─────────────────────────────────────────────────────────
    ok = 0
    failed = 0
    company_idx = 0

    for company in order:
        company_idx += 1
        company_key = data_retrive._sanitize_company_key(company)
        pdfs = copied.get(company, [])
        if not pdfs:
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

        pdf_path = _pick_driver_pdf(pdfs)
        emit({
            "type": "company-start",
            "index": company_idx,
            "total": total_companies,
            "company": company,
            "companyKey": company_key,
            "pdfFile": pdf_path.name if pdf_path else "",
        })

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
            emit({
                "type": "company-done",
                "index": company_idx,
                "total": total_companies,
                "company": company,
                "companyKey": company_key,
                "status": "crashed",
                "error": str(ex),
            })
            failed += 1
            continue

        status = res.get("status", "unknown") if isinstance(res, dict) else "unknown"
        err    = res.get("error") if isinstance(res, dict) else None
        if status in ("ok", "skipped_existing"):
            ok += 1
        else:
            failed += 1

        emit({
            "type": "company-done",
            "index": company_idx,
            "total": total_companies,
            "company": company,
            "companyKey": company_key,
            "status": status,
            "error": err,
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
