"""Align-only (no re-extract) for years already in Mongo. Excel wins."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _extract_align_years import align_year_to_excel, verify_year


def main(argv: list[str] | None = None) -> int:
    years = [int(x) for x in (argv or ["2019", "2020", "2021"])]
    results = []
    for year in years:
        print(f"\n=== Align {year} to Excel ===", flush=True)
        aligned = align_year_to_excel(year)
        aligned2 = align_year_to_excel(year)
        verified = verify_year(year)
        if verified["mismatches"]:
            print(
                f"  [{year}] {verified['mismatches']} still mismatch — re-aligning…",
                flush=True,
            )
            for row in verified["mismatch_rows"]:
                print(
                    f"    RETRY {row['label']!r}: excel={row['excel']} db={row['db']}",
                    flush=True,
                )
            align_year_to_excel(year)
            verified = verify_year(year)
        row = {
            **aligned,
            "aligned_pass2": aligned2.get("aligned_from_excel"),
            "verify": verified,
        }
        results.append(row)
        print(json.dumps(row, indent=2), flush=True)

    out = Path("_align_2019_2021_report.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    bad = [r for r in results if r["verify"]["mismatches"] > 0]
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
