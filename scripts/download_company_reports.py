"""
download_company_reports.py
===========================
For every existing company folder under ``reports/``, ensure both an
``Annual/`` and a ``Quarterly/`` sub-folder exists, then download the most
recent *N* years of annual reports and quarterly (interim) reports from the
public CSE financials API.

Folder layout produced
----------------------
    reports/
    └── ACL_PLASTICS_PLC/
        ├── Annual/
        │   ├── 643_1756350200465.pdf      (last 10 distinct fiscal years)
        │   └── ...
        └── Quarterly/
            ├── 643_1770974381263.pdf      (~40 quarters covering last 10 yrs)
            └── ...

Folder name -> CSE name resolution: underscores become spaces, then a fuzzy
match is run against the CSE trade summary (re-using ``get_report.py``'s
``resolve_symbol``). A manual override map at the top of this file handles
known mis-spellings (e.g. ``JANASHAKTHI FINACE`` -> ``JANASHAKTHI FINANCE PLC``).

Already-downloaded PDFs (matched by filename) are skipped, so the script is
fully resumable.

Usage
-----
    # Process every folder in reports/, last 10 years of each kind.
    python download_company_reports.py

    # Dry run — print what would happen, write nothing.
    python download_company_reports.py --dry-run

    # Process a single company folder.
    python download_company_reports.py --only ACL_PLASTICS_PLC

    # Only annual / only quarterly.
    python download_company_reports.py --kind annual
    python download_company_reports.py --kind quarterly

    # Different window.
    python download_company_reports.py --years 5
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from get_report import (
    _entry_sort_ms,
    cdn_url,
    download_pdf,
    fetch_financials,
    fetch_trade_summary,
    normalize_cdn_path,
    pick_annual_last_n_years,
    report_year,
    resolve_symbol,
)


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
REPORTS_DIR = BACKEND_DIR / "reports"

# Manual folder -> CSE company name overrides for cases where the folder name
# is mis-spelled or otherwise ambiguous. Keys are case-insensitive.
MANUAL_NAME_OVERRIDES: dict[str, str] = {
    "JANASHAKTHI FINACE": "JANASHAKTHI FINANCE PLC",
    "a. JANASHAKTHI FINACE": "JANASHAKTHI FINANCE PLC",
}


# ─────────────────────────────────────────────────────────────────────────────
# Quarterly picker (mirrors pick_annual_last_n_years from get_report.py)
# ─────────────────────────────────────────────────────────────────────────────

def _year_month_key(entry: dict[str, Any]) -> tuple[int, int] | None:
    """Return (year, month) of the quarter the entry represents, or None."""
    ms = entry.get("manualDate")
    if ms is not None:
        try:
            v = int(ms)
            if v > 10_000_000_000:
                v //= 1000
            dt = datetime.fromtimestamp(v, tz=timezone.utc)
            return (dt.year, dt.month)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    y = report_year(entry)
    return (y, 0) if y else None


def pick_quarterly_last_n_years(
    info_quarterly: Iterable[dict[str, Any]],
    years: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Most-recent quarterly entry per (year, month), within the last ``years``
    fiscal years (counted from the newest report year on file)."""
    now = now or datetime.now(tz=timezone.utc)

    bucketed: dict[tuple[int, int], dict[str, Any]] = {}
    for e in info_quarterly:
        if not isinstance(e, dict):
            continue
        key = _year_month_key(e)
        if key is None:
            continue
        y = key[0]
        if y > now.year + 1:
            continue
        prev = bucketed.get(key)
        if prev is None or _entry_sort_ms(e) >= _entry_sort_ms(prev):
            bucketed[key] = e

    if not bucketed:
        return []

    latest_year = max(k[0] for k in bucketed.keys())
    cutoff = latest_year - max(1, years) + 1

    keep = [e for k, e in bucketed.items() if k[0] >= cutoff]
    keep.sort(key=_entry_sort_ms, reverse=True)
    return keep


# ─────────────────────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────────────────────

def _entry_basename(entry: dict[str, Any]) -> str:
    """Pick the on-disk filename. Use the CDN basename so it matches PDFs
    that were copied straight from the CSE CDN (e.g. ``643_1756350200465.pdf``)."""
    rid = entry.get("id", "x")
    for key in ("path", "path2"):
        p = entry.get(key)
        if not p:
            continue
        base = str(p).rstrip("/").rsplit("/", 1)[-1].strip()
        if not base:
            continue
        if base.endswith("."):
            base = base[:-1]
        if not base.lower().endswith(".pdf"):
            base = f"{base}.pdf" if base else ""
        if base:
            return base
    y = report_year(entry) or "unknown"
    return f"{rid}_{y}.pdf"


