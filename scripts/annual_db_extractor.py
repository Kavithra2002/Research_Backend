"""
annual_db_extractor.py
======================
Run DB Annual — notes-only for the DB page.

Prerequisite: the company/year must already be extracted into Extracted Tables
(``financial_tables`` via Run Annual). This runner:

  1. Copies the same main FS (income, OCI, SoFP, cash flows) onto the DB page
  2. Extracts only the note tables from the annual PDF
  3. Writes printed JSON (notes live on the DB page only — never in Extracted Tables)

It does **not** re-run OpenAI vision on the main financial statements.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from datetime import datetime, timezone
from pathlib import Path

from comb_note_extractor import resolve_annual_pdf
from comb_workbook_store import ensure_indexes, get_db
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


def _company_name(db, company_slug: str) -> str:
    doc = db.companies.find_one({"slug": company_slug}, {"name": 1})
    if doc and doc.get("name"):
        return str(doc["name"]).strip()
    return company_slug.replace("_", " ").strip()


def _purge_note_docs(db, company_slug: str, year: int | None = None) -> int:
    """Remove note_* rows from financial_tables (Extracted Tables is FS-only)."""
    filt: dict[str, Any] = {
        "company_slug": company_slug,
        "statement_key": {"$regex": r"^note_", "$options": "i"},
    }
    if year is not None:
        filt["year"] = int(year)
    return int(db.financial_tables.delete_many(filt).deleted_count)


def _store_printed(
    db,
    payload: dict,
    *,
    include_notes: bool = False,
    replace_core: bool = True,
) -> tuple[int, int]:
    """Optionally write main FS into financial_tables.

    Notes are **never** written here — they stay in printed_statements JSON
    for the DB page only. ``include_notes`` is accepted for CLI compatibility.
    """
    del include_notes  # notes must not land in Extracted Tables / financial_tables
    slug = str(payload["company_slug"])
    year = int(payload["year"])
    name = str(payload.get("company_name") or slug)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report_group = f"Annual Report {year}"
    model = str(payload.get("extraction_model") or "extracted-tables")

    # Always strip any leftover note_* docs for this year.
    _purge_note_docs(db, slug, year)

    if not replace_core:
        db.companies.update_one(
            {"slug": slug},
            {
                "$set": {"name": name, "updated_at": now},
                "$setOnInsert": {"slug": slug, "created_at": now},
            },
            upsert=True,
        )
        return 0, 0

    db.financial_tables.delete_many(
        {
            "company_slug": slug,
            "year": year,
            "report_type": "annual",
            "statement_key": {
                "$in": ["income_statement", "oci", "sofp", "cash_flows"]
            },
        }
    )
    docs: list[dict] = []
    filled = 0
    missing = 0

    for statement in payload.get("statements") or []:
        if not statement.get("ok"):
            missing += 1
            continue
        rows = statement.get("rows") or []
        filled += sum(1 for row in rows if row.get("style") == "data")
        key = str(statement.get("key") or "")
        if key.startswith("note_"):
            continue
        title = str(statement.get("title") or key)
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
                "statement_key": key,
                "statement_title": title,
                "statement_label": title,
                "table_index": 0,
                "caption": None,
                "preamble": "",
                "footnotes": "",
                "header_rows": statement.get("header_rows") or [],
                "rows": rows,
                "row_count": len(rows),
                "source_pdf": payload.get("source_pdf"),
                "source_page": (statement.get("pages") or [None])[0],
                "source_pages": statement.get("pages") or [],
                "note_column": statement.get("note_column"),
                "unit": statement.get("unit") or payload.get("unit") or "",
                "extraction_model": model,
                "extraction_status": "ok",
                "extracted_at": now,
                "uploaded_at": now,
            }
        )
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
    force_note_capture: bool = False,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = True,
    api_key: str | None = None,
    model: str = "gpt-5",
    option: str = "1",
    dpi: int = 150,
    pdf_by_year: dict[int, Path] | None = None,
) -> int:
    del force_note_capture, use_pdf_extract, option  # reserved / legacy
    # Prefer OpenAI note capture+extract whenever a key is available.
    use_openai_notes = bool(use_openai_notes)
    pdf_by_year = pdf_by_year or {}
    resolved_key = None
    try:
        import extraction_accuracy as acc

        resolved_key = acc.resolve_api_key(api_key)
    except Exception:
        resolved_key = api_key.strip() if api_key else None
    if resolved_key:
        use_openai_notes = True

    if not years:
        emit({"type": "error", "message": "No years selected"})
        emit({"type": "done", "ok": 0, "failed": 1})
        return 1

    import db_page_sync as sync

    emit(
        {
            "type": "start",
            "mode": "db-annual",
            "company_slug": company_slug,
            "years": years,
            "totalSteps": len(years),
            "model": model if (resolved_key and use_openai_notes) else "notes-pdfplumber",
            "openai": bool(resolved_key and use_openai_notes),
        }
    )
    emit_log(
        "Run DB Annual: reuse Extracted Tables FS; "
        + (
            f"capture note pages + OpenAI vision extract (model={model})"
            if resolved_key and use_openai_notes
            else "local PDF note extract (no API key)"
        )
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
                    "label": f"Annual report {year}",
                    "index": idx,
                    "totalSteps": len(years),
                }
            )
            try:
                payload = sync.financial_tables_to_printed_payload(
                    db,
                    company_slug,
                    year,
                    include_mongo_notes=False,
                )
                if payload is None:
                    raise FileNotFoundError(
                        f"Before Run DB Annual you must run Run Annual "
                        f"extraction for {company_slug} {year} so the main "
                        f"FS tables are available on the Extracted Tables page."
                    )

                stmts_ok = sum(
                    1 for s in (payload.get("statements") or []) if s.get("ok")
                )
                if stmts_ok == 0:
                    raise FileNotFoundError(
                        f"Before Run DB Annual you must run Run Annual "
                        f"extraction for {company_slug} {year} — Extracted "
                        f"Tables has no usable Income / OCI / SoFP / Cash Flows."
                    )

                if not payload.get("company_name"):
                    payload["company_name"] = _company_name(db, company_slug)

                emit_log(
                    f"  Reusing Extracted Tables FS for {year} "
                    f"({stmts_ok} statement(s) — same tables as ET page)"
                )

                pdf_path = pdf_by_year.get(year)
                if pdf_path is None or not Path(pdf_path).exists():
                    src = payload.get("source_pdf")
                    if src and Path(str(src)).exists():
                        pdf_path = Path(str(src))
                if pdf_path is None or not Path(pdf_path).exists():
                    pdf_path = resolve_annual_pdf(db, company_slug, year)
                if pdf_path is not None:
                    pdf_path = Path(pdf_path)

                if use_note_extract:
                    if pdf_path is None or not pdf_path.exists():
                        raise FileNotFoundError(
                            f"Annual PDF not found for notes ({company_slug} {year})"
                        )
                    from printed_statements import collect_note_refs

                    note_refs = collect_note_refs(
                        payload.get("statements") or []
                    )
                    emit_log(
                        f"  Note refs from ET tables: "
                        f"{', '.join(note_refs) if note_refs else '(none)'}"
                    )
                    payload["source_pdf"] = str(pdf_path)

                    def _note_log(msg: str, level: str = "info") -> None:
                        emit_log(msg, level=level)

                    if resolved_key and use_openai_notes:
                        from db_note_openai import extract_notes_openai_for_statements

                        pdf_notes = extract_notes_openai_for_statements(
                            pdf_path,
                            payload.get("statements") or [],
                            year=year,
                            company_slug=company_slug,
                            api_key=resolved_key,
                            model=model,
                            dpi=dpi,
                            log=_note_log,
                        )
                    else:
                        from printed_statements import extract_notes_only

                        emit_log(
                            f"  Local PDF note extract from {pdf_path.name} "
                            "(set OPENAI_API_KEY for capture + vision)…"
                        )
                        pdf_notes = extract_notes_only(
                            pdf_path,
                            payload.get("statements") or [],
                            year=year,
                        )
                        for note in pdf_notes.values():
                            if not isinstance(note, dict):
                                continue
                            pages = note.get("pages") or []
                            if pages:
                                emit_log(
                                    f"    Note {note.get('note_ref')}: "
                                    f"pages {pages} "
                                    f"({len(note.get('rows') or [])} rows)"
                                )
                    payload["notes"] = pdf_notes
                    payload["extraction_model"] = (
                        "extracted-tables+openai-notes"
                        if resolved_key and use_openai_notes
                        else "extracted-tables+pdf-notes"
                    )
                elif skip_existing and (payload.get("notes") or {}):
                    cells_skipped += len(payload.get("notes") or {})

                out = sync.write_printed_payload(payload)
                emit_log(f"  DB page file: {out}")

                filled, missing = _store_printed(
                    db,
                    payload,
                    include_notes=use_note_extract,
                    replace_core=False,
                )
                cells_filled += filled
                cells_missing += missing

                for statement in payload.get("statements") or []:
                    emit_log(
                        f"  {statement.get('title')}: "
                        f"{len(statement.get('rows') or [])} rows "
                        f"ok={statement.get('ok')}"
                    )
                note_n = len(payload.get("notes") or {})
                if note_n:
                    emit_log(f"  Notes available: {note_n}")
                elif use_note_extract:
                    emit_log(
                        "  No note refs found on main FS — notes empty",
                        level="warn",
                    )

                ok += 1
                emit(
                    {
                        "type": "stage-done",
                        "year": year,
                        "stage": "annual",
                        "label": f"Annual report {year}",
                        "ok": True,
                        "company": company_slug,
                        "cells_filled": filled,
                        "cells_missing": missing,
                        "cells_skipped_existing": 0,
                        "notes": note_n,
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
                emit_log(str(exc), level="error")
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
    ap.add_argument("--force", action="store_true", help="Re-extract note tables")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip when notes already present (best-effort)",
    )
    ap.add_argument(
        "--no-note-extract",
        action="store_true",
        help="Only sync FS from Extracted Tables; skip note PDF extract",
    )
    ap.add_argument(
        "--include-notes",
        action="store_true",
        help="Extract note tables (UI default)",
    )
    ap.add_argument("--force-note-capture", action="store_true")
    ap.add_argument("--no-force-note-capture", action="store_true")
    ap.add_argument("--use-openai-notes", action="store_true")
    ap.add_argument("--use-pdf-extract", action="store_true")
    ap.add_argument("--no-pdf-extract", action="store_true")
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--model", default="gpt-5")
    ap.add_argument("--option", choices=["1", "2"], default="1")
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args(argv)

    years: list[int] = []
    pdf_by_year: dict[int, Path] = {}
    if args.years_stdin:
        try:
            payload = json.load(sys.stdin)
            raw = payload.get("years") if isinstance(payload, dict) else payload
            if isinstance(raw, list):
                years = sorted({int(y) for y in raw}, reverse=True)
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list):
                from runner_common import DEMO_DATA_DIR, resolve_report_year

                for raw_item in items:
                    if not isinstance(raw_item, dict):
                        continue
                    rel = raw_item.get("rel_path")
                    company = str(raw_item.get("company") or "").strip()
                    group = str(raw_item.get("group") or "").strip()
                    file_name = str(raw_item.get("file_name") or "").strip()
                    report_type = (
                        str(raw_item.get("report_type") or "Annual").strip()
                        or "Annual"
                    )
                    src: Path | None = None
                    if rel:
                        src = (DEMO_DATA_DIR / str(rel)).resolve()
                    elif company and group and file_name:
                        src = (
                            DEMO_DATA_DIR
                            / company
                            / report_type
                            / group
                            / file_name
                        ).resolve()
                    if src is None or not src.exists():
                        continue
                    year = resolve_report_year(
                        group, src.parent.name, src.name
                    )
                    if year is None:
                        try:
                            year = int(raw_item.get("year"))  # type: ignore[arg-type]
                        except (TypeError, ValueError):
                            year = None
                    if year is not None:
                        pdf_by_year[int(year)] = src
                        if int(year) not in years:
                            years.append(int(year))
                years = sorted(set(years), reverse=True)
        except Exception as exc:
            emit({"type": "error", "message": f"Invalid years JSON: {exc!r}"})
            return 2
    else:
        years = _parse_years(args.years)

    use_notes = bool(args.include_notes) and not bool(args.no_note_extract)

    return run_annual_db(
        years,
        company_slug=args.company_slug,
        skip_existing=bool(args.skip_existing) and not args.force,
        use_note_extract=use_notes,
        force_note_capture=bool(args.force_note_capture)
        and not args.no_force_note_capture,
        use_openai_notes=args.use_openai_notes,
        use_pdf_extract=not args.no_pdf_extract,
        api_key=args.apikey,
        model=args.model,
        option=args.option,
        dpi=args.dpi,
        pdf_by_year=pdf_by_year,
    )


if __name__ == "__main__":
    raise SystemExit(main())
