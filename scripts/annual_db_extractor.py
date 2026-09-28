"""
annual_db_extractor.py
======================
Annual DB run for the ticked years:

  Extract each company's printed income statement, statement of comprehensive
  income, statement of financial position, cash flow statement, and the notes
  those statements point at.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from datetime import datetime, timezone

from comb_note_extractor import resolve_annual_pdf
from comb_workbook_store import ensure_indexes, get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG
from printed_statements import default_output_path, extract_printed_report
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


def _company_name(db, company_slug: str) -> str:
    doc = db.companies.find_one({"slug": company_slug}, {"name": 1})
    if doc and doc.get("name"):
        return str(doc["name"]).strip()
    return company_slug.replace("_", " ").strip()


def _store_printed(db, payload: dict) -> tuple[int, int]:
    """Replace this company-year's annual tables with the printed extract."""
    slug = str(payload["company_slug"])
    year = int(payload["year"])
    name = str(payload.get("company_name") or slug)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report_group = f"Annual Report {year}"
    db.financial_tables.delete_many(
        {"company_slug": slug, "year": year, "report_type": "annual"}
    )
    docs: list[dict] = []
    filled = 0
    missing = 0

    def add_doc(statement_key: str, table: dict, title: str) -> None:
        nonlocal filled
        rows = table.get("rows") or []
        filled += sum(1 for row in rows if row.get("style") == "data")
        docs.append(
            {
                "company_slug": slug,
                "company_name": name,
                "year": year,
                "report_type": "annual",
                "report_group": report_group,
                "report_key": report_group,
                "quarter": None,
                "period_label": None,
                "statement_key": statement_key,
                "statement_title": title,
                "statement_label": title,
                "table_index": 0,
                "caption": None,
                "preamble": "",
                "footnotes": "",
                "header_rows": table.get("header_rows") or [],
                "rows": rows,
                "row_count": len(rows),
                "source_pdf": payload.get("source_pdf"),
                "source_page": (table.get("pages") or [None])[0],
                "source_pages": table.get("pages") or [],
                "note_column": table.get("note_column"),
                "unit": table.get("unit") or payload.get("unit") or "",
                "extraction_model": "printed-statements",
                "extraction_status": "ok" if table.get("ok", True) else "missing",
                "extracted_at": now,
                "uploaded_at": now,
            }
        )

    for statement in payload.get("statements") or []:
        if statement.get("ok"):
            add_doc(
                str(statement.get("key")),
                statement,
                str(statement.get("title") or statement.get("key")),
            )
        else:
            missing += 1
    seen_notes: set[str] = set()
    for note in (payload.get("notes") or {}).values():
        ref = str(note.get("note_ref") or "").strip()
        if not ref or ref in seen_notes or not note.get("ok"):
            continue
        seen_notes.add(ref)
        add_doc(f"note_{ref}", note, f"Note {ref} — {note.get('title') or ''}".strip(" —"))
    if docs:
        db.financial_tables.insert_many(docs)
    db.companies.update_one(
        {"slug": slug},
        {
            "$set": {"name": name, "updated_at": now},
            "$setOnInsert": {"slug": slug, "created_at": now},
        },
        upsert=True,
    )
    return filled, missing


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
                f"Printed statements for {year} "
                f"(income, comprehensive income, financial position, cash flows, notes)…"
            )
            try:
                pdf_path = resolve_annual_pdf(db, company_slug, year)
                if pdf_path is None or not pdf_path.exists():
                    raise FileNotFoundError(
                        f"Annual PDF not found for {company_slug} {year}"
                    )
                company_name = _company_name(db, company_slug)
                emit_log(f"  Reading {pdf_path.name}")
                payload = extract_printed_report(
                    pdf_path,
                    company_slug=company_slug,
                    company_name=company_name,
                    year=year,
                )
                out = default_output_path(company_slug, year)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                filled, missing = _store_printed(db, payload)
                cells_filled += filled
                cells_missing += missing
                skipped = 0
                cells_skipped += skipped
                for statement in payload.get("statements") or []:
                    emit_log(
                        f"  {statement.get('title')}: "
                        f"{len(statement.get('rows') or [])} rows "
                        f"pages {statement.get('pages') or []}"
                    )
                emit_log(f"  Notes extracted: {len(payload.get('notes') or {})}")

                ok += 1
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
