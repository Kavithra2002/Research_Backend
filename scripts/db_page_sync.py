"""
db_page_sync.py
===============
Shared bridge: Extracted Tables (Mongo ``financial_tables``) → DB page files.

The DB page shows the same main FS as Extracted Tables. Run DB Annual only
adds clickable note tables on top — it does not re-extract income / OCI /
SoFP / cash flows.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CORE_KEYS = ("income_statement", "oci", "sofp", "cash_flows")
# Match Extracted Tables tab labels exactly.
CORE_TITLES = {
    "income_statement": "Income Statement",
    "oci": "Other Comprehensive Income (OCI)",
    "sofp": "Statement of Financial Position",
    "cash_flows": "Statement of Cash Flows",
}
_NOTE_REF = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")


def build_printed_payload_from_openai(
    results: dict[str, Any],
    *,
    company_slug: str,
    company_name: str,
    year: int,
    source_pdf: str | Path,
    pages_json: Path | None = None,
    include_notes: bool = False,
    accuracy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert step3 results into the printed_statements JSON the DB page reads.

    Kept for Run Annual sync; Run DB Annual uses ``financial_tables_to_printed``.
    """
    import extraction_accuracy as acc

    pages_by_key: dict[str, list[int]] = {}
    titles: dict[str, str] = {}
    if pages_json and pages_json.exists():
        try:
            manifest = json.loads(pages_json.read_text(encoding="utf-8"))
            for key, info in (manifest.get("statements") or {}).items():
                if not isinstance(info, dict):
                    continue
                pages_by_key[str(key)] = list(info.get("pdf_pages_1based") or [])
                titles[str(key)] = str(info.get("title") or key)
        except Exception:
            pass

    payload = acc.openai_results_to_printed_payload(
        results,
        company_slug=company_slug,
        company_name=company_name,
        year=year,
        source_pdf=str(source_pdf),
        pages_by_key=pages_by_key,
        titles=titles,
    )
    payload["extraction_model"] = "openai-vision"
    if accuracy is not None:
        payload["accuracy"] = accuracy

    if include_notes:
        try:
            from printed_statements import extract_notes_only

            notes = extract_notes_only(
                Path(source_pdf),
                payload.get("statements") or [],
                year=year,
            )
            payload["notes"] = notes
        except Exception as exc:
            payload.setdefault("notes", {})
            payload.setdefault("notes_error", str(exc))

    return payload


