"""
Data_retrive.py
===============
Local (no OpenAI) annual financial-statement extractor.

Pulls the main printed FS tables from each company annual PDF with
``printed_statements`` / pdfplumber and writes JSON under ``testing/``:

  • Income statement / Statement of profit or loss
  • Statement of comprehensive income
  • Statement of financial position
  • Statement of cash flows

OpenAI vision is disabled for now.

Usage
-----
    python Data_retrive.py
    python Data_retrive.py --only company1 --force
    python Data_retrive.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import extraction_validation as val


# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).resolve().parent
BACKEND_DIR  = SCRIPT_DIR.parent
REPORTS_DIR  = BACKEND_DIR / "reports"
TESTING_DIR  = BACKEND_DIR / "testing"
BACKEND_ENV  = BACKEND_DIR / ".env"

# OpenAI is disabled — local PDF extract only.
USE_OPENAI = False

# Main FS keys kept for annual local extract.
ANNUAL_CAPTURE_KEYS = (
    "income_statement",
    "oci",
    "sofp",
    "cash_flows",
)

from runner_common import resolve_report_year

PDF_EXT = (".pdf", ".PDF")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_company_key(name: str) -> str:
    """Make a folder-safe key out of a company folder name."""
    s = name.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._")
    return s or "company"


def _natural_sort_key(name: str):
    """Sort 'company2' before 'company10' instead of lexicographically."""
    parts = re.split(r"(\d+)", name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _pick_pdf(company_dir: Path) -> Path | None:
    """Pick the most relevant PDF for a company folder.

    Preference order:
      1. <company>/Annual/*.pdf  (newest)
      2. <company>/*.pdf         (newest)
      3. any *.pdf in any sub-directory
    """
    annual = company_dir / "Annual"
    if annual.is_dir():
        pdfs = [p for p in annual.iterdir() if p.suffix in PDF_EXT]
        if pdfs:
            return max(pdfs, key=lambda p: p.stat().st_mtime)

    direct = [p for p in company_dir.iterdir() if p.is_file() and p.suffix in PDF_EXT]
    if direct:
        return max(direct, key=lambda p: p.stat().st_mtime)

    for p in company_dir.rglob("*.pdf"):
        return p
    for p in company_dir.rglob("*.PDF"):
        return p
    return None


def _discover_companies(reports_dir: Path) -> list[tuple[str, Path, Path]]:
    """Return [(company_key, company_folder, pdf_path), ...] for every report."""
    out: list[tuple[str, Path, Path]] = []
    if not reports_dir.exists():
        return out

    for child in sorted(reports_dir.iterdir(), key=lambda p: _natural_sort_key(p.name)):
        if not child.is_dir():
            continue
        pdf = _pick_pdf(child)
        if not pdf:
            print(f"  [skip] {child.name}: no PDF found")
            continue
        key = _sanitize_company_key(child.name)
        out.append((key, child, pdf))
    return out


def _load_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser (no dependency on python-dotenv)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env[k] = v
    except Exception:
        pass
    return env


def _resolve_api_key(cli_key: str | None) -> str | None:
    if cli_key:
        return cli_key
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    env = _load_env_file(BACKEND_ENV)
    return env.get("OPENAI_API_KEY")


def _results_already_done(out_dir: Path, company: str) -> bool:
    """True only if a REAL (non-dry-run) results JSON already exists.

    A previous ``--dry-run`` execution writes a placeholder
    ``<company>_results.json`` which we deliberately treat as "not done" so
    the real run will replace it.  Detection is done via
    ``extraction_meta.json`` (written by step 3) – if it records
    ``"dry_run": true`` we ignore the leftover.
    """
    results = out_dir / f"{company}_results.json"
    if not results.exists():
        return False

    meta = out_dir / "extraction_meta.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("dry_run") is True:
                return False
        except Exception:
            pass
    return True


def _load_results_json(company_out: Path, company: str) -> dict:
    path = company_out / f"{company}_results.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _year_from_path(pdf_path: Path) -> int:
    year = resolve_report_year(
        pdf_path.parent.name,
        pdf_path.parent.parent.name if pdf_path.parent else None,
        pdf_path.name,
    )
    return year if year is not None else datetime.now().year


def _printed_payload_to_results(payload: dict) -> dict:
    """Convert printed_statements payload into Extracted-Tables JSON shape."""
    results: dict = {}
    for statement in payload.get("statements") or []:
        key = str(statement.get("key") or "").strip()
        if not key:
            continue
        title = str(statement.get("title") or key)
        if not statement.get("ok"):
            results[key] = {
                "status": "missing",
                "title": title,
                "error": "statement not found in PDF",
                "data": None,
            }
            continue
        rows = []
        for row in statement.get("rows") or []:
            if not isinstance(row, dict):
                continue
            cells = row.get("cells") or []
            rows.append(
                {
                    "cells": [("" if c is None else str(c)) for c in cells],
                    "style": str(row.get("style") or "data"),
                }
            )
        results[key] = {
            "status": "ok",
            "title": title,
            "data": {
                "statement_title": title,
                "preamble": None,
                "footnotes": None,
                "tables": [
                    {
                        "caption": None,
                        "header_rows": statement.get("header_rows") or [],
                        "rows": rows,
                    }
                ],
            },
        }
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Per-company pipeline (local PDF only — OpenAI disabled)
# ─────────────────────────────────────────────────────────────────────────────

def process_company(
    company:     str,
    pdf_path:    Path,
    testing_dir: Path,
    api_key:     str | None = None,
    option:      str = "1",
    model:       str = "printed-statements",
    dry_run:     bool = False,
    dpi:         int = 150,
    force:       bool = False,
    comb_mode:   bool = False,
) -> dict:
    """Extract main FS tables from one annual PDF with local printed extract.

    ``api_key`` / ``model`` / ``dpi`` / ``option`` are accepted for call-site
    compatibility but OpenAI is not used.
    """
    from printed_statements import extract_printed_report

    company_out = testing_dir / company
    company_out.mkdir(parents=True, exist_ok=True)
    results_path = company_out / f"{company}_results.json"
    meta_path = company_out / "extraction_meta.json"

    print(f"\n{'#'*70}")
    print(f"#  COMPANY : {company}")
    print(f"#  PDF     : {pdf_path}")
    print(f"#  OUTPUT  : {company_out}")
    print(f"#  MODE    : local printed-statements (OpenAI disabled)")
    print(f"{'#'*70}")

    if not force and _results_already_done(company_out, company):
        print(f"  [skip] {company}: results already exist — use --force to rebuild")
        return {"company": company, "status": "skipped_existing"}

    year = _year_from_path(pdf_path)
    print(f"\n  → Local PDF extract for year {year} (main FS only, no notes)")
    try:
        payload = extract_printed_report(
            pdf_path,
            company_slug=company,
            company_name=company.replace("_", " "),
            year=year,
            include_notes=False,
        )
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step1_failed", "error": str(e)}

    results = _printed_payload_to_results(payload)
    ok_keys = [k for k, v in results.items() if v.get("status") == "ok"]
    for key in ANNUAL_CAPTURE_KEYS:
        entry = results.get(key) or {}
        n_rows = 0
        tables = ((entry.get("data") or {}).get("tables") or [])
        if tables:
            n_rows = len(tables[0].get("rows") or [])
        print(
            f"  [{key}] status={entry.get('status', 'missing')} rows={n_rows}"
        )

    manifest = {
        "company": company,
        "source_pdf": str(pdf_path),
        "statements": {
            k: {"pages": s.get("pages") or []}
            for s in (payload.get("statements") or [])
            for k in [str(s.get("key") or "")]
            if k
        },
    }
    (company_out / f"{company}_pages.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if dry_run:
        meta_path.write_text(
            json.dumps(
                {
                    "company": company,
                    "source_pdf": pdf_path.name,
                    "model": "printed-statements",
                    "api_status": "disabled",
                    "dry_run": True,
                    "statements": ok_keys,
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"company": company, "status": "ok", "gaps": []}

    results_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    meta_path.write_text(
        json.dumps(
            {
                "company": company,
                "source_pdf": pdf_path.name,
                "model": "printed-statements",
                "api_status": "disabled",
                "dry_run": False,
                "statements": ok_keys,
                "year": year,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    validation = val.validate_annual_results(
        results, manifest, option="1", comb_mode=comb_mode
    )
    (company_out / "validation_report.json").write_text(
        json.dumps(validation.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if validation.ok:
        print("  [validate] main FS statements captured")
    else:
        print(
            f"  [validate] gaps: {', '.join(validation.all_gaps()) or 'none'}"
        )

    if not ok_keys:
        return {
            "company": company,
            "status": "no_statements_found",
            "validation": validation.to_dict(),
            "gaps": validation.all_gaps(),
        }

    status = "ok" if validation.ok or comb_mode or ok_keys else "missing_statements"
    # Accept partial main-FS success so a missing OCI does not force OpenAI retries.
    if ok_keys and not validation.ok:
        status = "ok"
    return {
        "company": company,
        "status": status,
        "validation": validation.to_dict(),
        "gaps": validation.all_gaps() if not validation.ok else [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Master runner
# ─────────────────────────────────────────────────────────────────────────────

def run(
    reports_dir: Path,
    testing_dir: Path,
    api_key:     str | None,
    option:      str,
    model:       str,
    dry_run:     bool,
    dpi:         int,
    only:        str | None,
    skip:        list[str],
    start_at:    str | None,
    limit:       int | None,
    force:       bool,
) -> None:

    testing_dir.mkdir(parents=True, exist_ok=True)

    companies = _discover_companies(reports_dir)
    if only:
        wanted = {only.lower(), _sanitize_company_key(only).lower()}
        companies = [c for c in companies
                     if c[0].lower() in wanted or c[1].name.lower() in wanted]
    if skip:
        skip_set = {s.lower() for s in skip}
        companies = [c for c in companies if c[0].lower() not in skip_set]
    if start_at:
        target = start_at.lower()
        try:
            idx = next(i for i, c in enumerate(companies)
                       if c[0].lower() == target or c[1].name.lower() == target)
            companies = companies[idx:]
        except StopIteration:
            print(f"  [warn] --start {start_at} not found in company list")
    if limit:
        companies = companies[:limit]

    if not companies:
        print(f"\n  No companies to process in {reports_dir}")
        return

    print(f"\n{'='*70}")
    print(f"  DATA RETRIEVE — local PDF extract (OpenAI disabled)")
    print(f"  Reports    : {reports_dir}")
    print(f"  Output     : {testing_dir}")
    print(f"  Companies  : {len(companies)}")
    print(f"  Engine     : printed-statements / pdfplumber")
    print(f"  Statements : {', '.join(ANNUAL_CAPTURE_KEYS)}")
    print(f"  Dry-run    : {dry_run}")
    print(f"  Force      : {force}")
    print(f"{'='*70}")

    print(f"\n  Planned order ({len(companies)} compan"
          f"{'y' if len(companies)==1 else 'ies'}):")
    for i, (key, _cdir, _pdf) in enumerate(companies, 1):
        out_dir = testing_dir / key
        done = (not force) and _results_already_done(out_dir, key)
        tag  = "  [will-skip: results exist]" if done else ""
        print(f"    {i:>3}. {key}{tag}")
    print()

    log: list[dict] = []
    t0 = datetime.now()

    for i, (company, _company_dir, pdf_path) in enumerate(companies, 1):
        print(f"\n[{i}/{len(companies)}]  >>> {company}")
        try:
            res = process_company(
                company     = company,
                pdf_path    = pdf_path,
                testing_dir = testing_dir,
                api_key     = api_key,
                option      = option,
                model       = model,
                dry_run     = dry_run,
                dpi         = dpi,
                force       = force,
            )
        except KeyboardInterrupt:
            print("\n  Interrupted by user — saving log so far.")
            log.append({"company": company, "status": "interrupted"})
            break
        except Exception as e:
            traceback.print_exc()
            res = {"company": company, "status": "crashed", "error": str(e)}

        log.append(res)

    # ── Summary + log ───────────────────────────────────────────────────────
    elapsed = (datetime.now() - t0).total_seconds()
    summary_path = testing_dir / "_run_log.json"
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reports_dir":  str(reports_dir),
        "testing_dir":  str(testing_dir),
        "option":       option,
        "model":        model,
        "dpi":          dpi,
        "dry_run":      dry_run,
        "force":        force,
        "elapsed_sec":  round(elapsed, 1),
        "total":        len(log),
        "results":      log,
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n{'='*70}")
    print(f"  DONE  — {len(log)} compan{'y' if len(log)==1 else 'ies'} processed "
          f"in {elapsed:.1f}s")
    print(f"  Log   -> {summary_path}")

    counts: dict[str, int] = {}
    for r in log:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for status, n in sorted(counts.items()):
        print(f"    {status:<22} : {n}")

    print(f"\n  Open the side-by-side viewer:")
    print(f"     python testing.py --serve")
    print(f"{'='*70}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Per-company driver: local printed FS extract into testing/<company>/ "
                    "(OpenAI disabled).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--reports",  type=Path, default=REPORTS_DIR,
                    help=f"Folder containing per-company report sub-folders  "
                         f"(default: {REPORTS_DIR})")
    ap.add_argument("--testing",  type=Path, default=TESTING_DIR,
                    help=f"Output folder for images + JSON  "
                         f"(default: {TESTING_DIR})")
    ap.add_argument("--apikey",   default=None,
                    help="Ignored — OpenAI is disabled")
    ap.add_argument("--option",   choices=["1", "2"], default="1",
                    help="Ignored — main FS only")
    ap.add_argument("--model",    default="printed-statements",
                    help="Ignored — local extract engine")
    ap.add_argument("--dpi",      type=int, default=150,
                    help="Ignored — local extract does not render page images")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Locate statements but skip writing final results JSON")
    ap.add_argument("--only",     default=None,
                    help="Process only this single company folder name")
    ap.add_argument("--skip",     action="append", default=[],
                    help="Skip a company (may be passed multiple times)")
    ap.add_argument("--start",    default=None,
                    help="Resume the run starting at this company")
    ap.add_argument("--limit",    type=int, default=None,
                    help="Process at most N companies")
    ap.add_argument("--force",    action="store_true",
                    help="Re-process companies even if results JSON already exists")
    args = ap.parse_args()

    run(
        reports_dir = args.reports.resolve(),
        testing_dir = args.testing.resolve(),
        api_key     = None,
        option      = "1",
        model       = "printed-statements",
        dry_run     = args.dry_run,
        dpi         = args.dpi,
        only        = args.only,
        skip        = args.skip,
        start_at    = args.start,
        limit       = args.limit,
        force       = args.force,
    )
