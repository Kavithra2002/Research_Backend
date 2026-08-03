"""
Data_retrive.py
===============
End-to-end driver that walks through every company report PDF, captures the
relevant financial-statement page images, ships them to OpenAI GPT-4o for
verbatim table transcription, and saves both the images and the resulting
JSON under a self-contained per-company folder inside ``testing/``.

Output layout
-------------
    testing/
    ├── <company>/
    │   ├── <company>_pages.json          (step-1 manifest)
    │   ├── captures/
    │   │   ├── income_statement/page_0170.png
    │   │   ├── oci/page_0171.png
    │   │   └── ...                        (one folder per statement)
    │   ├── <company>_results.json         (OpenAI JSON output)
    │   ├── <company>_results.html         (HTML mirror of the JSON)
    │   ├── <company>_preview.html         (raw captured images preview)
    │   └── extraction_meta.json
    └── ...

The script keeps going company-by-company until the source-reports list is
exhausted.  Already-finished companies are skipped on re-run so the pipeline
is fully resumable.

Usage
-----
    # Process every company found in ./reports
    python Data_retrive.py --apikey sk-...

    # Cheaper / faster run
    python Data_retrive.py --model gpt-4o-mini

    # Process only a specific company sub-folder
    python Data_retrive.py --only company1

    # Re-run a company even if results already exist
    python Data_retrive.py --only company1 --force

    # Core statements only (skip Notes)
    python Data_retrive.py --option 1

    # Core + Notes
    python Data_retrive.py --option 2

    # Dry run – render images, don't call OpenAI
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

# Re-use the existing step1 / step2 / step3 modules so we share the heading
# detection logic, image rendering pipeline, and OpenAI prompts.
import step1_find_pages as step1
import step2_capture_pages as step2
import step3_send_to_openai as step3
import extraction_validation as val


# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).resolve().parent
BACKEND_DIR  = SCRIPT_DIR.parent
REPORTS_DIR  = BACKEND_DIR / "reports"
TESTING_DIR  = BACKEND_DIR / "testing"
BACKEND_ENV  = BACKEND_DIR / ".env"

MAX_VALIDATION_ROUNDS = 3

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


def _save_validation_meta(company_out: Path, report: val.ValidationResult) -> None:
    meta_path = company_out / "validation_report.json"
    meta_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _run_validation_and_fill_gaps(
    *,
    company: str,
    pdf_path: Path,
    company_out: Path,
    manifest_path: Path,
    captures_dir: Path,
    api_key: str | None,
    option: str,
    model: str,
    dry_run: bool,
    dpi: int,
    company_slug: str | None = None,
    max_rounds: int = MAX_VALIDATION_ROUNDS,
    comb_mode: bool = False,
) -> val.ValidationResult:
    """
    After the initial step1→3 pass, validate completeness and re-scan /
    re-capture / re-extract any missing or failed statements.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = _load_results_json(company_out, company)

    print("  [validate] required annual statements:")
    for item in val.annual_required_checklist():
        print(f"    · {val.ANNUAL_DISPLAY_NAMES.get(item, item)}")

    last_report = val.validate_annual_results(
        results, manifest, option=option, comb_mode=comb_mode
    )

    for round_num in range(1, max_rounds + 1):
        gaps = last_report.all_gaps()
        if not gaps or dry_run:
            break

        print(
            f"\n  → VALIDATION round {round_num}/{max_rounds} — "
            f"{len(gaps)} gap(s): {', '.join(gaps)}"
        )

        missing_scan = list(last_report.missing_manifest)
        failed_keys = list(last_report.failed_extraction)
        keys_to_fix = val.annual_keys_to_fix(last_report)
        if not keys_to_fix:
            break

        # Re-scan PDF for statements step-1 missed.
        if missing_scan:
            try:
                import pdfplumber
                with pdfplumber.open(str(pdf_path)) as pdf:
                    updated = step1.rescan_missing_keys(
                        pdf, missing_scan, manifest.get("statements") or {},
                    )
                manifest = step1.merge_manifest_statements(manifest, updated)
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                newly_found = [k for k in missing_scan if k in updated]
                if newly_found:
                    print(f"  [validate] re-scan found: {', '.join(newly_found)}")
            except Exception as e:
                print(f"  [validate] re-scan failed: {e}")

        # Capture images for any keys we now have in the manifest.
        capture_keys = [
            k for k in keys_to_fix if k in (manifest.get("statements") or {})
        ]
        if capture_keys:
            try:
                step2.run(
                    manifest_path=manifest_path,
                    out_dir=captures_dir,
                    dpi=dpi,
                    only_keys=capture_keys,
                )
            except Exception as e:
                print(f"  [validate] re-capture failed: {e}")

        # Re-extract failed / newly captured statements via OpenAI.
        if not dry_run and api_key and capture_keys:
            try:
                results = step3.run_statements(
                    manifest_path=manifest_path,
                    captures_dir=captures_dir,
                    api_key=api_key,
                    keys=capture_keys,
                    model=model,
                    dry_run=False,
                    out_dir=company_out,
                    existing_results=results,
                )
            except Exception as e:
                print(f"  [validate] re-extract failed: {e}")

        last_report = val.validate_annual_results(
        results, manifest, option=option, comb_mode=comb_mode
    )

    _save_validation_meta(company_out, last_report)
    if last_report.ok:
        print("  [validate] all expected statements captured")
    else:
        remaining = last_report.all_gaps()
        print(
            f"  [validate] still missing after {max_rounds} round(s): "
            f"{', '.join(remaining)}"
        )
    return last_report


