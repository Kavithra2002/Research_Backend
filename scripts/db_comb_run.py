"""
db_comb_run.py
==============
Developer test runner for COMB / DB-page capture into MongoDB
(``Run DB all``).

Runs annual then quarterly OpenAI vision extractors with accuracy
recheck for the selected years. Accuracy beats speed — 1–2 minutes
per report is acceptable.

Streams NDJSON progress events for the DB page Run button.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from generate_comb_model import (
    COMMERCIAL_BANK_SLUG,
    QUARTERLY_PILOT_QUARTERS,
)


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


def run_capture(
    years: list[int],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
    use_note_extract: bool = True,
    force_note_capture: bool = False,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = True,
    api_key: str | None = None,
    model: str = "gpt-5",
    force: bool = False,
) -> int:
    del force_note_capture, use_openai_notes  # reserved / legacy flags

    if not years:
        emit({"type": "error", "message": "No years selected"})
        emit({"type": "done", "ok": 0, "failed": 1, "cells_filled": 0, "cells_missing": 0})
        return 1

    import annual_db_extractor as annual_db
    import quarter_db_extractor as quarter_db

    emit(
        {
            "type": "start",
            "mode": "db-all",
            "company_slug": company_slug,
            "years": years,
            "totalSteps": len(years) * (1 + len(QUARTERLY_PILOT_QUARTERS)),
            "model": model,
            "openai": True,
        }
    )
    emit_log(
        f"Run DB all: notes-only annual (from Extracted Tables) + quarterly "
        f"for {len(years)} year(s) (model={model})"
    )

    # ── Annual (Extracted FS + notes → DB page) ────────────────────────
    emit_log("── Annual DB extract (notes only) ──")
    rc_annual = annual_db.run_annual_db(
        years,
        company_slug=company_slug,
        skip_existing=not force,
        use_note_extract=use_note_extract,
        use_pdf_extract=use_pdf_extract,
        api_key=api_key,
        model=model,
    )

    # ── Quarterly (refresh + COMB map → DB page) ───────────────────────
    steps: list[tuple[int, str]] = [
        (year, q) for year in years for q in QUARTERLY_PILOT_QUARTERS
    ]
    emit_log(f"── Quarterly DB extract ({len(steps)} period(s)) ──")
    rc_quarter = quarter_db.run_quarter_db(
        steps,
        company_slug=company_slug,
        skip_existing=not force,
        api_key=api_key,
        model=model,
    )

    failed = (0 if rc_annual == 0 else 1) + (0 if rc_quarter == 0 else 1)
    emit(
        {
            "type": "done",
            "ok": 2 - failed,
            "failed": failed,
            "years": years,
            "annual_rc": rc_annual,
            "quarterly_rc": rc_quarter,
        }
    )
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="DB-page capture (annual + quarterly) with OpenAI accuracy recheck"
    )
    ap.add_argument("--years", default="", help="Comma-separated years, e.g. 2022,2021")
    ap.add_argument("--years-stdin", action="store_true", help="Read years JSON from stdin")
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument("--no-note-extract", action="store_true")
    ap.add_argument(
        "--include-notes",
        action="store_true",
        help="Extract note tables (default on unless --no-note-extract)",
    )
    ap.add_argument("--force-note-capture", action="store_true")
    ap.add_argument("--use-openai-notes", action="store_true")
    ap.add_argument("--use-pdf-extract", action="store_true")
    ap.add_argument("--force", action="store_true", help="Re-extract even if cells filled")
    ap.add_argument("--apikey", default=None, help="OpenAI API key")
    ap.add_argument("--model", default="gpt-5", help="OpenAI vision model")
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

    use_notes = not bool(args.no_note_extract)
    if args.include_notes:
        use_notes = True

    return run_capture(
        years,
        company_slug=args.company_slug.strip() or COMMERCIAL_BANK_SLUG,
        use_note_extract=use_notes,
        force_note_capture=args.force_note_capture,
        use_openai_notes=args.use_openai_notes,
        use_pdf_extract=args.use_pdf_extract or True,
        api_key=args.apikey,
        model=args.model,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
