#!/usr/bin/env python3
"""
Universal Statement of Financial Position (SoFP) extractor → JSON.

Pipeline (no per-company manual rules):
1. Locate SoFP pages (TOC + heading + footer mapping, same as extract_income).
2. For each candidate PDF page offset, collect tables from many pdfplumber strategies
   (lines/text + extra tolerances).
3. Stitch page+1 continuations when the table repeats headers.
4. Repair split amount cells; trim footers.
5. Score each candidate with structure checks + balance-sheet validation
   (Total assets vs Total equity and liabilities per column).
6. Emit the best candidate to JSON with extraction_meta (validation_score, etc.).

Usage:
  python extract_sofp_universal.py path/to/report.pdf --company company6 -o json_logs/company6_SoFP.json

Limitation: vector PDFs only (pdfplumber). Scanned image PDFs need OCR (not included).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from extract_income import extract_sofp_from_pdf_to_json


def main() -> None:
    p = argparse.ArgumentParser(
        description="Extract Statement of Financial Position to JSON (universal pipeline)."
    )
    p.add_argument("pdf", type=Path, help="Path to annual report PDF")
    p.add_argument(
        "--company",
        "-c",
        required=True,
        help='Company key stored in JSON (e.g. "company6")',
    )
    p.add_argument(
        "--out",
        "-o",
        type=Path,
        default=None,
        help="Output JSON path (default: json_logs/<company>_SoFP.json next to this script)",
    )
    args = p.parse_args()
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent
    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file():
        raise SystemExit(f"PDF not found: {pdf}")
    out = args.out
    if out is None:
        out = backend_dir / "json_logs" / f"{args.company}_SoFP.json"
    else:
        out = out.expanduser().resolve()

    extract_sofp_from_pdf_to_json(pdf, args.company, out)


if __name__ == "__main__":
    main()