# ─────────────────────────────────────────────────────────────────────────────
# Per-company pipeline
# ─────────────────────────────────────────────────────────────────────────────

def process_company(
    company:     str,
    pdf_path:    Path,
    testing_dir: Path,
    api_key:     str | None,
    option:      str,
    model:       str,
    dry_run:     bool,
    dpi:         int,
    force:       bool,
    comb_mode:   bool = False,
) -> dict:
    """Run STEP-1 → STEP-2 → STEP-3 for a single company.

    All artefacts are written under ``testing/<company>/``.
    """
    company_out = testing_dir / company
    company_out.mkdir(parents=True, exist_ok=True)

    captures_dir  = company_out / "captures"
    manifest_path = company_out / f"{company}_pages.json"

    print(f"\n{'#'*70}")
    print(f"#  COMPANY : {company}")
    print(f"#  PDF     : {pdf_path}")
    print(f"#  OUTPUT  : {company_out}")
    print(f"{'#'*70}")

    if not force and _results_already_done(company_out, company):
        print(f"  [skip] {company}: results already exist — use --force to rebuild")
        return {"company": company, "status": "skipped_existing"}

    # ── STEP 1 : detect page numbers ────────────────────────────────────────
    print(f"\n  → STEP 1  detect financial-statement pages")
    try:
        manifest = step1.run(
            pdf_path = pdf_path,
            company  = company,
            out_path = manifest_path,
            verbose  = True,
        )
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step1_failed", "error": str(e)}

    if not manifest or not manifest.get("statements"):
        return {"company": company, "status": "no_statements_found"}

    # ── STEP 2 : render captured images ─────────────────────────────────────
    print(f"\n  → STEP 2  render statement page images  (dpi={dpi})")
    try:
        step2.run(
            manifest_path = manifest_path,
            out_dir       = captures_dir,
            dpi           = dpi,
        )
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step2_failed", "error": str(e)}

    # Move the auto-created preview into the company folder so all artefacts
    # live in one place.
    preview_src = captures_dir / f"{company}_preview.html"
    if preview_src.exists():
        try:
            (company_out / f"{company}_preview.html").write_bytes(
                preview_src.read_bytes()
            )
        except Exception:
            pass

    # ── STEP 3 : send images to OpenAI ──────────────────────────────────────
    print(f"\n  → STEP 3  call OpenAI ({model})  option={option}  dry_run={dry_run}")
    try:
        step3.run(
            manifest_path = manifest_path,
            captures_dir  = captures_dir,
            api_key       = api_key,
            option        = option,
            model         = model,
            dry_run       = dry_run,
            out_dir       = company_out,
        )
    except SystemExit:
        # step3 calls sys.exit on missing apikey — convert to soft error
        return {"company": company, "status": "step3_failed",
                "error": "missing API key"}
    except Exception as e:
        traceback.print_exc()
        return {"company": company, "status": "step3_failed", "error": str(e)}

    if dry_run:
        return {"company": company, "status": "ok"}

    validation = _run_validation_and_fill_gaps(
        company=company,
        pdf_path=pdf_path,
        company_out=company_out,
        manifest_path=manifest_path,
        captures_dir=captures_dir,
        api_key=api_key,
        option=option,
        model=model,
        dry_run=dry_run,
        dpi=dpi,
        company_slug=company,
        comb_mode=comb_mode,
    )

    if not validation.ok and not comb_mode:
        return {
            "company": company,
            "status": "missing_statements",
            "validation": validation.to_dict(),
            "gaps": validation.all_gaps(),
        }

    status = "ok" if validation.ok or comb_mode else "missing_statements"
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

    if not dry_run and not api_key:
        print("\nERROR: OpenAI API key not found.")
        print("       Pass --apikey, set OPENAI_API_KEY, or add it to .env")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"  DATA RETRIEVE — financial statement pipeline")
    print(f"  Reports    : {reports_dir}")
    print(f"  Output     : {testing_dir}")
    print(f"  Companies  : {len(companies)}")
    print(f"  Option     : {option}  ({'Core only' if option=='1' else 'Core + Notes'})")
    print(f"  Model      : {model}")
    print(f"  DPI        : {dpi}")
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
        description="Per-company driver: capture statement images, send to OpenAI, "
                    "save images + JSON under testing/<company>/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--reports",  type=Path, default=REPORTS_DIR,
                    help=f"Folder containing per-company report sub-folders  "
                         f"(default: {REPORTS_DIR})")
    ap.add_argument("--testing",  type=Path, default=TESTING_DIR,
                    help=f"Output folder for images + JSON  "
                         f"(default: {TESTING_DIR})")
    ap.add_argument("--apikey",   default=None,
                    help="OpenAI API key (else OPENAI_API_KEY env or .env)")
    ap.add_argument("--option",   choices=["1", "2"], default="1",
                    help="1 = core statements  2 = core + Notes  (default: 1)")
    ap.add_argument("--model",    default="gpt-4o",
                    help="OpenAI model name  (default: gpt-4o)")
    ap.add_argument("--dpi",      type=int, default=150,
                    help="Image render DPI  (default: 150)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Render images but DON'T call OpenAI")
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

    api_key = _resolve_api_key(args.apikey)

    run(
        reports_dir = args.reports.resolve(),
        testing_dir = args.testing.resolve(),
        api_key     = api_key,
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
