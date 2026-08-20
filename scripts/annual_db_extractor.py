"""
annual_db_extractor.py
======================
Annual DB run — same COMB pipeline as Commercial Bank, for any company:

  1. FS Description values from financial_tables
  2. PDF statement extract (pdfplumber, OpenAI fallback)
  3. Note page PNG captures
  4. Note-table fill for the DB Notes dropdown

Drivers and Ratios are not populated.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from comb_workbook_store import ensure_indexes, get_db
from extract_comb_data import extract_annual_comb
from generate_comb_model import COMMERCIAL_BANK_SLUG
from runner_common import configure_stdio, emit, emit_log

configure_stdio()


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


def run_annual_db(
    years: list[int],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
    skip_existing: bool = False,
    use_note_extract: bool = True,
    force_note_capture: bool = True,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = True,
) -> int:
    if not years:
        emit({"type": "error", "message": "No years selected"})
        emit({"type": "done", "ok": 0, "failed": 1})
        return 1

    emit(
        {
            "type": "start",
            "mode": "db-annual",
            "company_slug": company_slug,
            "years": years,
            "totalSteps": len(years),
            "skipExisting": skip_existing,
        }
    )

    db, client = get_db()
    ensure_indexes(db)
    ok = failed = 0
    cells_filled = cells_missing = cells_skipped = 0

    try:
        for idx, year in enumerate(years, 1):
            emit(
                {
                    "type": "stage-start",
                    "year": year,
                    "stage": "annual",
                    "label": f"{year} · annual",
                    "index": idx,
                    "totalSteps": len(years),
                }
            )
            emit_log(
                f"DB annual extraction for {year} "
                f"(PDF statements + note tables)…"
            )
            try:
                result = extract_annual_comb(
                    db,
                    company_slug,
                    year,
                    use_note_extract=use_note_extract,
                    force_note_capture=force_note_capture,
                    use_openai_notes=use_openai_notes,
                    use_pdf_extract=use_pdf_extract,
                    skip_existing=skip_existing,
                )
                filled = int(result.get("cells_filled", 0) or 0)
                missing = int(result.get("cells_missing", 0) or 0)
                skipped = int(result.get("cells_skipped_existing", 0) or 0)
                cells_filled += filled
                cells_missing += missing
                cells_skipped += skipped
                ok += 1
                validation = result.get("validation") or {}
                if validation.get("recovered"):
                    emit_log(
                        f"  Validation recovered {validation['recovered']} FS cell(s), "
                        f"corrected {validation.get('corrected', 0)}"
                    )
                if validation.get("pdf_corrected"):
                    emit_log(
                        f"  PDF cross-check corrected {validation['pdf_corrected']} FS value(s) "
                        f"against source annual report"
                    )
                if validation.get("still_missing"):
                    emit_log(
                        f"  Still missing after validation: "
                        f"{validation['still_missing']} FS cell(s)",
                        level="warn",
                    )
                emit(
                    {
                        "type": "stage-done",
                        "year": year,
                        "stage": "annual",
                        "label": f"Annual report {year}",
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
                        "stage": "annual",
                        "label": f"Annual report {year}",
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
    ap.add_argument("--years", default="")
    ap.add_argument("--years-stdin", action="store_true")
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument("--force", action="store_true", help="Re-extract filled cells")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip cells that already have a filled value",
    )
    ap.add_argument("--no-note-extract", action="store_true")
    ap.add_argument("--force-note-capture", action="store_true")
    ap.add_argument(
        "--no-force-note-capture",
        action="store_true",
        help="Do not recapture note images/tables when they already exist",
    )
    ap.add_argument(
        "--use-openai-notes",
        action="store_true",
        help="Use OpenAI vision for note extraction (uses API credits)",
    )
    ap.add_argument(
        "--use-pdf-extract",
        action="store_true",
        help="Extract FS values from the annual report PDF (default)",
    )
    ap.add_argument(
        "--no-pdf-extract",
        action="store_true",
        help="Skip PDF statement extract (financial_tables mapper only)",
    )
    args = ap.parse_args(argv)

    years: list[int] = []
    if args.years_stdin:
        try:
            payload = json.load(sys.stdin)
            raw = payload.get("years") if isinstance(payload, dict) else payload
            if isinstance(raw, list):
                years = sorted({int(y) for y in raw}, reverse=True)
        except Exception as exc:
            emit({"type": "error", "message": f"Invalid years JSON: {exc!r}"})
            return 2
    else:
        years = _parse_years(args.years)

    return run_annual_db(
        years,
        company_slug=args.company_slug,
        skip_existing=bool(args.skip_existing) and not args.force,
        use_note_extract=not args.no_note_extract,
        force_note_capture=not args.no_force_note_capture,
        use_openai_notes=args.use_openai_notes,
        use_pdf_extract=not args.no_pdf_extract,
    )


if __name__ == "__main__":
    raise SystemExit(main())