def write_printed_payload(payload: dict[str, Any]) -> Path:
    """Write ``backend/extracted/printed_statements/<slug>/<year>.json``."""
    from printed_statements import default_output_path

    slug = str(payload.get("company_slug") or "company")
    year = int(payload["year"])
    out = default_output_path(slug, year)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def sync_openai_annual_to_db_page(
    results: dict[str, Any],
    *,
    company_slug: str,
    company_name: str,
    year: int,
    source_pdf: str | Path,
    pages_json: Path | None = None,
    include_notes: bool = False,
    accuracy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build + write printed payload; returns (payload, path)."""
    payload = build_printed_payload_from_openai(
        results,
        company_slug=company_slug,
        company_name=company_name,
        year=year,
        source_pdf=source_pdf,
        pages_json=pages_json,
        include_notes=include_notes,
        accuracy=accuracy,
    )
    path = write_printed_payload(payload)
    return payload, path


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _row_note_ref(row: dict[str, Any], note_column: int | None) -> str | None:
    existing = str(row.get("note_ref") or "").strip()
    if existing and _NOTE_REF.fullmatch(existing):
        return existing
    cells = row.get("cells") or []
    idx = note_column if isinstance(note_column, int) else 1
    if idx < 0 or idx >= len(cells):
        return None
    cand = _cell_str(cells[idx]).strip()
    if _NOTE_REF.fullmatch(cand):
        return cand
    return None


def _mongo_doc_to_printed_statement(doc: dict[str, Any], key: str) -> dict[str, Any]:
    note_column = doc.get("note_column")
    if not isinstance(note_column, int):
        note_column = None
    rows_out: list[dict[str, Any]] = []
    note_hits = 0
    for row in doc.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cells = [_cell_str(c) for c in (row.get("cells") or [])]
        note_ref = _row_note_ref(row, note_column)
        if note_ref:
            note_hits += 1
        rows_out.append(
            {
                "cells": cells,
                "note_ref": note_ref,
                "style": str(row.get("style") or "data"),
            }
        )
    if note_column is None and note_hits >= 2:
        note_column = 1
    pages = list(doc.get("source_pages") or [])
    if not pages and doc.get("source_page") is not None:
        try:
            pages = [int(doc["source_page"])]
        except (TypeError, ValueError):
            pages = []
    # Prefer the shared ET label so DB statement options match Extracted Tables.
    title = CORE_TITLES.get(key) or str(
        doc.get("statement_title")
        or doc.get("statement_label")
        or key
    )
    return {
        "key": key,
        "title": title,
        "pages": pages,
        "header_rows": doc.get("header_rows") or [],
        "rows": rows_out,
        "note_column": note_column,
        "unit": str(doc.get("unit") or ""),
        "ok": len(rows_out) >= 1,
        "extraction_model": str(doc.get("extraction_model") or "extracted-tables"),
    }


def _mongo_note_doc_to_printed(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    title = str(doc.get("statement_title") or doc.get("statement_label") or "")
    # "Note 5 — REVENUE" → title "REVENUE"
    m = re.match(r"(?i)^note\s+\S+\s*[—\-:]\s*(.+)$", title.strip())
    short = m.group(1).strip() if m else title
    pages = list(doc.get("source_pages") or [])
    if not pages and doc.get("source_page") is not None:
        try:
            pages = [int(doc["source_page"])]
        except (TypeError, ValueError):
            pages = []
    rows_out: list[dict[str, Any]] = []
    for row in doc.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows_out.append(
            {
                "cells": [_cell_str(c) for c in (row.get("cells") or [])],
                "note_ref": row.get("note_ref"),
                "style": str(row.get("style") or "data"),
            }
        )
    return {
        "note_ref": ref,
        "title": short or f"Note {ref}",
        "pages": pages,
        "header_rows": doc.get("header_rows") or [],
        "rows": rows_out,
        "note_column": doc.get("note_column"),
        "unit": str(doc.get("unit") or ""),
        "ok": len(rows_out) > 0,
    }


def financial_tables_to_printed_payload(
    db: Any,
    company_slug: str,
    year: int,
    *,
    include_mongo_notes: bool = True,
) -> dict[str, Any] | None:
    """Build DB-page printed payload from Extracted Tables Mongo docs.

    Returns None when no core FS tables exist for that company/year.
    """
    statements: list[dict[str, Any]] = []
    company_name = company_slug.replace("_", " ")
    source_pdf = ""
    unit = ""
    found_any = False

    for key in CORE_KEYS:
        doc = db.financial_tables.find_one(
            {
                "company_slug": company_slug,
                "year": int(year),
                "report_type": "annual",
                "statement_key": key,
            }
        )
        if not doc:
            statements.append(
                {
                    "key": key,
                    "title": CORE_TITLES[key],
                    "pages": [],
                    "header_rows": [],
                    "rows": [],
                    "note_column": None,
                    "unit": "",
                    "ok": False,
                }
            )
            continue
        found_any = True
        if doc.get("company_name"):
            company_name = str(doc["company_name"]).strip() or company_name
        if doc.get("source_pdf") and not source_pdf:
            source_pdf = str(doc["source_pdf"])
        printed = _mongo_doc_to_printed_statement(doc, key)
        if printed.get("unit") and not unit:
            unit = str(printed["unit"])
        statements.append(printed)

    if not found_any:
        return None

    notes: dict[str, Any] = {}
    if include_mongo_notes:
        note_docs = db.financial_tables.find(
            {
                "company_slug": company_slug,
                "year": int(year),
                "report_type": "annual",
                "statement_key": {"$regex": r"^note_"},
            }
        )
        for doc in note_docs:
            key = str(doc.get("statement_key") or "")
            ref = key[5:] if key.startswith("note_") else ""
            if not ref:
                continue
            notes[ref] = _mongo_note_doc_to_printed(doc, ref)

    return {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": int(year),
        "source_pdf": source_pdf,
        "unit": unit,
        "statements": statements,
        "notes": notes,
        "extraction_model": "extracted-tables",
    }


def sync_extracted_year_to_db_page(
    db: Any,
    company_slug: str,
    year: int,
    *,
    include_mongo_notes: bool = True,
    extract_notes_from_pdf: bool = False,
    pdf_path: Path | str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Write printed JSON for one year from Extracted Tables (+ optional notes)."""
    payload = financial_tables_to_printed_payload(
        db,
        company_slug,
        year,
        include_mongo_notes=include_mongo_notes,
    )
    if payload is None:
        raise FileNotFoundError(
            f"No Extracted Tables for {company_slug} {year}. "
            "Run Annual for this company/year first."
        )

    resolved_pdf: Path | None = None
    if pdf_path and Path(pdf_path).exists():
        resolved_pdf = Path(pdf_path)
    elif payload.get("source_pdf") and Path(str(payload["source_pdf"])).exists():
        resolved_pdf = Path(str(payload["source_pdf"]))

    if extract_notes_from_pdf:
        if resolved_pdf is None:
            try:
                from comb_note_extractor import resolve_annual_pdf

                resolved = resolve_annual_pdf(db, company_slug, year)
                if resolved and Path(resolved).exists():
                    resolved_pdf = Path(resolved)
            except Exception:
                resolved_pdf = None
        if resolved_pdf is None:
            payload.setdefault("notes", payload.get("notes") or {})
            payload["notes_error"] = f"Annual PDF not found for notes ({year})"
        else:
            from printed_statements import extract_notes_only

            payload["source_pdf"] = str(resolved_pdf)
            pdf_notes = extract_notes_only(
                resolved_pdf,
                payload.get("statements") or [],
                year=year,
            )
            # Prefer freshly extracted notes; keep any Mongo notes not re-found.
            merged = dict(payload.get("notes") or {})
            merged.update(pdf_notes)
            payload["notes"] = merged

    path = write_printed_payload(payload)
    return payload, path


def sync_company_from_extracted_tables(
    db: Any,
    company_slug: str,
    *,
    years: list[int] | None = None,
    extract_notes_from_pdf: bool = False,
) -> list[dict[str, Any]]:
    """Fill DB page printed JSON for all (or given) Extracted Tables years."""
    if years is None:
        years = sorted(
            db.financial_tables.distinct(
                "year",
                {
                    "company_slug": company_slug,
                    "report_type": "annual",
                    "statement_key": {"$in": list(CORE_KEYS)},
                },
            ),
            reverse=True,
        )
    results: list[dict[str, Any]] = []
    for year in years:
        try:
            payload, path = sync_extracted_year_to_db_page(
                db,
                company_slug,
                int(year),
                include_mongo_notes=True,
                extract_notes_from_pdf=extract_notes_from_pdf,
            )
            results.append(
                {
                    "year": int(year),
                    "ok": True,
                    "path": str(path),
                    "notes": len(payload.get("notes") or {}),
                    "statements_ok": sum(
                        1 for s in (payload.get("statements") or []) if s.get("ok")
                    ),
                }
            )
        except Exception as exc:
            results.append({"year": int(year), "ok": False, "error": str(exc)})
    return results
