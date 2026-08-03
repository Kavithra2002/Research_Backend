"""
CLI wrapper for historical FS Excel export (Cover + FS, 2017-2025 actuals).

Usage:
    python export_fs_workbook.py --company-slug acl_plastics_plc --output out.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generate_comb_model import export_historical_fs_workbook


def main() -> int:
    parser = argparse.ArgumentParser(description="Export historical FS workbook")
    parser.add_argument("--company-slug", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--company-name", default="")
    parser.add_argument("--ticker", default="")
    args = parser.parse_args()

    try:
        result = export_historical_fs_workbook(
            args.company_slug.strip(),
            Path(args.output),
            company_name=args.company_name.strip() or None,
            ticker=args.ticker.strip() or None,
        )
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