def _download_entry(
    entry: dict[str, Any],
    dest_dir: Path,
    pause_s: float,
    dry_run: bool,
) -> str:
    """Download a single CSE financials entry. Returns one of:
    ``"saved" | "exists" | "skipped" | "failed" | "would-download"``.
    """
    path = entry.get("path")
    if not path:
        print(f"    [skip] no path in entry id={entry.get('id')}", file=sys.stderr)
        return "skipped"

    url_primary: str | None = None
    try:
        url_primary = cdn_url(str(path))
    except ValueError:
        pass

    alt = normalize_cdn_path(entry.get("path2")) if entry.get("path2") else None
    url_alt: str | None = None
    if alt:
        try:
            url_alt = cdn_url(alt)
        except ValueError:
            url_alt = None

    fname = _entry_basename(entry)
    dest_pdf = dest_dir / fname

    if dest_pdf.exists() and dest_pdf.stat().st_size > 1024:
        print(f"    exists  {fname}")
        return "exists"

    if dry_run:
        print(f"    would   {fname}  <-  {url_primary or url_alt}")
        return "would-download"

    dest_dir.mkdir(parents=True, exist_ok=True)
    for url in (url_primary, url_alt):
        if not url:
            continue
        try:
            print(f"    saving  {fname} ...")
            download_pdf(url, dest_pdf)
            if pause_s > 0:
                time.sleep(pause_s)
            return "saved"
        except urllib.error.HTTPError as ex:
            print(f"      http {ex.code} for {url}", file=sys.stderr)
        except OSError as ex:
            print(f"      error {ex!r} for {url}", file=sys.stderr)
        except Exception as ex:  # noqa: BLE001 — surface every failure mode
            print(f"      error {ex!r} for {url}", file=sys.stderr)

    return "failed"


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

def folder_to_query(folder_name: str) -> str:
    """Turn a folder like 'COLOMBO_FORT_LAND_&_BUILDING_PLC' into a search query."""
    override = MANUAL_NAME_OVERRIDES.get(folder_name) or MANUAL_NAME_OVERRIDES.get(
        folder_name.upper()
    )
    if override:
        return override
    return folder_name.replace("_", " ").strip()


def resolve_folder(
    folder_name: str,
    rows: list[dict[str, Any]],
    min_score: float,
) -> tuple[str, str, float]:
    query = folder_to_query(folder_name)
    sym, name, score = resolve_symbol(query, rows)
    if not sym or score < min_score:
        return "", name, score
    return sym, name, score


# ─────────────────────────────────────────────────────────────────────────────
# Per-folder driver
# ─────────────────────────────────────────────────────────────────────────────

