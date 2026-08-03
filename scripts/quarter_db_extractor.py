"""
quarter_db_extractor.py
=======================
Extract DB-page quarterly cell values from financial_tables keywords.

Skips cells that already have filled values unless --force is passed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from typing import Any

from comb_workbook_store import ensure_indexes, get_db
from extract_comb_data import extract_quarterly_comb
from generate_comb_model import COMMERCIAL_BANK_SLUG, QUARTERLY_PILOT_QUARTERS
from runner_common import configure_stdio, emit, emit_log

configure_stdio()

_YEAR_RE = re.compile(r"(20\d{2})")
_QUARTER_RE = re.compile(r"\bQ([1-4])\b", re.I)


def _year_quarter_from_item(raw: dict[str, Any]) -> tuple[int, str] | None:
    """Resolve (year, Qn) from a ticked local report item."""
    year_raw = raw.get("year")
    quarter_raw = raw.get("quarter")
    sources = [
        raw.get("group"),
        raw.get("file_name"),
        raw.get("rel_path"),
        raw.get("group_name"),
    ]

    year: int | None = None
    if year_raw is not None:
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            year = None
    if year is None:
        for s in sources:
            if not s:
                continue
            m = _YEAR_RE.search(str(s))
            if m:
                year = int(m.group(1))
                break
    if year is None or year < 1990 or year > 2100:
        return None

    quarter: str | None = None
    if quarter_raw is not None:
        q = str(quarter_raw).strip().upper()
        if q.isdigit() and q in {"1", "2", "3", "4"}:
            quarter = f"Q{q}"
        elif q.startswith("Q") and len(q) == 2 and q[1].isdigit():
            quarter = q
    if quarter is None:
        for s in sources:
            if not s:
                continue
            m = _QUARTER_RE.search(str(s))
            if m:
                quarter = f"Q{m.group(1)}"
                break
    if quarter is None:
        return None

    return year, quarter


def _parse_steps(payload: dict[str, Any]) -> list[tuple[int, str]]:
    """Return [(year, quarter), ...] from request payload."""
    steps: list[tuple[int, str]] = []
    items = payload.get("items") or []
    if isinstance(items, list) and items:
        seen: set[tuple[int, str]] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            parsed = _year_quarter_from_item(raw)
            if not parsed or parsed in seen:
                continue
            seen.add(parsed)
            steps.append(parsed)
        if steps:
            return sorted(steps, key=lambda t: (-t[0], t[1]))

    years = payload.get("years")
    quarters = payload.get("quarters") or QUARTERLY_PILOT_QUARTERS
    if isinstance(years, list):
        for year in sorted({int(y) for y in years}, reverse=True):
            for quarter in quarters:
                steps.append((year, str(quarter)))
    return steps


def run_quarter_db(
    steps: list[tuple[int, str]],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
    skip_existing: bool = True,
) -> int:
    if not steps:
        emit({"type": "error", "message": "No quarterly steps selected"})
        emit({"type": "done", "ok": 0, "failed": 1})
        return 1

    emit(
        {
            "type": "start",
            "mode": "db-quarterly",
            "company_slug": company_slug,
            "totalSteps": len(steps),
            "skipExisting": skip_existing,
        }
    )

    db, client = get_db()
    ensure_indexes(db)
    ok = failed = 0
    cells_filled = cells_missing = cells_skipped = 0

    try:
        for idx, (year, quarter) in enumerate(steps, 1):
            label = f"{year} · {quarter}"
            emit(
                {
                    "type": "stage-start",
                    "year": year,
                    "quarter": quarter,
                    "stage": "quarterly",
                    "label": label,
                    "index": idx,
                    "totalSteps": len(steps),
                }
            )
            emit_log(f"DB quarterly extraction for {label}…")
            try:
                result = extract_quarterly_comb(
                    db,
                    company_slug,
                    year,
                    quarter,
                    skip_existing=skip_existing,
                )
                filled = int(result.get("cells_filled", 0) or 0)
                missing = int(result.get("cells_missing", 0) or 0)
                skipped = int(result.get("cells_skipped_existing", 0) or 0)
                validation = result.get("validation") or {}
                cells_filled += filled
                cells_missing += missing
                cells_skipped += skipped
                ok += 1
                if validation.get("recovered"):
                    emit_log(
                        f"  Validation recovered {validation['recovered']} cell(s), "
                        f"corrected {validation.get('corrected', 0)}"
                    )
                if validation.get("pdf_corrected"):
                    emit_log(
                        f"  PDF cross-check corrected {validation['pdf_corrected']} value(s) "
                        f"against source report"
                    )
                if validation.get("still_missing"):
                    emit_log(
                        f"  Still missing after validation: "
                        f"{validation['still_missing']} cell(s)",
                        level="warn",
                    )
                emit(
                    {
                        "type": "stage-done",
                        "year": year,
                        "quarter": quarter,
                        "label": (
                            f"Quarterly report {year} {quarter}"
                            if quarter
                            else f"Quarterly report {year}"
                        ),
                        "ok": True,
                        "cells_filled": filled,
                        "cells_missing": missing,
                        "cells_skipped_existing": skipped,
                        "validation": validation,
                    }
                )
            except Exception as exc:
                failed += 1
                emit(
                    {
                        "type": "stage-done",
                        "year": year,
                        "quarter": quarter,
                        "stage": "quarterly",
                        "ok": False,
                        "error": str(exc),
                    }
                )
                emit_log(traceback.format_exc(), level="error")

        emit(
            {
                "type": "done",
                "ok": ok,
                "failed": failed,
                "cells_filled": cells_filled,
                "cells_missing": cells_missing,
                "cells_skipped_existing": cells_skipped,
            }
        )
        return 0 if failed == 0 else 1
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items-stdin", action="store_true")
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    if not args.items_stdin:
        emit({"type": "error", "message": "Provide --items-stdin with years/quarters JSON."})
        return 2

    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        emit({"type": "error", "message": f"Invalid stdin JSON: {exc!r}"})
        return 2

    steps = _parse_steps(payload if isinstance(payload, dict) else {})
    return run_quarter_db(
        steps,
        company_slug=args.company_slug,
        skip_existing=not args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
