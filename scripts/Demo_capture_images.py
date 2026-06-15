"""
Demo_capture_images.py
======================
Capture-ONLY pipeline for the demo dataset.

For every **annual** report PDF under ``backend/Demo_Data/<Company>/Annual/
<Annual Report YYYY>/*.pdf`` this script renders the relevant financial
statement page images (STEP 1 → STEP 2 of the existing pipeline) and saves
them, mirroring the Demo_Data layout, under::

    backend/Demo_Data_captures/
    └── <Company>/
        └── Annual/
            └── <YYYY>/
                ├── income_statement/page_0123.png
                ├── sofp/page_0170.png
                └── ...                       (one folder per statement key)

It does NOT call OpenAI — the table data already lives in MongoDB, so only the
page images are missing.  The statement-folder keys match the ``statement_key``
values stored in MongoDB so the comparison page can line each captured page up
with its extracted table.

Quarterly reports are intentionally skipped (not needed for the demo).

Usage
-----
    python Demo_capture_images.py                 # all 5 demo companies
    python Demo_capture_images.py --only "Ambeon Holdings PLC"
    python Demo_capture_images.py --dpi 150 --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import step1_find_pages as step1
import step2_capture_pages as step2


SCRIPT_DIR   = Path(__file__).resolve().parent
BACKEND_DIR  = SCRIPT_DIR.parent
DEMO_DATA    = BACKEND_DIR / "Demo_Data"
CAPTURES_OUT = BACKEND_DIR / "Demo_Data_captures"

PDF_EXT = (".pdf", ".PDF")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _year_from_folder(folder_name: str) -> int | None:
    """'Annual Report 2025' -> 2025  (takes the LAST 4-digit year present)."""
    years = [int(m.group(0)) for m in _YEAR_RE.finditer(folder_name)]
    years = [y for y in years if 1990 <= y <= 2100]
    return max(years) if years else None


def _pick_primary_pdf(report_dir: Path) -> Path | None:
    """Choose the main report PDF in a report folder.

    Preference:
      1. a file whose name contains 'annual report'
      2. otherwise the largest PDF
    """
    pdfs = [p for p in report_dir.iterdir() if p.is_file() and p.suffix in PDF_EXT]
    if not pdfs:
        return None
    named = [p for p in pdfs if "annual report" in p.name.lower()]
    pool = named or pdfs
    return max(pool, key=lambda p: p.stat().st_size)


def _company_done(company_out: Path, year: int) -> bool:
    year_dir = company_out / "Annual" / str(year)
    if not year_dir.is_dir():
        return False
    # Done if at least one statement folder holds a PNG.
    for stmt_dir in year_dir.iterdir():
        if stmt_dir.is_dir() and any(stmt_dir.glob("*.png")):
            return True
    return False


def capture_one_report(
    *,
    company: str,
    year: int,
    pdf_path: Path,
    out_root: Path,
    dpi: int,
) -> dict:
    """Run STEP 1 + STEP 2 for one annual report and write images under
    ``<out_root>/<company>/Annual/<year>/<statement_key>/``.
    """
    year_dir = out_root / company / "Annual" / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ambeon-cap-") as tmp_str:
        tmp = Path(tmp_str)
        manifest_path = tmp / "pages.json"

        # STEP 1 — detect statement page ranges.
        manifest = step1.run(
            pdf_path=pdf_path,
            company=f"{company} {year}",
            out_path=manifest_path,
            verbose=False,
        )
        if not manifest or not manifest.get("statements"):
            return {"status": "no_statements", "images": 0}

        # STEP 2 — render the confirmed statement pages straight into the
        # final year directory (one subfolder per statement_key).
        res = step2.run(
            manifest_path=manifest_path,
            out_dir=year_dir,
            dpi=dpi,
        )

    img_map = (res or {}).get("img_map", {})
    total = sum(len(v) for v in img_map.values())

    # Drop the auto-generated preview HTML — we only want PNG folders here.
    for junk in year_dir.glob("*_preview.html"):
        try:
            junk.unlink()
        except Exception:
            pass
    # Remove any empty statement folders so listings stay clean.
    for sub in year_dir.iterdir():
        if sub.is_dir() and not any(sub.glob("*.png")):
            try:
                sub.rmdir()
            except Exception:
                pass

    return {"status": "ok", "images": total,
            "statements": {k: len(v) for k, v in img_map.items() if v}}


def run(
    *,
    demo_root: Path,
    out_root: Path,
    only: str | None,
    dpi: int,
    force: bool,
) -> None:
    if not demo_root.is_dir():
        print(f"ERROR: Demo_Data folder not found: {demo_root}")
        sys.exit(1)

    companies = sorted(
        p for p in demo_root.iterdir()
        if p.is_dir() and (p / "Annual").is_dir()
    )
    if only:
        want = only.strip().lower()
        companies = [c for c in companies if c.name.lower() == want]
        if not companies:
            print(f"ERROR: company '{only}' not found under {demo_root}")
            sys.exit(1)

    out_root.mkdir(parents=True, exist_ok=True)

    print(f"{'='*70}")
    print(f"  DEMO CAPTURE — annual statement page images (no OpenAI)")
    print(f"  Source : {demo_root}")
    print(f"  Output : {out_root}")
    print(f"  DPI    : {dpi}   Force: {force}")
    print(f"  Companies: {len(companies)}")
    print(f"{'='*70}")

    grand_total = 0
    for company_dir in companies:
        company = company_dir.name
        annual_dir = company_dir / "Annual"
        report_dirs = sorted(
            p for p in annual_dir.iterdir() if p.is_dir()
        )
        print(f"\n#### {company}  ({len(report_dirs)} annual report folders)")

        for report_dir in report_dirs:
            year = _year_from_folder(report_dir.name)
            if year is None:
                print(f"   [skip] {report_dir.name}: no year in folder name")
                continue

            if not force and _company_done(out_root / company, year):
                print(f"   [skip] {year}: captures already exist")
                continue

            pdf = _pick_primary_pdf(report_dir)
            if not pdf:
                print(f"   [skip] {year}: no PDF in {report_dir.name}")
                continue

            print(f"   - {year}: {pdf.name} ...", end="", flush=True)
            try:
                res = capture_one_report(
                    company=company, year=year, pdf_path=pdf,
                    out_root=out_root, dpi=dpi,
                )
            except Exception as ex:
                traceback.print_exc()
                print(f" CRASHED ({ex})")
                continue

            if res["status"] == "ok":
                grand_total += res["images"]
                stmt_str = ", ".join(
                    f"{k}:{n}" for k, n in (res.get("statements") or {}).items()
                )
                print(f" {res['images']} image(s)  [{stmt_str}]")
            else:
                print(f" {res['status']}")

    print(f"\n{'='*70}")
    print(f"  DONE — {grand_total} image(s) captured into {out_root.name}/")
    print(f"{'='*70}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEMO_DATA,
                    help=f"Demo_Data root (default: {DEMO_DATA})")
    ap.add_argument("--out", type=Path, default=CAPTURES_OUT,
                    help=f"Output root (default: {CAPTURES_OUT})")
    ap.add_argument("--only", default=None,
                    help="Process only this company folder (exact display name).")
    ap.add_argument("--dpi", type=int, default=150, help="Render DPI (default 150).")
    ap.add_argument("--force", action="store_true",
                    help="Re-render even if captures already exist.")
    args = ap.parse_args()

    run(
        demo_root=args.source.resolve(),
        out_root=args.out.resolve(),
        only=args.only,
        dpi=args.dpi,
        force=args.force,
    )
