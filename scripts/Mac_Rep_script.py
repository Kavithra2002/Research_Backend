"""
Mac_Rep_script.py
=================
Download "Macroeconomic Developments in Charts" PDF reports from the
Central Bank of Sri Lanka and store them under year-based subfolders.

Layout produced
---------------
    backend/MaC Reports/
    ├── 2026/
    │   └── Sri Lanka - Macroeconomic Developments in Charts - As at end March 2026.pdf
    ├── 2025/
    │   ├── Sri Lanka - Macroeconomic Developments in Charts - As at end December 2025.pdf
    │   └── ...
    └── ...

PDFs are named exactly as shown on the CBSL page (sanitized for Windows paths).
Already-downloaded PDFs (non-empty on disk) are skipped, so re-running is
resumable. Use ``--refresh`` to delete existing PDFs and download again.

Usage
-----
    python Mac_Rep_script.py
    python Mac_Rep_script.py --refresh
    python Mac_Rep_script.py --dry-run
    python Mac_Rep_script.py --years 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEFAULT_ROOT = BACKEND_DIR / "MaC Reports"

BASE_URL = "https://www.cbsl.gov.lk"
LIST_URL = (
    f"{BASE_URL}/en/statistics/economic-indicators/macro-economic-chart-pack"
)
TITLE_MARKER = "Macroeconomic Developments in Charts"
YEAR_RE = re.compile(r"(20\d{2})")


class ReportLink(NamedTuple):
    title: str
    url: str
    year: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mac_Rep_script/1.0 (+CBSL macro chart pack downloader)",
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        }
    )
    return s


def _extract_year(title: str, url: str) -> str | None:
    for text in (title, url):
        m = YEAR_RE.search(text)
        if m:
            return m.group(1)
    return None


def _safe_filename(title: str) -> str:
    """Build a Windows-safe filename from the CBSL page link text."""
    name = title.strip()
    name = name.replace(":", " -")
    name = re.sub(r'[<>"/\\|?*]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    if len(name) > 200:
        stem, ext = name.rsplit(".", 1)
        name = f"{stem[:195]}.{ext}"
    return name


def emit_event(enabled: bool, payload: dict[str, object]) -> None:
    if enabled:
        print(json.dumps(payload), flush=True)


def clear_existing_reports(root: Path, dry_run: bool, json_events: bool = False) -> int:
    """Remove all PDFs under ``root`` (year subfolders). Returns count removed."""
    removed = 0
    if not root.exists():
        return removed
    for pdf in sorted(root.rglob("*.pdf")):
        if dry_run:
            print(f"  would delete  {pdf.relative_to(root)}")
        else:
            pdf.unlink()
            print(f"  deleted     {pdf.relative_to(root)}")
        removed += 1
    for folder in sorted(root.iterdir(), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            if not dry_run:
                folder.rmdir()
    return removed


def fetch_report_links(session: requests.Session) -> list[ReportLink]:
    """Collect all Macroeconomic Developments in Charts links across pages."""
    links: list[ReportLink] = []
    seen_urls: set[str] = set()
    page = 0

    while True:
        page_url = LIST_URL if page == 0 else f"{LIST_URL}?page={page}"
        resp = session.get(page_url, timeout=60)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        page_links: list[ReportLink] = []
        for anchor in soup.find_all("a", href=True):
            title = anchor.get_text(strip=True)
            if TITLE_MARKER not in title:
                continue
            href = urljoin(BASE_URL, anchor["href"])
            if href in seen_urls:
                continue
            year = _extract_year(title, href)
            if not year:
                print(f"  [skip] could not parse year: {title}", file=sys.stderr)
                continue
            page_links.append(ReportLink(title=title, url=href, year=year))
            seen_urls.add(href)

        if not page_links:
            break

        links.extend(page_links)

        page_numbers = [
            int(a.get_text(strip=True))
            for a in soup.select(".pager a, .pagination a")
            if a.get_text(strip=True).isdigit()
        ]
        max_page = max(page_numbers) if page_numbers else 0
        page += 1
        if page > max_page:
            break

    links.sort(key=lambda r: (r.year, r.title), reverse=True)
    return links


def download_pdf(
    session: requests.Session,
    url: str,
    dest: Path,
    pause_s: float,
) -> None:
    resp = session.get(url, timeout=120, stream=True)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                fh.write(chunk)
    if pause_s > 0:
        time.sleep(pause_s)


def process_report(
    session: requests.Session,
    report: ReportLink,
    root: Path,
    pause_s: float,
    dry_run: bool,
    json_events: bool = False,
) -> str:
    """Returns: saved | exists | failed | would."""
    dest_dir = root / report.year
    fname = _safe_filename(report.title)
    dest = dest_dir / fname

    if dest.exists() and dest.stat().st_size > 1024:
        if not json_events:
            print(f"  exists  [{report.year}] {fname}")
        return "exists"

    if dry_run:
        if not json_events:
            print(f"  would   [{report.year}] {fname}  <-  {report.url}")
        return "would"

    try:
        if not json_events:
            print(f"  saving  [{report.year}] {fname}")
        download_pdf(session, report.url, dest, pause_s)
        emit_event(
            json_events,
            {
                "type": "saved",
                "year": report.year,
                "file": fname,
                "title": report.title,
            },
        )
        return "saved"
    except (urllib.error.HTTPError, requests.RequestException, OSError) as ex:
        if not json_events:
            print(f"  failed  [{report.year}] {fname}: {ex!r}", file=sys.stderr)
        emit_event(
            json_events,
            {
                "type": "error",
                "year": report.year,
                "file": fname,
                "message": str(ex),
            },
        )
        return "failed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Root folder for year subfolders (default: {DEFAULT_ROOT}).",
    )
    ap.add_argument(
        "--years",
        type=int,
        default=0,
        help="Only download reports from the last N calendar years (0 = all).",
    )
    ap.add_argument(
        "--pause",
        type=float,
        default=0.35,
        help="Seconds to sleep after each successful download (default: 0.35).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List downloads without writing any PDFs.",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Delete existing PDFs in the output folder before downloading.",
    )
    ap.add_argument(
        "--json-events",
        action="store_true",
        help="Emit machine-readable JSON progress lines on stdout.",
    )
    args = ap.parse_args(argv)

    json_events = bool(args.json_events)
    root: Path = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    if args.refresh:
        if not json_events:
            print(f"Clearing existing PDFs under {root} ...")
        n_removed = clear_existing_reports(root, dry_run=args.dry_run, json_events=json_events)
        if not json_events:
            print(f"  removed {n_removed} file(s)\n")

    session = _session()
    emit_event(json_events, {"type": "phase", "phase": "fetching"})
    if not json_events:
        print(f"Fetching report index from CBSL ...")
    reports = fetch_report_links(session)
    if not reports:
        emit_event(json_events, {"type": "error", "message": "No reports found"})
        if not json_events:
            print("No reports found — aborting.", file=sys.stderr)
        return 1

    if args.years and args.years > 0:
        cutoff = max(int(r.year) for r in reports) - args.years + 1
        reports = [r for r in reports if int(r.year) >= cutoff]

    if not json_events:
        print(
            f"Found {len(reports)} report(s). Saving to {root}\n"
            f"  dry-run : {args.dry_run}"
        )

    emit_event(
        json_events,
        {"type": "start", "total": len(reports), "root": str(root)},
    )

    counts = {"saved": 0, "exists": 0, "failed": 0, "would": 0}
    for index, report in enumerate(reports, start=1):
        emit_event(
            json_events,
            {
                "type": "progress",
                "current": index,
                "total": len(reports),
                "year": report.year,
                "title": report.title,
            },
        )
        outcome = process_report(
            session,
            report,
            root,
            pause_s=max(0.0, args.pause),
            dry_run=args.dry_run,
            json_events=json_events,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    emit_event(
        json_events,
        {
            "type": "done",
            "saved": counts["saved"],
            "exists": counts["exists"],
            "failed": counts["failed"],
            "would": counts["would"],
            "newReports": counts["saved"] + counts["would"],
        },
    )
    if not json_events:
        print(
            f"\nDone — saved={counts['saved']} exists={counts['exists']} "
            f"failed={counts['failed']} would={counts['would']}"
        )
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
