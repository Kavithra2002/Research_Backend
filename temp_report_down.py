"""
Download the latest CSE annual report PDF for listed companies in a rank range.

Uses the same public CSE APIs as `get_report.py` (trade summary + financials).
Companies are ordered by listed name (case-insensitive), similar to `--all` in
`get_report.py`. Voting and non-voting lines that share the same company name
are collapsed to one row (primary symbol sorts first).

Default: ranks 8 through 50 (folders `company8` … `company50` under
`reports/<companyN>/Annual/`), for when `company1` … `company7` already exist.

Run from any directory:
  python temp_report_down.py
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

from get_report import (
    cdn_url,
    download_pdf,
    fetch_financials,
    fetch_trade_summary,
    normalize_cdn_path,
    pick_annual_last_n_years,
    report_year,
)


def _listed_rows_sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per listing symbol, sorted by name; then one row per company name (drops e.g. X vs N duplicates)."""
    seen_sym: set[str] = set()
    by_sym: list[dict[str, Any]] = []
    for r in rows:
        sym = str(r.get("symbol") or "")
        name = str(r.get("name") or "").strip()
        if not sym or not name or sym in seen_sym:
            continue
        seen_sym.add(sym)
        by_sym.append(r)
    by_sym.sort(
        key=lambda x: (str(x.get("name") or "").casefold(), str(x.get("symbol") or ""))
    )
    out: list[dict[str, Any]] = []
    prev_name_cf = ""
    for r in by_sym:
        ncf = str(r.get("name") or "").casefold()
        if ncf == prev_name_cf:
            continue
        prev_name_cf = ncf
        out.append(r)
    return out


def _download_one_annual(
    entry: dict[str, Any], dest: Path, pause_s: float, dry_run: bool
) -> bool:
    y = report_year(entry) or "unknown_year"
    path = entry.get("path")
    if not path:
        print(f"    [skip] no path in entry id={entry.get('id')}", file=sys.stderr)
        return False

    url_primary = None
    try:
        url_primary = cdn_url(str(path))
    except ValueError:
        pass
    alt = normalize_cdn_path(entry.get("path2")) if entry.get("path2") else None
    url_alt = None
    if alt:
        try:
            url_alt = cdn_url(alt)
        except ValueError:
            url_alt = None

    rid = entry.get("id", "x")
    fname = f"{rid}_{y}.pdf"
    dest_pdf = dest / fname

    if dest_pdf.exists() and dest_pdf.stat().st_size > 1024:
        print(f"    exists {dest_pdf}")
        return True

    if dry_run:
        print(f"    would download {fname} <- {url_primary or url_alt}")
        return True

    dest.mkdir(parents=True, exist_ok=True)
    for url in (url_primary, url_alt):
        if not url:
            continue
        try:
            print(f"    downloading {fname} ...")
            download_pdf(url, dest_pdf)
            if pause_s > 0:
                time.sleep(pause_s)
            return True
        except urllib.error.HTTPError as ex:
            print(f"    http {ex.code} for {url}", file=sys.stderr)
        except OSError as ex:
            print(f"    error {ex!r} for {url}", file=sys.stderr)
        except Exception as ex:
            print(f"    error {ex!r} for {url}", file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    default_reports = here / "reports"

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--reports-dir",
        type=Path,
        default=default_reports,
        help=f"Root folder for companyN/Annual (default: {default_reports})",
    )
    p.add_argument(
        "--rank-start",
        type=int,
        default=8,
        help="First company by sorted CSE list (1-based). Default 8.",
    )
    p.add_argument(
        "--rank-end",
        type=int,
        default=50,
        help="Last company by sorted CSE list (1-based). Default 50.",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=0.35,
        help="Seconds to sleep after each successful PDF download.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve targets only; do not write PDFs.",
    )
    args = p.parse_args(argv)

    if args.rank_start < 1 or args.rank_end < args.rank_start:
        print("Invalid --rank-start / --rank-end.", file=sys.stderr)
        return 2

    args.reports_dir = args.reports_dir.resolve()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching CSE trade summary...")
    rows = fetch_trade_summary()
    if not rows:
        print("Empty trade summary.", file=sys.stderr)
        return 1

    ranked = _listed_rows_sorted(rows)
    n = len(ranked)
    if args.rank_end > n:
        print(
            f"Warning: only {n} listed companies; clamping rank-end to {n}.",
            file=sys.stderr,
        )
    rank_end = min(args.rank_end, n)
    if args.rank_start > rank_end:
        print("rank-start is past available companies.", file=sys.stderr)
        return 1

    slice_rows = ranked[args.rank_start - 1 : rank_end]

    print(
        f"Will process CSE ranks {args.rank_start}-{rank_end} "
        f"({len(slice_rows)} companies) into {args.reports_dir}"
    )

    for offset, r in enumerate(slice_rows):
        rank = args.rank_start + offset
        sym = str(r.get("symbol") or "")
        name = str(r.get("name") or "")
        dest = args.reports_dir / f"company{rank}" / "Annual"
        print(f"[{rank}/{rank_end}] {name} ({sym})")

        fin = fetch_financials(sym)
        annual = fin.get("infoAnnualData") or []
        if not isinstance(annual, list):
            annual = []

        picked = pick_annual_last_n_years(annual, years=1)
        if not picked:
            print("    [skip] no annual report data", file=sys.stderr)
            continue

        ok = _download_one_annual(
            picked[0], dest, pause_s=max(0.0, args.pause), dry_run=args.dry_run
        )
        if not ok:
            print("    [fail] could not obtain PDF", file=sys.stderr)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
