"""
Extract_newly_updated.py

Scan all CSE (Colombo Stock Exchange) listed companies and download every
report that was uploaded TODAY (annual, quarterly/interim, and any other
report categories returned by the public CSE `financials` API).

Output layout (local dev):
    ./newly_uploaded_report/<company name>/<report type>/<id>_<year>.pdf

When STORAGE_DRIVER=r2, each PDF is also uploaded to R2:
    updated_reports/<company name>/<report type>/<id>_<year>.pdf

Progress is emitted as NDJSON to stdout so a parent process (such as the
Next.js API route) can stream live progress to the UI. Each line is a JSON
object with a `type` field; the final line is `{"type": "done", ...}`.

Run from anywhere:
    python Extract_newly_updated.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

from get_report import (
    cdn_url,
    download_pdf,
    fetch_financials,
    fetch_trade_summary,
    normalize_cdn_path,
    report_year,
    safe_dir_name,
)
import r2_storage

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "newly_uploaded_report"

REPORT_TYPE_LABELS: dict[str, str] = {
    "infoAnnualData": "Annual",
    "infoInterimData": "Quarterly",
    "infoInterimDataNew": "Quarterly",
    "infoAnnouncementData": "Announcements",
    "infoCircularData": "Circulars",
    "infoNonFinancialData": "Non Financial",
    "infoCorporateData": "Corporate",
    "infoQuarterly": "Quarterly",
}


def emit(obj: dict[str, Any]) -> None:
    """Write a single NDJSON record and flush so the parent sees it live."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ms_to_dt(ms: Any) -> datetime | None:
    if ms is None:
        return None
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    if v > 10_000_000_000:
        v = v // 1000
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _upload_dt(entry: dict[str, Any]) -> datetime | None:
    """Best-effort upload timestamp for a financials entry."""
    for k in ("uploadedDate", "manualDate", "authorizedDate"):
        dt = _ms_to_dt(entry.get(k))
        if dt is not None:
            return dt
    return None


def _is_today(dt: datetime, today: date, tz: timezone) -> bool:
    return dt.astimezone(tz).date() == today


def _iter_data_buckets(fin: dict[str, Any]) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    """
    Yield (label, entries) for every list-of-dict bucket in financials that
    looks like report metadata (has `path` keys).
    """
    for key, value in fin.items():
        if not isinstance(value, list) or not value:
            continue
        if not isinstance(value[0], dict):
            continue
        sample = value[0]
        if "path" not in sample and "path2" not in sample:
            continue
        label = REPORT_TYPE_LABELS.get(key)
        if label is None:
            stripped = key
            for prefix in ("info",):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):]
            for suffix in ("Data", "DataNew"):
                if stripped.endswith(suffix):
                    stripped = stripped[: -len(suffix)]
            label = stripped or key
        yield label, value


