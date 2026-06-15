"""
Demo_data_download_script.py
============================
Download the past N years (default 10) of annual and quarterly financial
reports for a fixed list of CSE-listed companies into the ``Demo_Data``
folder.

Layout produced
---------------
    backend/Demo_Data/
    └── Dialog Axiata PLC/
        ├── Annual/
        │   ├── Annual report 2025/
        │   │   └── 643_1756350200465.pdf
        │   ├── Annual report 2024/
        │   │   └── ...
        │   └── ...
        └── Quarterly/
            ├── Quarterly report 2025 Q2/
            │   └── 643_1770974381263.pdf
            ├── Quarterly report 2025 Q1/
            └── ...

Re-uses helpers from ``get_report.py`` (CSE symbol resolution + PDF download)
and ``download_company_reports.py``-style picker logic.

Already-downloaded PDFs (matched by filename + non-empty size) are skipped, so
re-running the script is fully resumable.

Usage
-----
    # Default: 5 companies, last 10 years, annual + quarterly.
    python Demo_data_download_script.py

    # Dry run — list what would be downloaded, write nothing.
    python Demo_data_download_script.py --dry-run

    # Only annual / only quarterly.
    python Demo_data_download_script.py --kind annual
    python Demo_data_download_script.py --kind quarterly

    # Different window or company list.
    python Demo_data_download_script.py --years 5
    python Demo_data_download_script.py --companies "Dialog Axiata PLC" "LOLC Holdings PLC"
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from get_report import (  # noqa: E402  — local helpers
    cdn_url,
    download_pdf,
    fetch_financials,
    fetch_trade_summary,
    normalize_cdn_path,
    pick_annual_last_n_years,
    report_year,
    resolve_symbol,
    safe_dir_name,
)

BACKEND_DIR = SCRIPT_DIR.parent
DEMO_DATA_DIR = BACKEND_DIR / "Demo_Data"

# The five companies requested. Names are matched fuzzily against the CSE
# trade-summary feed, so minor wording differences are fine.
COMPANIES: list[str] = [
    "Dialog Axiata PLC",
    "John Keells Holdings PLC",
    "Ceylon Tobacco Company PLC",
    "Commercial Bank of Ceylon PLC",
    "LOLC Holdings PLC",
]

# Hard-coded CSE symbol overrides for the cases where the fuzzy name matcher
# is unreliable (e.g. "LOLC HOLDINGS" vs "LOLC FINANCE" both normalize to
# "LOLC" once the noise suffix "HOLDINGS" is stripped). Keys are
# case-insensitive. Take precedence over name resolution when present.
SYMBOL_OVERRIDES: dict[str, str] = {
    "dialog axiata plc":            "DIAL.N0000",
    "john keells holdings plc":     "JKH.N0000",
    "ceylon tobacco company plc":   "CTC.N0000",
    "commercial bank of ceylon plc": "COMB.N0000",
    "lolc holdings plc":            "LOLC.N0000",
}


# ─────────────────────────────────────────────────────────────────────────────
# Quarter detection
# ─────────────────────────────────────────────────────────────────────────────

_QUARTER_TOKEN = re.compile(r"\bQ\s*([1-4])\b", re.I)
_QUARTER_WORD = re.compile(
    r"\b(1st|2nd|3rd|4th|first|second|third|fourth)\s+quarter\b", re.I
)
_WORD_TO_Q = {
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4,
    "first": 1, "second": 2, "third": 3, "fourth": 4,
}


def _entry_period_end(entry: dict[str, Any]) -> datetime | None:
    """Best-effort period-end timestamp (UTC) for a quarterly entry."""
    ms = entry.get("manualDate")
    if ms is None:
        return None
    try:
        v = int(ms)
        if v > 10_000_000_000:  # millis -> seconds
            v //= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _quarter_from_month(m: int) -> int:
    if 1 <= m <= 3:
        return 1
    if 4 <= m <= 6:
        return 2
    if 7 <= m <= 9:
        return 3
    return 4


def quarter_label(entry: dict[str, Any]) -> tuple[int, int] | None:
    """Return ``(year, quarter)`` for a CSE quarterly entry, or ``None``.

    Strategy:
      1. ``manualDate`` -> calendar quarter from period-end month.
      2. Title-text fallbacks (``Q1``, ``First Quarter`` etc.) when there is
         no usable date.
    """
    dt = _entry_period_end(entry)
    if dt is not None:
        return dt.year, _quarter_from_month(dt.month)

    y = report_year(entry)
    if not y:
        return None

    text = str(entry.get("fileText") or "")
    m = _QUARTER_TOKEN.search(text)
    if m:
        return y, int(m.group(1))
    m2 = _QUARTER_WORD.search(text)
    if m2:
        return y, _WORD_TO_Q[m2.group(1).lower()]
    return y, 0  # year-only — name folder without "Qn"


def _entry_sort_ms(x: dict[str, Any]) -> int:
    """Most-recent-first ordering key (epoch seconds)."""
    for k in ("manualDate", "uploadedDate", "authorizedDate"):
        v = x.get(k)
        try:
            if v is None:
                continue
            iv = int(v)
            return iv if iv < 10_000_000_000 else iv // 1000
        except (TypeError, ValueError):
            continue
    return 0


def pick_quarterly_last_n_years(
    info_quarterly: Iterable[dict[str, Any]],
    years: int,
    now: datetime | None = None,
) -> list[tuple[int, int, dict[str, Any]]]:
    """Latest entry per ``(year, quarter)`` within the most recent ``years``
    fiscal years (counted from the newest report year on file)."""
    now = now or datetime.now(tz=timezone.utc)

    bucket: dict[tuple[int, int], dict[str, Any]] = {}
    for e in info_quarterly:
        if not isinstance(e, dict):
            continue
        ql = quarter_label(e)
        if ql is None:
            continue
        y, _q = ql
        if y > now.year + 1:
            continue
        prev = bucket.get(ql)
        if prev is None or _entry_sort_ms(e) >= _entry_sort_ms(prev):
            bucket[ql] = e

    if not bucket:
        return []

    latest_year = max(y for (y, _) in bucket.keys())
    cutoff = latest_year - max(1, years) + 1

    out = [(y, q, e) for (y, q), e in bucket.items() if y >= cutoff]
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Filenames + download
# ─────────────────────────────────────────────────────────────────────────────

def _entry_basename(entry: dict[str, Any]) -> str:
    """On-disk PDF name — keep the CSE CDN basename so it matches what the
    other scripts in this repo expect (e.g. ``643_1756350200465.pdf``)."""
    rid = entry.get("id", "x")
    for key in ("path", "path2"):
        p = entry.get(key)
        if not p:
            continue
        base = str(p).rstrip("/").rsplit("/", 1)[-1].strip().rstrip(".")
        if not base:
            continue
        if not base.lower().endswith(".pdf"):
            base = f"{base}.pdf"
        return base
    return f"{rid}.pdf"


def _download_entry(
    entry: dict[str, Any],
    dest_dir: Path,
    pause_s: float,
    dry_run: bool,
) -> str:
    """Download a single CSE entry into ``dest_dir``.

    Returns one of: ``"saved" | "exists" | "skipped" | "failed" | "would"``.
    """
    path = entry.get("path")
    if not path:
        print(f"      [skip] no path in entry id={entry.get('id')}", file=sys.stderr)
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
    dest = dest_dir / fname

    if dest.exists() and dest.stat().st_size > 1024:
        print(f"      exists  {fname}")
        return "exists"

    if dry_run:
        print(f"      would   {fname}  <-  {url_primary or url_alt}")
        return "would"

    dest_dir.mkdir(parents=True, exist_ok=True)
    for url in (url_primary, url_alt):
        if not url:
            continue
        try:
            print(f"      saving  {fname}")
            download_pdf(url, dest)
            if pause_s > 0:
                time.sleep(pause_s)
            return "saved"
        except urllib.error.HTTPError as ex:
            print(f"        http {ex.code} for {url}", file=sys.stderr)
        except OSError as ex:
            print(f"        error {ex!r} for {url}", file=sys.stderr)
        except Exception as ex:  # noqa: BLE001
            print(f"        error {ex!r} for {url}", file=sys.stderr)

    return "failed"


def _bump(bucket: dict[str, int], outcome: str) -> None:
    if outcome in bucket:
        bucket[outcome] += 1


# ─────────────────────────────────────────────────────────────────────────────
# Per-company driver
# ─────────────────────────────────────────────────────────────────────────────

def process_company(
    name: str,
    rows: list[dict[str, Any]],
    root: Path,
    years: int,
    kind: str,
    min_score: float,
    pause_s: float,
    dry_run: bool,
) -> dict[str, Any]:
    print(f"\n=== {name} ===")

    override_sym = SYMBOL_OVERRIDES.get(name.strip().casefold())
    if override_sym:
        official = next(
            (str(r.get("name") or "") for r in rows if r.get("symbol") == override_sym),
            name,
        )
        sym, score = override_sym, 1.0
        print(f"  match : {official}  ({sym}, override)")
    else:
        sym, official, score = resolve_symbol(name, rows)
        if not sym or score < min_score:
            print(
                f"  [skip] could not resolve to a CSE symbol "
                f"(best='{official}', score={score:.2f})",
                file=sys.stderr,
            )
            return {"company": name, "status": "unresolved", "score": score}
        print(f"  match : {official}  ({sym}, score={score:.2f})")

    company_dir = root / safe_dir_name(name)
    annual_root = company_dir / "Annual"
    quarterly_root = company_dir / "Quarterly"
    annual_root.mkdir(parents=True, exist_ok=True)
    quarterly_root.mkdir(parents=True, exist_ok=True)

    fin = fetch_financials(sym)
    if not isinstance(fin, dict):
        fin = {}

    summary: dict[str, Any] = {
        "company": name,
        "official": official,
        "symbol": sym,
        "score": round(score, 3),
        "annual":    {"picked": 0, "saved": 0, "exists": 0, "failed": 0, "would": 0},
        "quarterly": {"picked": 0, "saved": 0, "exists": 0, "failed": 0, "would": 0},
    }

    if kind in ("both", "annual"):
        annual = fin.get("infoAnnualData") or []
        if not isinstance(annual, list):
            annual = []
        picked_a = pick_annual_last_n_years(annual, years=years)
        summary["annual"]["picked"] = len(picked_a)
        print(f"  annual    : {len(picked_a)} report(s) targeted (last {years} years)")
        for e in picked_a:
            y = report_year(e) or "unknown"
            sub = annual_root / safe_dir_name(f"Annual report {y}")
            outcome = _download_entry(e, sub, pause_s, dry_run)
            _bump(summary["annual"], outcome)

    if kind in ("both", "quarterly"):
        quarterly = fin.get("infoQuarterlyData") or []
        if not isinstance(quarterly, list):
            quarterly = []
        picked_q = pick_quarterly_last_n_years(quarterly, years=years)
        summary["quarterly"]["picked"] = len(picked_q)
        print(
            f"  quarterly : {len(picked_q)} report(s) targeted (last {years} years)"
        )
        for y, q, e in picked_q:
            label = f"Quarterly report {y} Q{q}" if q else f"Quarterly report {y}"
            sub = quarterly_root / safe_dir_name(label)
            outcome = _download_entry(e, sub, pause_s, dry_run)
            _bump(summary["quarterly"], outcome)

    summary["status"] = "ok"
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=DEMO_DATA_DIR,
        help=f"Root directory for company folders (default: {DEMO_DATA_DIR}).",
    )
    ap.add_argument(
        "--years",
        type=int,
        default=10,
        help="Window for both annual and quarterly reports (default: 10).",
    )
    ap.add_argument(
        "--kind",
        choices=("both", "annual", "quarterly"),
        default="both",
        help="Which kinds of reports to download (default: both).",
    )
    ap.add_argument(
        "--companies",
        nargs="*",
        default=COMPANIES,
        help="Override the default 5-company list.",
    )
    ap.add_argument(
        "--min-score",
        type=float,
        default=0.78,
        help="Minimum fuzzy-match score 0-1 for company name resolution (default: 0.78).",
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

    root: Path = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    print("Fetching CSE trade summary ...")
    rows = fetch_trade_summary()
    if not rows:
        print("Empty CSE trade summary — aborting.", file=sys.stderr)
        return 1

    print(
        f"Will process {len(args.companies)} company/companies into {root}\n"
        f"  years   : {args.years}\n"
        f"  kind    : {args.kind}\n"
        f"  dry-run : {args.dry_run}"
    )

    results: list[dict[str, Any]] = []
    t0 = datetime.now()
    for i, name in enumerate(args.companies, 1):
        print(f"\n[{i}/{len(args.companies)}]")
        try:
            res = process_company(
                name=name,
                rows=rows,
                root=root,
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
            res = {"company": name, "status": "crashed", "error": str(ex)}
        results.append(res)

    elapsed = (datetime.now() - t0).total_seconds()

    print("\n" + "=" * 86)
    print(f" Done in {elapsed:.1f}s — {len(results)} company/companies processed")
    print("=" * 86)
    print(
        f"{'Company':32s}  {'Sym':10s}  "
        f"Annual(pick/save/exist/fail/would)   "
        f"Quarterly(pick/save/exist/fail/would)"
    )
    for r in results:
        if r.get("status") != "ok":
            print(f"{r.get('company',''):32s}  -  status={r.get('status')}")
            continue
        a = r["annual"]
        q = r["quarterly"]
        print(
            f"{r['company']:32s}  {r['symbol']:10s}  "
            f"{a['picked']:>3}/{a['saved']:>3}/{a['exists']:>3}/{a['failed']:>3}/{a['would']:>3}      "
            f"{q['picked']:>3}/{q['saved']:>3}/{q['exists']:>3}/{q['failed']:>3}/{q['would']:>3}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
