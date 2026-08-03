"""
Return FS, Drivers, Ratios, or Quarterly preview JSON for the DB page.

Usage:
    python preview_db_workbook.py --view fs --company-slug Commercial_Bank_of_Ceylon_PLC
    python preview_db_workbook.py --view drivers --company-slug Commercial_Bank_of_Ceylon_PLC
    python preview_db_workbook.py --view ratios --company-slug Commercial_Bank_of_Ceylon_PLC
    python preview_db_workbook.py --view quarterly --company-slug Commercial_Bank_of_Ceylon_PLC
"""
from __future__ import annotations

import argparse
import json
import sys

from generate_comb_model import (
    COMMERCIAL_BANK_SLUG,
    build_drivers_preview_data,
    build_fs_preview_data,
    build_notes_preview_data,
    build_quarterly_preview_data,
    build_ratios_preview_data,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview COMB workbook grid JSON")
    parser.add_argument(
        "--view",
        choices=["fs", "drivers", "ratios", "quarterly", "notes"],
        required=True,
    )
    parser.add_argument("--company-slug", default="")
    parser.add_argument(
        "--years",
        default="",
        help="Comma-separated years (default: template baseline through latest stored year)",
    )
    args = parser.parse_args()

    try:
        slug = args.company_slug.strip()
        years = (
            [int(y.strip()) for y in args.years.split(",") if y.strip()]
            if args.years.strip()
            else None
        )

        if args.view == "quarterly":
            if slug and slug != COMMERCIAL_BANK_SLUG:
                raise ValueError(
                    "Quarterly view is only available for Commercial Bank of Ceylon PLC."
                )
            payload = build_quarterly_preview_data(
                slug or COMMERCIAL_BANK_SLUG,
                years=years,
            )
        elif args.view == "drivers":
            if not slug:
                raise ValueError("company-slug is required for drivers view")
            payload = build_drivers_preview_data(slug, years=years)
        elif args.view == "ratios":
            if not slug:
                raise ValueError("company-slug is required for ratios view")
            payload = build_ratios_preview_data(slug, years=years)
        elif args.view == "notes":
            if not slug:
                raise ValueError("company-slug is required for notes view")
            payload = build_notes_preview_data(slug, years=years)
        else:
            if not slug:
                raise ValueError("company-slug is required for fs view")
            payload = build_fs_preview_data(slug, years=years)
        print(json.dumps(payload), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
