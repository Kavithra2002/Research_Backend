"""
db_comb_run.py
==============
Developer test runner for COMB DB-page data capture into MongoDB.

Streams NDJSON progress events for the DB page Run button
(``/api/db/run``).

Input
-----
    python db_comb_run.py --years 2022,2021
    echo '{"years":[2022]}' | python db_comb_run.py --years-stdin
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from extract_comb_data import (
    extract_annual_comb,
    extract_quarterly_comb,
)
from generate_comb_model import (
    COMMERCIAL_BANK_SLUG,
    QUARTERLY_PILOT_QUARTERS,
)
from comb_workbook_store import ensure_indexes, get_db


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_log(text: str, level: str = "info") -> None:
    for raw in str(text).splitlines():
        line = raw.rstrip()
        if line.strip():
            emit({"type": "log", "level": level, "message": line})


def _parse_years(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return sorted(set(out), reverse=True)


def _steps_for_year(year: int) -> list[tuple[str, str | None]]:
    steps: list[tuple[str, str | None]] = [("annual", None)]
    for q in QUARTERLY_PILOT_QUARTERS:
        steps.append(("quarterly", q))
    return steps


def run_capture(
    years: list[int],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
    use_note_extract: bool = True,
    force_note_capture: bool = False,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = False,
) -> int:
    if not years:
        emit({"type": "error", "message": "No years selected"})
        emit({"type": "done", "ok": 0, "failed": 1, "cells_filled": 0, "cells_missing": 0})
        return 1

    all_steps: list[tuple[int, str, str | None]] = []
    for year in years:
        for stage, quarter in _steps_for_year(year):
            all_steps.append((year, stage, quarter))

    total_steps = len(all_steps)
    emit(
        {
            "type": "start",
            "company_slug": company_slug,
            "years": years,
            "totalSteps": total_steps,
        }
    )

    db, client = get_db()
    ensure_indexes(db)

    ok = 0
    failed = 0
    cells_filled = 0
    cells_missing = 0
    done_steps = 0
    seen_years: set[int] = set()

    try:
        for year in years:
            if year not in seen_years:
                seen_years.add(year)
                emit(
                    {
                        "type": "year-start",
                        "year": year,
                        "index": years.index(year) + 1,
                        "totalYears": len(years),
                    }
                )

            for stage, quarter in _steps_for_year(year):
                label = f"{year} · {stage}" + (f" {quarter}" if quarter else "")
                emit(
                    {
                        "type": "stage-start",
                        "year": year,
                        "stage": stage,
                        "quarter": quarter,
                        "label": label,
                        "done": done_steps,
                        "totalSteps": total_steps,
                    }
                )
                emit_log(f"Capturing {label}…")

                try:
                    if stage == "annual":
                        result = extract_annual_comb(
                            db,
                            company_slug,
                            year,
                            use_note_extract=use_note_extract,
                            force_note_capture=force_note_capture,
                            use_openai_notes=use_openai_notes,
                            use_pdf_extract=use_pdf_extract,
                        )
                    else:
                        result = extract_quarterly_comb(
                            db,
                            company_slug,
                            year,
                            quarter or "Q1",
                        )
                    step_ok = bool(result.get("ok", True))
                    filled = int(result.get("cells_filled", 0) or 0)
                    missing = int(result.get("cells_missing", 0) or 0)
                    cells_filled += filled
                    cells_missing += missing
                    if step_ok:
                        ok += 1
                    else:
                        failed += 1
                    done_steps += 1
                    emit(
                        {
                            "type": "stage-done",
                            "year": year,
                            "stage": stage,
                            "quarter": quarter,
                            "label": label,
                            "ok": step_ok,
                            "cells_filled": filled,
                            "cells_missing": missing,
                            "done": done_steps,
                            "totalSteps": total_steps,
                        }
                    )
                    emit_log(
                        f"  {label}: {filled} filled, {missing} missing"
                        + ("" if step_ok else " (issues)")
                    )
                except Exception as exc:
                    failed += 1
                    done_steps += 1
                    emit(
                        {
                            "type": "stage-done",
                            "year": year,
                            "stage": stage,
                            "quarter": quarter,
                            "ok": False,
                            "error": str(exc),
                            "done": done_steps,
                            "totalSteps": total_steps,
                        }
                    )
                    emit_log(f"  {label} failed: {exc}", level="error")

            emit({"type": "year-done", "year": year})

        emit(
            {
                "type": "done",
                "ok": ok,
                "failed": failed,
                "cells_filled": cells_filled,
                "cells_missing": cells_missing,
                "years": years,
            }
        )
        return 0 if failed == 0 else 1
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        emit_log(traceback.format_exc(), level="error")
        emit(
            {
                "type": "done",
                "ok": ok,
                "failed": failed + 1,
                "cells_filled": cells_filled,
                "cells_missing": cells_missing,
            }
        )
        return 1
    finally:
        client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="COMB DB-page capture for selected years")
    ap.add_argument("--years", default="", help="Comma-separated years, e.g. 2022,2021")
    ap.add_argument("--years-stdin", action="store_true", help="Read years JSON from stdin")
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument("--no-note-extract", action="store_true")
    ap.add_argument("--force-note-capture", action="store_true")
    ap.add_argument("--use-openai-notes", action="store_true")
    ap.add_argument("--use-pdf-extract", action="store_true")
    args = ap.parse_args()

    years: list[int] = []
    if args.years_stdin:
        try:
            payload = json.load(sys.stdin)
            raw = payload.get("years") if isinstance(payload, dict) else payload
            if isinstance(raw, list):
                years = sorted({int(y) for y in raw}, reverse=True)
        except Exception as exc:
            emit({"type": "error", "message": f"Invalid stdin JSON: {exc}"})
            return 1
    elif args.years.strip():
        years = _parse_years(args.years)

    use_pdf_extract = args.use_pdf_extract
    return run_capture(
        years,
        company_slug=args.company_slug.strip() or COMMERCIAL_BANK_SLUG,
        use_note_extract=not args.no_note_extract,
        force_note_capture=args.force_note_capture,
        use_openai_notes=args.use_openai_notes,
        use_pdf_extract=use_pdf_extract,
    )


if __name__ == "__main__":
    raise SystemExit(main())
