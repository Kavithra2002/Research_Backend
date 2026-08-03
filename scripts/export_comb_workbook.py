"""
CLI wrapper for COMB workbook export (Cover + FS + Drivers + Ratios + Quarterly).

Usage:
    python export_comb_workbook.py --company-slug Commercial_Bank_of_Ceylon_PLC --output out.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generate_comb_model import export_comb_workbook


def main() -> int:
    parser = argparse.ArgumentParser(description="Export COMB pilot workbook")
    parser.add_argument("--company-slug", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--company-name", default="")
    parser.add_argument("--ticker", default="")
    parser.add_argument(
        "--years",
        default="",
        help="Comma-separated years (default: template baseline through latest stored year)",
    )
    args = parser.parse_args()

    years = (
        [int(y.strip()) for y in args.years.split(",") if y.strip()]
        if args.years.strip()
        else None
    )

    try:
        result = export_comb_workbook(
            args.company_slug.strip(),
            Path(args.output),
            company_name=args.company_name.strip() or None,
            ticker=args.ticker.strip() or None,
            years=years,
        )
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