def _download_entry(
    entry: dict[str, Any], dest_dir: Path, pause_s: float
) -> tuple[Path | None, str | None]:
    """
    Download a single entry. Returns (saved_path, error_message). If the file
    already exists with content, returns (path, None) without re-downloading.
    """
    raw_path = entry.get("path")
    url_primary = None
    if raw_path:
        try:
            url_primary = cdn_url(str(raw_path))
        except ValueError:
            url_primary = None

    alt = normalize_cdn_path(entry.get("path2")) if entry.get("path2") else None
    url_alt = None
    if alt:
        try:
            url_alt = cdn_url(alt)
        except ValueError:
            url_alt = None

    if not (url_primary or url_alt):
        return None, "no downloadable path"

    rid = entry.get("id", "x")
    year = report_year(entry) or "unknown"
    fname = f"{rid}_{year}.pdf"
    dest = dest_dir / fname

    if dest.exists() and dest.stat().st_size > 1024:
        if r2_storage.is_r2_enabled():
            company = dest_dir.parent.name
            report_type = dest_dir.name
            r2_key = r2_storage.updated_report_key(company, report_type, fname)
            if not r2_storage.object_exists(r2_key):
                try:
                    r2_storage.upload_file(dest, r2_key)
                except Exception:
                    pass
        return dest, None

    # On hosted (R2), the file may already exist in the bucket from a prior scan.
    if r2_storage.is_r2_enabled():
        company = dest_dir.parent.name
        report_type = dest_dir.name
        key = r2_storage.updated_report_key(company, report_type, fname)
        if r2_storage.object_exists(key):
            dest_dir.mkdir(parents=True, exist_ok=True)
            if r2_storage.download_file(key, dest):
                return dest, None

    dest_dir.mkdir(parents=True, exist_ok=True)
    last_err: str | None = None
    for url in (url_primary, url_alt):
        if not url:
            continue
        try:
            download_pdf(url, dest)
            if r2_storage.is_r2_enabled():
                company = dest_dir.parent.name
                report_type = dest_dir.name
                r2_key = r2_storage.updated_report_key(
                    company, report_type, dest.name
                )
                try:
                    r2_storage.upload_file(dest, r2_key)
                except Exception as ex:
                    return None, f"R2 upload failed: {ex!r}"
            if pause_s > 0:
                time.sleep(pause_s)
            return dest, None
        except urllib.error.HTTPError as ex:
            last_err = f"HTTP {ex.code} for {url}"
        except Exception as ex:
            last_err = f"{ex!r} for {url}"
    return None, last_err or "download failed"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output folder (default: {DEFAULT_OUT}).",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=0.15,
        help="Seconds to sleep after each PDF download.",
    )
    p.add_argument(
        "--tz-offset",
        type=float,
        default=5.5,
        help="Hours offset from UTC used to decide 'today' (default 5.5 for Sri Lanka).",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Override target date in YYYY-MM-DD. Defaults to today in --tz-offset.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only scan the first N companies (0 = no limit). Useful for testing.",
    )
    p.add_argument(
        "--symbols",
        default=None,
        help=(
            "Comma-separated list of CSE symbols to restrict the scan to "
            "(e.g. SAMP.N0000,COMB.N0000). Names are matched case-insensitively."
        ),
    )
    p.add_argument(
        "--companies",
        default=None,
        help=(
            "Pipe-separated list of company names to restrict the scan to "
            "(use '||' as separator). Matched case-insensitively against the "
            "official CSE company name."
        ),
    )
    p.add_argument(
        "--filter-file",
        default=None,
        help=(
            "Path to a JSON file with shape "
            "{\"symbols\": [...], \"companies\": [...], \"groupName\": \"...\"}. "
            "Used to restrict the scan to a saved group."
        ),
    )
    args = p.parse_args(argv)

    tz = timezone(timedelta(hours=args.tz_offset))
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            emit({"type": "error", "message": f"Invalid --date: {args.date!r}"})
            return 2
    else:
        target_date = datetime.now(tz=tz).date()

    out_root: Path = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # Resolve the optional company-group filter. Symbols always win when both
    # are provided since they're the unique CSE identifier.
    filter_symbols: set[str] = set()
    filter_company_names: set[str] = set()
    filter_group_name: str | None = None

    def _load_filter_from_text(symbols_text: str | None, companies_text: str | None) -> None:
        nonlocal filter_symbols, filter_company_names
        if symbols_text:
            for s in symbols_text.split(","):
                s = s.strip().upper()
                if s:
                    filter_symbols.add(s)
        if companies_text:
            for c in companies_text.split("||"):
                c = c.strip().casefold()
                if c:
                    filter_company_names.add(c)

    if args.filter_file:
        try:
            with open(args.filter_file, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                syms = payload.get("symbols")
                if isinstance(syms, list):
                    for s in syms:
                        if isinstance(s, str):
                            s = s.strip().upper()
                            if s:
                                filter_symbols.add(s)
                names = payload.get("companies")
                if isinstance(names, list):
                    for c in names:
                        if isinstance(c, str):
                            c = c.strip().casefold()
                            if c:
                                filter_company_names.add(c)
                gname = payload.get("groupName")
                if isinstance(gname, str) and gname.strip():
                    filter_group_name = gname.strip()
        except Exception as ex:
            emit({
                "type": "log",
                "level": "warning",
                "message": f"Could not read --filter-file {args.filter_file}: {ex!r}",
            })

    _load_filter_from_text(args.symbols, args.companies)

    emit({
        "type": "start",
        "targetDate": target_date.isoformat(),
        "tzOffsetHours": args.tz_offset,
        "outDir": str(out_root),
        "filterGroup": filter_group_name,
        "filterCount": len(filter_symbols) or len(filter_company_names),
    })

    try:
        rows = fetch_trade_summary()
    except Exception as ex:
        emit({"type": "error", "message": f"Failed to fetch trade summary: {ex!r}"})
        return 1

    if not rows:
        emit({"type": "error", "message": "Trade summary returned no companies."})
        return 1

    seen_sym: set[str] = set()
    companies: list[dict[str, Any]] = []
    for r in rows:
        sym = str(r.get("symbol") or "")
        name = str(r.get("name") or "").strip()
        if not sym or not name or sym in seen_sym:
            continue
        seen_sym.add(sym)
        companies.append({"symbol": sym, "name": name})
    companies.sort(key=lambda c: c["name"].casefold())

    if filter_symbols or filter_company_names:
        before = len(companies)
        companies = [
            c
            for c in companies
            if (
                (filter_symbols and c["symbol"].upper() in filter_symbols)
                or (
                    filter_company_names
                    and c["name"].strip().casefold() in filter_company_names
                )
            )
        ]
        emit({
            "type": "log",
            "level": "info",
            "message": (
                f"Filter applied: scanning {len(companies)} of {before} companies"
                + (f" (group: {filter_group_name})" if filter_group_name else "")
            ),
        })
        if not companies:
            emit({
                "type": "error",
                "message": (
                    "No companies matched the selected group filter. "
                    "Make sure the group's symbols still exist on CSE."
                ),
            })
            emit({
                "type": "done",
                "targetDate": target_date.isoformat(),
                "totalCompanies": 0,
                "foundCount": 0,
                "failedCount": 0,
                "found": [],
                "failed": [],
                "outDir": str(out_root),
            })
            return 0

    if args.limit and args.limit > 0:
        companies = companies[: args.limit]

    total = len(companies)
    emit({"type": "scan-start", "total": total})

    found_records: list[dict[str, Any]] = []
    failed_records: list[dict[str, Any]] = []

    for idx, c in enumerate(companies, start=1):
        sym = c["symbol"]
        name = c["name"]
        emit({
            "type": "progress",
            "index": idx,
            "total": total,
            "company": name,
            "symbol": sym,
        })

        try:
            fin = fetch_financials(sym)
        except Exception as ex:
            failed_records.append({"company": name, "symbol": sym, "error": repr(ex)})
            emit({
                "type": "company-error",
                "company": name,
                "symbol": sym,
                "error": repr(ex),
            })
            continue

        if not isinstance(fin, dict):
            continue

        company_dir = out_root / safe_dir_name(name)

        for label, entries in _iter_data_buckets(fin):
            type_dir = company_dir / safe_dir_name(label)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                dt = _upload_dt(entry)
                if dt is None:
                    continue
                if not _is_today(dt, target_date, tz):
                    continue

                title = (
                    str(entry.get("fileText") or "").strip()
                    or str(entry.get("title") or "").strip()
                    or f"id {entry.get('id', '?')}"
                )

                saved, err = _download_entry(entry, type_dir, pause_s=args.pause)
                if saved is None:
                    failed_records.append({
                        "company": name,
                        "symbol": sym,
                        "reportType": label,
                        "title": title,
                        "error": err or "unknown error",
                    })
                    emit({
                        "type": "download-error",
                        "company": name,
                        "reportType": label,
                        "title": title,
                        "error": err or "unknown error",
                    })
                    continue

                record = {
                    "company": name,
                    "symbol": sym,
                    "reportType": label,
                    "title": title,
                    "fileName": saved.name,
                    "path": str(saved),
                    "uploadedAt": dt.astimezone(tz).isoformat(),
                    "year": report_year(entry),
                    "id": entry.get("id"),
                }
                found_records.append(record)
                emit({"type": "found", **record})

    emit({
        "type": "done",
        "targetDate": target_date.isoformat(),
        "totalCompanies": total,
        "foundCount": len(found_records),
        "failedCount": len(failed_records),
        "found": found_records,
        "failed": failed_records,
        "outDir": str(out_root),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