def process_folder(
    folder: Path,
    rows: list[dict[str, Any]],
    years: int,
    kind: str,
    min_score: float,
    pause_s: float,
    dry_run: bool,
) -> dict[str, Any]:
    print(f"\n=== {folder.name} ===")
    sym, official, score = resolve_folder(folder.name, rows, min_score)
    if not sym:
        print(
            f"  [skip] could not resolve a CSE symbol "
            f"(best='{official}', score={score:.2f})",
            file=sys.stderr,
        )
        return {"folder": folder.name, "status": "unresolved", "score": score}

    print(f"  match  : {official}  ({sym}, score={score:.2f})")

    annual_dir = folder / "Annual"
    quarterly_dir = folder / "Quarterly"
    annual_dir.mkdir(parents=True, exist_ok=True)
    quarterly_dir.mkdir(parents=True, exist_ok=True)

    fin = fetch_financials(sym)
    if not isinstance(fin, dict):
        fin = {}

    summary: dict[str, Any] = {
        "folder": folder.name,
        "symbol": sym,
        "official_name": official,
        "score": round(score, 3),
        "annual": {"picked": 0, "saved": 0, "exists": 0, "failed": 0, "would": 0},
        "quarterly": {"picked": 0, "saved": 0, "exists": 0, "failed": 0, "would": 0},
    }

    def _tally(bucket: dict[str, int], outcome: str) -> None:
        if outcome == "saved":
            bucket["saved"] += 1
        elif outcome == "exists":
            bucket["exists"] += 1
        elif outcome == "would-download":
            bucket["would"] += 1
        elif outcome == "failed":
            bucket["failed"] += 1

    if kind in ("annual", "both"):
        annual = fin.get("infoAnnualData") or []
        if not isinstance(annual, list):
            annual = []
        picked_a = pick_annual_last_n_years(annual, years=years)
        summary["annual"]["picked"] = len(picked_a)
        print(f"  annual : {len(picked_a)} report(s) targeted (last {years} years)")
        for e in picked_a:
            outcome = _download_entry(e, annual_dir, pause_s, dry_run)
            _tally(summary["annual"], outcome)

    if kind in ("quarterly", "both"):
        quarterly = fin.get("infoQuarterlyData") or []
        if not isinstance(quarterly, list):
            quarterly = []
        picked_q = pick_quarterly_last_n_years(quarterly, years=years)
        summary["quarterly"]["picked"] = len(picked_q)
        print(f"  quart. : {len(picked_q)} report(s) targeted (last {years} years)")
        for e in picked_q:
            outcome = _download_entry(e, quarterly_dir, pause_s, dry_run)
            _tally(summary["quarterly"], outcome)

    summary["status"] = "ok"
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _list_folders(reports_dir: Path) -> list[Path]:
    if not reports_dir.exists():
        return []
    return sorted(
        (p for p in reports_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name.casefold(),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help=f"Root containing per-company folders (default: {REPORTS_DIR}).",
    )
    ap.add_argument(
        "--years",
        type=int,
        default=10,
        help="Window for both kinds of report (default: 10).",
    )
    ap.add_argument(
        "--kind",
        choices=("both", "annual", "quarterly"),
        default="both",
        help="Which kind of report to download (default: both).",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="Process only this folder name (may be repeated).",
    )
    ap.add_argument(
        "--skip",
        action="append",
        default=[],
        help="Skip a folder name (may be repeated).",
    )
    ap.add_argument(
        "--min-score",
        type=float,
        default=0.78,
        help="Minimum fuzzy-match score 0-1 for name resolution (default: 0.78).",
    )
    ap.add_argument(
        "--pause",
        type=float,
        default=0.35,
        help="Seconds to sleep after each successful PDF download (default: 0.35).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve symbols and list downloads without writing any PDFs.",
    )
    args = ap.parse_args(argv)

    reports_dir: Path = args.reports_dir.resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)

    folders = _list_folders(reports_dir)
    if args.only:
        wanted = {s.casefold() for s in args.only}
        folders = [f for f in folders if f.name.casefold() in wanted]
    if args.skip:
        unwanted = {s.casefold() for s in args.skip}
        folders = [f for f in folders if f.name.casefold() not in unwanted]

    if not folders:
        print(f"No company folders found under {reports_dir}", file=sys.stderr)
        return 1

    print(f"Fetching CSE trade summary ...")
    rows = fetch_trade_summary()
    if not rows:
        print("Empty CSE trade summary — aborting.", file=sys.stderr)
        return 1

    print(
        f"Will process {len(folders)} folder(s) under {reports_dir}\n"
        f"  years   : {args.years}\n"
        f"  kind    : {args.kind}\n"
        f"  dry-run : {args.dry_run}"
    )

    results: list[dict[str, Any]] = []
    t0 = datetime.now()
    for i, folder in enumerate(folders, 1):
        print(f"\n[{i}/{len(folders)}]")
        try:
            res = process_folder(
                folder=folder,
                rows=rows,
                years=max(1, args.years),
                kind=args.kind,
                min_score=args.min_score,
                pause_s=max(0.0, args.pause),
                dry_run=args.dry_run,
            )
        except KeyboardInterrupt:
            print("\nInterrupted by user.", file=sys.stderr)
            break
        except Exception as ex:  # noqa: BLE001
            print(f"  [error] {ex!r}", file=sys.stderr)
            res = {"folder": folder.name, "status": "crashed", "error": str(ex)}
        results.append(res)

    elapsed = (datetime.now() - t0).total_seconds()

    print("\n" + "=" * 72)
    print(f" Done in {elapsed:.1f}s — {len(results)} folder(s) processed")
    print("=" * 72)
    print(f"{'Folder':40s}  {'Sym':10s}  Annual(p/s/e/f)  Quarterly(p/s/e/f)")
    for r in results:
        if r.get("status") != "ok":
            print(f"{r.get('folder',''):40s}  -  status={r.get('status')}")
            continue
        a = r["annual"]
        q = r["quarterly"]
        print(
            f"{r['folder']:40s}  {r['symbol']:10s}  "
            f"{a['picked']:>2}/{a['saved']:>2}/{a['exists']:>2}/{a['failed']:>2}        "
            f"{q['picked']:>2}/{q['saved']:>2}/{q['exists']:>2}/{q['failed']:>2}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
