"""CSE company catalog and report listing for live table extraction."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from Demo_data_download_script import (
    _entry_basename,
    _entry_sort_ms,
    quarter_label,
)
from get_report import safe_dir_name
from get_report import fetch_financials, fetch_trade_summary, report_year, resolve_symbol


def list_cse_companies() -> list[dict[str, str]]:
    rows = fetch_trade_summary()
    out: list[dict[str, str]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if not name or not symbol:
            continue
        out.append({"name": name, "symbol": symbol, "displayName": name})
    out.sort(key=lambda x: x["name"].lower())
    return out


def resolve_company(name: str, min_score: float = 0.78) -> dict[str, Any]:
    rows = fetch_trade_summary()
    sym, official, score = resolve_symbol(name, rows)
    if not sym or score < min_score:
        return {
            "ok": False,
            "company": name,
            "error": f"Could not resolve CSE symbol (best={official!r}, score={score:.2f})",
        }
    return {
        "ok": True,
        "company": official,
        "symbol": sym,
        "score": round(score, 3),
    }


def _latest_annual_by_year(entries: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    bucket: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        year = report_year(entry)
        if not year:
            continue
        prev = bucket.get(year)
        if prev is None or _entry_sort_ms(entry) >= _entry_sort_ms(prev):
            bucket[year] = entry
    return bucket


def _latest_quarterly_by_period(
    entries: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    bucket: dict[tuple[int, int], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ql = quarter_label(entry)
        if ql is None:
            continue
        prev = bucket.get(ql)
        if prev is None or _entry_sort_ms(entry) >= _entry_sort_ms(prev):
            bucket[ql] = entry
    return bucket


def list_company_reports(company_name: str, years: int = 15) -> dict[str, Any]:
    resolved = resolve_company(company_name)
    if not resolved.get("ok"):
        return resolved

    symbol = str(resolved["symbol"])
    official = str(resolved["company"])
    fin = fetch_financials(symbol)
    if not isinstance(fin, dict):
        fin = {}

    annual_raw = fin.get("infoAnnualData") or []
    quarterly_raw = fin.get("infoQuarterlyData") or []
    if not isinstance(annual_raw, list):
        annual_raw = []
    if not isinstance(quarterly_raw, list):
        quarterly_raw = []

    annual_by_year = _latest_annual_by_year(annual_raw)
    quarterly_by_period = _latest_quarterly_by_period(quarterly_raw)

    now_year = datetime.now(tz=timezone.utc).year
    cutoff = now_year - max(1, years)

    years_map: dict[int, dict[str, Any]] = {}

    for year, entry in annual_by_year.items():
        if year < cutoff:
            continue
        slot = years_map.setdefault(
            year,
            {"year": year, "annual": None, "quarterly": []},
        )
        slot["annual"] = {
            "year": year,
            "report_type": "Annual",
            "group": f"Annual report {year}",
            "file_name": _entry_basename(entry),
            "entry_id": entry.get("id"),
            "file_text": str(entry.get("fileText") or ""),
        }

    for (year, quarter), entry in quarterly_by_period.items():
        if year < cutoff:
            continue
        slot = years_map.setdefault(
            year,
            {"year": year, "annual": None, "quarterly": []},
        )
        label = (
            f"Quarterly report {year} Q{quarter}"
            if quarter
            else f"Quarterly report {year}"
        )
        slot["quarterly"].append(
            {
                "year": year,
                "quarter": quarter,
                "report_type": "Quarterly",
                "group": label,
                "file_name": _entry_basename(entry),
                "entry_id": entry.get("id"),
                "file_text": str(entry.get("fileText") or ""),
            }
        )

    year_list = sorted(years_map.keys(), reverse=True)
    for year in year_list:
        years_map[year]["quarterly"].sort(
            key=lambda q: int(q.get("quarter") or 0), reverse=True
        )

    return {
        "ok": True,
        "company": official,
        "symbol": symbol,
        "years": [years_map[y] for y in year_list],
    }


def match_report_selection(
    company_name: str,
    selection: dict[str, Any],
) -> dict[str, Any] | None:
    """Find the CSE financials entry for a user selection."""
    catalog = list_company_reports(company_name, years=30)
    if not catalog.get("ok"):
        return None

    want_type = str(selection.get("report_type") or selection.get("type") or "").lower()
    want_year = int(selection.get("year") or 0)
    want_quarter = selection.get("quarter")
    want_quarter = int(want_quarter) if want_quarter is not None else None

    for block in catalog.get("years") or []:
        if int(block.get("year") or 0) != want_year:
            continue
        if want_type.startswith("ann"):
            annual = block.get("annual")
            if annual:
                return annual
        if want_type.startswith("quart"):
            for q in block.get("quarterly") or []:
                qn = int(q.get("quarter") or 0)
                if want_quarter is None or qn == want_quarter:
                    return q
    return None


def demo_folder_for_report(company_name: str, report: dict[str, Any]) -> tuple[str, str]:
    """Return (relative_folder, abs_folder_name) under Demo_Data."""
    company_dir = safe_dir_name(company_name)
    report_type = str(report.get("report_type") or "")
    group = str(report.get("group") or "")
    if report_type.lower().startswith("quart"):
        rel = f"{company_dir}/Quarterly/{safe_dir_name(group)}"
    else:
        rel = f"{company_dir}/Annual/{safe_dir_name(group)}"
    return rel, group
