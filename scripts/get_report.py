"""
Download CSE (Colombo Stock Exchange) listed company annual reports.

Uses public CSE JSON APIs (same as the website): trade summary to resolve a
company name to a trading symbol, then financials to list annual PDFs.

Output layout (default): ./COMPANY/<company name>/<id>_<year>.pdf

By default, up to the 10 most recent distinct annual-report years returned by
CSE are downloaded (based on report dates / titles), not “calendar year == today only”.

Company names can be passed on the command line and/or one name per line in a
text file. Use --all to download for every company returned by CSE trade summary.
Processing stops when all names have been handled (or skipped on ambiguous /
missing matches).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

CSE_ORIGIN = "https://www.cse.lk"
CSE_API = f"{CSE_ORIGIN}/api"
CDN_BASE = "https://cdn.cse.lk"

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "COMPANY"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _json_post(url: str, body: str, referer: str, timeout: int = 120) -> Any:
    data = body.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": CSE_ORIGIN,
            "Referer": referer,
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_trade_summary(timeout: int = 120) -> list[dict[str, Any]]:
    """All listed equities with `name` and `symbol` (e.g. SAMP.N0000)."""
    url = f"{CSE_API}/tradeSummary"
    j = _json_post(url, "", referer=f"{CSE_ORIGIN}/", timeout=timeout)
    rows = j.get("reqTradeSummery") or j.get("reqTradeSummary") or []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("symbol") and r.get("name")]


def fetch_financials(symbol: str, timeout: int = 120) -> dict[str, Any]:
    url = f"{CSE_API}/financials"
    body = urllib.parse.urlencode({"symbol": symbol})
    referer = f"{CSE_ORIGIN}/company-profile?symbol={urllib.parse.quote(symbol)}"
    j = _json_post(url, body, referer=referer, timeout=timeout)
    if not isinstance(j, dict):
        return {}
    return j


_SUFFIX_NOISE = re.compile(
    r"\b(PLC|P\.L\.C\.|LTD|LIMITED|INC|CORP|CORPORATION|HOLDINGS?|GROUP)\b\.?",
    re.I,
)


def normalize_company_label(s: str) -> str:
    t = (s or "").upper()
    t = _SUFFIX_NOISE.sub(" ", t)
    t = re.sub(r"[^\w\s&]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def resolve_symbol(query: str, rows: list[dict[str, Any]]) -> tuple[str, str, float]:
    """
    Return (symbol, official_name, score) for best match.
    score in [0,1]; caller should enforce a minimum threshold.
    """
    qn = normalize_company_label(query)
    if not qn:
        return "", "", 0.0

    best_sym = ""
    best_name = ""
    best = 0.0

    for r in rows:
        name = str(r.get("name") or "")
        sym = str(r.get("symbol") or "")
        nn = normalize_company_label(name)
        if not sym or not nn:
            continue

        if qn == nn:
            return sym, name, 1.0
        if qn in nn or nn in qn:
            score = 0.92
        else:
            score = SequenceMatcher(None, qn, nn).ratio()

        token_q = set(qn.split())
        token_n = set(nn.split())
        if len(token_q) >= 2 and token_q <= token_n:
            score = max(score, 0.88)
        if len(token_n) >= 2 and token_n <= token_q:
            score = max(score, 0.85)

        if score > best:
            best = score
            best_sym = sym
            best_name = name

    return best_sym, best_name, best


def _year_from_ms(ms: Any) -> int | None:
    if ms is None:
        return None
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    # CSE uses millis since epoch
    if v > 10_000_000_000:
        v = int(v / 1000)
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).year
    except (OSError, OverflowError, ValueError):
        return None


def _year_hint_from_text(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(20\d{2})\s*/\s*(20\d{2})", text)
    if m:
        try:
            return int(m.group(2))
        except ValueError:
            pass
    nums = [int(x) for x in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    return max(nums) if nums else None


def report_year(entry: dict[str, Any]) -> int | None:
    y = _year_from_ms(entry.get("manualDate"))
    if y:
        return y
    return _year_hint_from_text(str(entry.get("fileText") or ""))


def normalize_cdn_path(path: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    p = path.strip().lstrip("/")
    if not p:
        return None
    if p.endswith("."):
        p = p.rstrip(".")
    if not p.startswith("cmt/"):
        p = "cmt/" + p
    return p


def cdn_url(path: str) -> str:
    rel = normalize_cdn_path(path)
    if not rel:
        raise ValueError("empty path")
    parts = rel.split("/")
    enc = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    return f"{CDN_BASE}/{enc}"


def download_pdf(url: str, dest: Path, timeout: int = 180) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _entry_sort_ms(x: dict[str, Any]) -> int:
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


def pick_annual_last_n_years(
    info_annual: Iterable[dict[str, Any]], years: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    """
    Keep the newest upload per distinct report year, then take the `years` most
    recent report years available from CSE (fiscal/calendar year from dates
    and titles — not tied to “this calendar year only”).
    """
    now = now or datetime.now(tz=timezone.utc)
    current_year = now.year

    by_year: dict[int, dict[str, Any]] = {}
    for e in info_annual:
        if not isinstance(e, dict):
            continue
        y = report_year(e)
        if y is None or y > current_year + 1:
            continue
        prev = by_year.get(y)
        if prev is None:
            by_year[y] = e
            continue
        if _entry_sort_ms(e) >= _entry_sort_ms(prev):
            by_year[y] = e

    sorted_years = sorted(by_year.keys(), reverse=True)
    chosen_years = sorted_years[: max(1, years)]
    return [by_year[y] for y in sorted(chosen_years, reverse=True)]


def safe_dir_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    name = re.sub(r"\s+", " ", name).strip() or "unknown_company"
    return name[:180]


def load_company_queries(paths: list[Path], inline: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    out.extend(s.strip() for s in inline if s.strip())
    return out


@dataclass
class DownloadResult:
    symbol: str
    saved: list[Path]
    skipped: list[str]


def process_company(
    query: str,
    rows: list[dict[str, Any]],
    root: Path,
    years: int,
    min_score: float,
    dry_run: bool,
    pause_s: float,
) -> DownloadResult | None:
    sym, official, score = resolve_symbol(query, rows)
    if not sym or score < min_score:
        print(f"[skip] No confident match for {query!r} (best score {score:.2f}).", file=sys.stderr)
        return None

    print(f"[match] {query!r} -> {official} ({sym}), score={score:.2f}")

    fin = fetch_financials(sym)
    annual = fin.get("infoAnnualData") or []
    if not isinstance(annual, list):
        annual = []

    picked = pick_annual_last_n_years(annual, years=years)
    if not picked:
        print(f"[warn] No annual reports in the last {years} years for {sym}.", file=sys.stderr)
        return DownloadResult(symbol=sym, saved=[], skipped=["no annual data in window"])

    dest_dir = root / safe_dir_name(official)
    saved: list[Path] = []
    skipped: list[str] = []

    for e in picked:
        y = report_year(e) or "unknown_year"
        path = e.get("path")
        if not path:
            skipped.append(str(e.get("id")))
            continue
        url_primary = None
        try:
            url_primary = cdn_url(str(path))
        except ValueError:
            pass
        alt = normalize_cdn_path(e.get("path2")) if e.get("path2") else None
        url_alt = None
        if alt:
            try:
                url_alt = cdn_url(alt)
            except ValueError:
                url_alt = None

        rid = e.get("id", "x")
        fname = f"{rid}_{y}.pdf"
        dest = dest_dir / fname

        if dest.exists() and dest.stat().st_size > 1024:
            print(f"  exists {dest.name}")
            saved.append(dest)
            continue

        if dry_run:
            print(f"  would download {fname} <- {url_primary or url_alt}")
            continue

        ok = False
        for url in (url_primary, url_alt):
            if not url:
                continue
            try:
                print(f"  downloading {fname} ...")
                download_pdf(url, dest)
                ok = True
                break
            except urllib.error.HTTPError as ex:
                print(f"  http {ex.code} for {url}", file=sys.stderr)
            except Exception as ex:
                print(f"  error {ex!r} for {url}", file=sys.stderr)

        if ok:
            saved.append(dest)
        else:
            skipped.append(str(path))

        if pause_s > 0:
            time.sleep(pause_s)

    return DownloadResult(symbol=sym, saved=saved, skipped=skipped)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "companies",
        nargs="*",
        help="Company names or fragments as shown on the CSE (e.g. 'Sampath Bank').",
    )
    p.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        type=Path,
        default=[],
        help="Text file with one company name per line (# comments allowed). Repeatable.",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Folder under which per-company dirs are created (default: {DEFAULT_ROOT}).",
    )
    p.add_argument(
        "--years",
        type=int,
        default=10,
        help="How many most recent distinct annual-report years to download (default: 10).",
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=0.78,
        help="Minimum fuzzy-match score 0-1 for name resolution (default: 0.78).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve symbols and list downloads without writing PDFs.",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=0.35,
        help="Seconds to sleep between PDF downloads (default: 0.35).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Download for every listed company from CSE (do not pass names or --file).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.all and (args.companies or args.files):
        print("Use either --all or explicit names/--file, not both.", file=sys.stderr)
        return 2

    args.root.mkdir(parents=True, exist_ok=True)

    print("Fetching listed companies from CSE...")
    rows = fetch_trade_summary()
    if not rows:
        print("Failed to load trade summary (empty list).", file=sys.stderr)
        return 1

    if args.all:
        seen: set[str] = set()
        queries: list[str] = []
        for r in rows:
            sym = str(r.get("symbol") or "")
            name = str(r.get("name") or "").strip()
            if not sym or not name or sym in seen:
                continue
            seen.add(sym)
            queries.append(name)
        queries.sort(key=str.casefold)
        print(f"Downloading annual reports for all {len(queries)} listed companies.")
    else:
        queries = load_company_queries(args.files, list(args.companies))
        if not queries:
            print(
                "No company names provided. Pass names, use --file, or use --all.",
                file=sys.stderr,
            )
            return 2

    for q in queries:
        process_company(
            q,
            rows=rows,
            root=args.root,
            years=max(1, args.years),
            min_score=args.min_score,
            dry_run=args.dry_run,
            pause_s=max(0.0, args.pause),
        )

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
