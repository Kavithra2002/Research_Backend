"""
Data_retrive.py
===============
Annual financial-statement extractor.

Pipeline (when OPENAI_API_KEY is set):
  1. step1_find_pages   — locate statement pages in the PDF
  2. step2_capture_pages — render those pages to PNG captures
  3. step3_send_to_openai — OpenAI vision verbatim table extract

Falls back to local ``printed_statements`` / pdfplumber when no API key
is available.

Writes JSON under ``testing/<company>/``:

  • Income statement / Statement of profit or loss
  • Statement of comprehensive income
  • Statement of financial position
  • Statement of cash flows

Usage
-----
    python Data_retrive.py
    python Data_retrive.py --only company1 --force
    python Data_retrive.py --dry-run --apikey sk-...
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

# Prefer OpenAI vision on captured statement pages when a key is available.
USE_OPENAI = True

# Main FS keys kept for annual extract.
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
# Per-company pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _process_company_printed(
    company: str,
    pdf_path: Path,
    testing_dir: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    comb_mode: bool = False,
) -> dict:
    """Fallback: local printed_statements extract (no OpenAI)."""
    from printed_statements import extract_printed_report

    company_out = testing_dir / company
    company_out.mkdir(parents=True, exist_ok=True)
    results_path = company_out / f"{company}_results.json"
    meta_path = company_out / "extraction_meta.json"

    print(f"\n{'#'*70}")
    print(f"#  COMPANY : {company}")
    print(f"#  PDF     : {pdf_path}")
    print(f"#  OUTPUT  : {company_out}")
    print(f"#  MODE    : local printed-statements (no API key)")
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

    # Layout accuracy gate (no capture re-extract without OpenAI).
    try:
        import extraction_accuracy as acc

        needs = acc.keys_needing_reextract(payload, report_year=year)
        (company_out / "accuracy_report.json").write_text(
            json.dumps(
                {
                    "ok": len(needs) == 0,
                    "remaining": needs,
                    "notes": [
                        "Local printed extract — set OPENAI_API_KEY to "
                        "re-extract wrong/missing values from page captures."
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if needs:
            print("  [accuracy] layout/value issues (no OpenAI re-extract):")
            for key, reasons in needs.items():
                for r in reasons:
                    print(f"    ! {r}")
        else:
            print("  [accuracy] printed layout looks consistent")
    except Exception as e:
        print(f"  [warn] accuracy check skipped: {e}")

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
    if ok_keys and not validation.ok:
        status = "ok"
    return {
        "company": company,
        "status": status,
        "validation": validation.to_dict(),
        "gaps": validation.all_gaps() if not validation.ok else [],
    }


def process_company(
    company:     str,
    pdf_path:    Path,
    testing_dir: Path,
    api_key:     str | None = None,
    option:      str = "1",
    model:       str = "gpt-5",
    dry_run:     bool = False,
    dpi:         int = 150,
    force:       bool = False,
    comb_mode:   bool = False,
) -> dict:
    """Extract main FS tables from one annual PDF.

    With an OpenAI key: step1 find pages → step2 capture → step3 vision extract.
    Without a key: local printed_statements fallback.
    """
    resolved_key = _resolve_api_key(api_key) if USE_OPENAI else None
    if not resolved_key or not USE_OPENAI:
        if USE_OPENAI and not resolved_key:
            print("  [warn] OPENAI_API_KEY not set — using local printed extract")
        return _process_company_printed(
            company,
            pdf_path,
            testing_dir,
            dry_run=dry_run,
            force=force,
            comb_mode=comb_mode,
        )

    import time

    import extraction_timing as timing
    import step1_find_pages as step1
    import step2_capture_pages as step2
    import step3_send_to_openai as step3

    company_out = testing_dir / company
    company_out.mkdir(parents=True, exist_ok=True)
    pages_json = company_out / f"{company}_pages.json"
    captures_dir = company_out / "captures"
    results_path = company_out / f"{company}_results.json"
    t_all = time.monotonic()

    print(f"\n{'#'*70}")
    print(f"#  COMPANY : {company}")
    print(f"#  PDF     : {pdf_path}")
    print(f"#  OUTPUT  : {company_out}")
    print(f"#  MODE    : OpenAI vision (step1 → step2 → step3)")
    print(f"#  MODEL   : {model}")
    print(f"#  TARGET  : ~{timing.TARGET_SECONDS:.0f}s typical / {timing.MAX_SECONDS:.0f}s max")
    print(f"{'#'*70}")

    if not force and _results_already_done(company_out, company):
        print(f"  [skip] {company}: results already exist — use --force to rebuild")
        return {"company": company, "status": "skipped_existing"}

    # STEP 1 — locate statement pages (core FS only for option 1)
    print("\n  → STEP 1: find statement pages")
    t1 = time.monotonic()
    try:
        only = list(ANNUAL_CAPTURE_KEYS) if option == "1" else None
        manifest = step1.run(
            pdf_path=pdf_path,
            company=company,
            out_path=pages_json,
            verbose=True,
            only_keys=only,
            reuse_manifest=not force,
            api_key=resolved_key,
            model=model,
        )
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step1_failed", "error": str(e)}
    timing.log_timing("STEP 1", t1)

    if not manifest or not (manifest.get("statements") or {}):
        return {
            "company": company,
            "status": "no_statements_found",
            "error": "step1 found no financial statement pages",
        }

    # STEP 2 — render page images (needed pages only, parallel)
    print("\n  → STEP 2: capture statement page images")
    t2 = time.monotonic()
    try:
        cap = step2.run(
            manifest_path=pages_json,
            out_dir=captures_dir,
            dpi=dpi,
            only_keys=list(ANNUAL_CAPTURE_KEYS) if option == "1" else None,
        )
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step2_failed", "error": str(e)}
    timing.log_timing("STEP 2", t2)

    img_map = (cap or {}).get("img_map") or {}
    n_imgs = sum(len(v) for v in img_map.values())
    print(f"  [step2] {n_imgs} page image(s) across {len(img_map)} statement(s)")
    if n_imgs == 0 and not dry_run:
        return {
            "company": company,
            "status": "step2_failed",
            "error": "no page images captured for OpenAI",
        }

    if dry_run:
        print("  [dry-run] skipping OpenAI step3")
        return {"company": company, "status": "ok", "gaps": []}

    # STEP 3 — OpenAI vision extract (parallel per statement)
    print("\n  → STEP 3: OpenAI vision extract from captures")
    t3 = time.monotonic()
    try:
        results = step3.run(
            manifest_path=pages_json,
            captures_dir=captures_dir,
            api_key=resolved_key,
            option=option,
            model=model,
            dry_run=False,
            out_dir=company_out,
        )
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step3_failed", "error": str(e)}
    timing.log_timing("STEP 3", t3)

    if not isinstance(results, dict):
        results = {}

    # STEP 3b — one accuracy gate; re-extract only wrong/missing keys.
    print("\n  → STEP 3b: accuracy recheck (re-extract bad statements if needed)")
    t3b = time.monotonic()
    try:
        import extraction_accuracy as acc

        year = _year_from_path(pdf_path)
        results, accuracy = acc.accuracy_pass_annual(
            results=results,
            manifest_path=pages_json,
            captures_dir=captures_dir,
            api_key=resolved_key,
            model=model,
            report_year=year,
            max_rounds=timing.DEFAULT_ACCURACY_ROUNDS,
            log=lambda msg, level="info": print(
                f"  [accuracy{'!' if level == 'warn' else ''}] {msg}"
            ),
        )
        results_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (company_out / "accuracy_report.json").write_text(
            json.dumps(accuracy, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"  [warn] accuracy recheck failed: {e}")
    timing.log_timing("STEP 3b accuracy", t3b)
    timing.log_timing("TOTAL report", t_all)

    ok_keys = [
        k for k, v in results.items()
        if isinstance(v, dict) and v.get("status") == "ok"
    ]
    for key in ANNUAL_CAPTURE_KEYS:
        entry = results.get(key) or {}
        n_rows = 0
        tables = ((entry.get("data") or {}).get("tables") or [])
        if tables:
            n_rows = len(tables[0].get("rows") or [])
        print(
            f"  [{key}] status={entry.get('status', 'missing')} rows={n_rows}"
        )

    validation = val.validate_annual_results(
        results, manifest, option=option, comb_mode=comb_mode
    )
    (company_out / "validation_report.json").write_text(
        json.dumps(validation.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if validation.ok:
        print("  [validate] main FS statements captured via OpenAI")
    else:
        print(
            f"  [validate] gaps: {', '.join(validation.all_gaps()) or 'none'}"
        )

    if not ok_keys and not results_path.exists():
        return {
            "company": company,
            "status": "no_statements_found",
            "validation": validation.to_dict(),
            "gaps": validation.all_gaps(),
        }

    status = "ok" if validation.ok or comb_mode or ok_keys else "missing_statements"
    if ok_keys and not validation.ok:
        status = "ok"
    return {
        "company": company,
        "status": status,
        "validation": validation.to_dict(),
        "gaps": validation.all_gaps() if not validation.ok else [],
        "model": model,
        "api_status": "ok",
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
    print(f"  DATA RETRIEVE — OpenAI vision extract (step1→2→3)")
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
        description="Per-company driver: find/capture pages → OpenAI table extract "
                    "into testing/<company>/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--reports",  type=Path, default=REPORTS_DIR,
                    help=f"Folder containing per-company report sub-folders  "
                         f"(default: {REPORTS_DIR})")
    ap.add_argument("--testing",  type=Path, default=TESTING_DIR,
                    help=f"Output folder for images + JSON  "
                         f"(default: {TESTING_DIR})")
    ap.add_argument("--apikey",   default=None,
                    help="OpenAI API key (else OPENAI_API_KEY env / backend/.env)")
    ap.add_argument("--option",   choices=["1", "2"], default="1",
                    help="1=core statements only  2=core+notes")
    ap.add_argument("--model",    default="gpt-5",
                    help="OpenAI model (default: gpt-5)")
    ap.add_argument("--dpi",      type=int, default=150,
                    help="Page render DPI for captures (default 150)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Locate/capture pages but skip OpenAI calls")
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
        api_key     = _resolve_api_key(args.apikey),
        option      = args.option,
        model       = args.model,
        dry_run     = args.dry_run,
        dpi         = args.dpi,
        only        = args.only,
        skip        = args.skip,
        start_at    = args.start,
        limit       = args.limit,
        force       = args.force,
    )
