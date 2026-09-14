"""
Native annual FS extraction for non-bank issuers.

Stores each company's own income statement, OCI, statement of financial
position, and cash-flow tables (plus their note tables) into
``financial_tables``. Description rows follow the printed report, not the
COMB bank template.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re

from comb_fs_pdf_extract import (
    _fitz_texts_cached,
    _notes_section_start,
    find_statement_pages,
    statement_kind_from_text,
)
from comb_note_extractor import (
    _company_display_name,
    _split_header_body,
    _words_table_rows,
    resolve_annual_pdf,
)
from comb_reconcile import parse_number
from company_notes_extract import capture_and_fill_company_notes
from generate_comb_model import DataExtractor

NATIVE_STATEMENTS: tuple[str, ...] = (
    "income_statement",
    "oci",
    "sofp",
    "cash_flows",
)

STATEMENT_TITLES: dict[str, str] = {
    "income_statement": "Income Statement / Statement of Profit or Loss",
    "oci": "Statement of Other Comprehensive Income",
    "sofp": "Statement of Financial Position",
    "cash_flows": "Statement of Cash Flows",
}

_MIN_BODY_ROWS = 5
_GLUED_NOTE_AMT = re.compile(
    r"^(\d{1,2}(?:\.\d{1,2})?)\s+("
    r"[\(\-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?"
    r"|[\(\-]?\d{4,}(?:\.\d+)?\)?"
    r"|-"
    r")$"
)
_LABEL_TRAILING_AMT = re.compile(
    r"^(.*\S)\s+(\(\d{1,3}(?:,\d{3})+(?:\.\d+)?\)|\d{1,3}(?:,\d{3})+(?:\.\d+)?)$"
)


def _page_starts_other_statement(statement_key: str, text: str) -> bool:
    """True when this page begins a different primary statement."""
    head = (text or "").lower()[:1200]
    kind = statement_kind_from_text(text)
    if kind and kind != statement_key:
        return True
    if statement_key == "income_statement":
        if re.search(r"statement of\s+(other\s+)?comprehensive income", head):
            return True
        if "other comprehensive income" in head[:900] and "revenue" not in head[:500]:
            return True
        if "non-current assets" in head and "as at" in head:
            return True
    if statement_key == "oci":
        if "non-current assets" in head and (
            "as at" in head or re.search(r"\bassets\b", head[:400])
        ):
            return True
        if re.search(r"\bassets\b", head[:350]) and "property, plant" in head:
            return True
        if "statement of financial position" in head:
            return True
        if "balance as at" in head and "stated" in head:
            return True
    if statement_key == "sofp":
        if "operating activities" in head:
            return True
        if "statement of changes in equity" in head:
            return True
        if "balance as at" in head and "stated capital" in head:
            return True
        if "notes to the financial statements" in head:
            return True
    if statement_key == "cash_flows":
        if "notes to the" in head and "financial statements" in head:
            return True
        if "statement of changes in equity" in head:
            return True
    return False


def _expand_pages(
    texts: list[str],
    start: int,
    statement_key: str,
    *,
    max_pages: int = 2,
) -> list[int]:
    out = [start]
    for page_num in range(start + 1, min(start + max_pages, len(texts) + 1)):
        if _page_starts_other_statement(statement_key, texts[page_num - 1]):
            break
        out.append(page_num)
    return out


def _body_amount_rows(body_rows: list[list[str]]) -> int:
    n = 0
    for row in body_rows:
        for cell in row[1:]:
            val = parse_number(cell)
            if val is not None and abs(val) >= 1_000:
                n += 1
                break
    return n


def _table_quality(
    statement_key: str,
    header_rows: list[list[str]],
    body_rows: list[list[str]],
) -> int:
    header_blob = " ".join(" ".join(str(c) for c in row) for row in header_rows[:8]).lower()
    body_labels = " ".join(str(row[0]) for row in body_rows[:30] if row).lower()
    score = len(body_rows) + _body_amount_rows(body_rows) * 3
    kind = statement_kind_from_text(header_blob)
    if kind == statement_key:
        score += 80
    elif kind and kind != statement_key:
        score -= 120
    if statement_key == "income_statement":
        if any(t in body_labels for t in ("revenue", "gross income", "interest income")):
            score += 50
        if "other comprehensive income" in header_blob and "profit or loss" not in header_blob:
            score -= 90
    elif statement_key == "oci":
        if "other comprehensive" in header_blob or "other comprehensive" in body_labels:
            score += 40
        if "profit" in body_labels and "other comprehensive" in body_labels:
            score += 30
        if "revenue" in body_labels and "cost of sales" in body_labels:
            score -= 80
        if "stated capital" in body_labels or "revaluation reserves" in body_labels:
            score -= 70
    elif statement_key == "sofp":
        if "total assets" in body_labels:
            score += 50
        if "property, plant" in body_labels or "property plant" in body_labels:
            score += 25
        if "revenue" in body_labels and "total assets" not in body_labels:
            score -= 60
        if "summarised" in header_blob or "associate" in body_labels:
            score -= 80
    elif statement_key == "cash_flows":
        if "cash" in body_labels and (
            "operating" in body_labels or "investing" in body_labels
        ):
            score += 50
    return score


def _split_glued_note_amounts(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
) -> tuple[list[list[str]], list[list[str]]]:
    """Split '5 19,693,286,605' into Note + amount so note capture can run."""
    hits = 0
    for row in body_rows[:40]:
        if len(row) >= 2 and _GLUED_NOTE_AMT.match(str(row[1]).strip()):
            hits += 1
    if hits < 3:
        return header_rows, body_rows

    new_body: list[list[str]] = []
    for row in body_rows:
        cells = [str(c) if c is not None else "" for c in row]
        if len(cells) < 2:
            new_body.append(cells)
            continue
        glued = _GLUED_NOTE_AMT.match(cells[1].strip())
        if glued:
            new_body.append([cells[0], glued.group(1), glued.group(2), *cells[2:]])
            continue
        trailing = _LABEL_TRAILING_AMT.match(cells[0].strip())
        if trailing and not cells[1].strip():
            new_body.append([trailing.group(1), "", trailing.group(2), *cells[2:]])
            continue
        new_body.append([cells[0], "", *cells[1:]])

    new_header: list[list[str]] = []
    inserted = False
    for row in header_rows:
        cells = [str(c) if c is not None else "" for c in row]
        if not inserted and any("note" in c.lower() for c in cells):
            new_header.append(
                [cells[0] if cells else "", "Note", *cells[1:]]
            )
            inserted = True
        else:
            pad = [cells[0] if cells else "", ""]
            new_header.append(pad + cells[1:])
    if not inserted:
        new_header.append(["", "Note"])
    return new_header, new_body


def _align_year_headers(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
) -> list[list[str]]:
    """Rebuild Group/Company year banners so current-year maps to the first amount column."""
    max_cols = max((len(r) for r in body_rows), default=0)
    if max_cols < 4:
        return header_rows
    cy, py = str(year), str(year - 1)
    entity = [""] * max_cols
    years_row = [""] * max_cols
    years_row[1] = "Note"
    entity[2] = "Group"
    years_row[2] = cy
    years_row[3] = py
    if max_cols > 4:
        entity[4] = "Company"
        years_row[4] = cy
    if max_cols > 5:
        years_row[5] = py
    return [entity, years_row]


def _extract_pages_table(pdf, page_nums: list[int]) -> tuple[list[list[str]], list[list[str]]]:
    combined: list[list[str]] = []
    for page_num in page_nums:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        raw = _words_table_rows(pdf.pages[page_num - 1])
        if raw:
            combined.extend(raw)
    if not combined:
        return [], []
    header_rows, body_rows = _split_header_body(combined)
    return _split_glued_note_amounts(header_rows, body_rows)


def extract_native_statement(
    pdf_path: Path,
    statement_key: str,
    year: int | None = None,
) -> dict[str, Any] | None:
    try:
        import pdfplumber
    except ImportError:
        return None

    candidates = find_statement_pages(
        pdf_path, statement_key, max_pages=8, generic=True
    )
    if not candidates:
        return None

    texts = _fitz_texts_cached(pdf_path)
    if not texts:
        return None

    notes_start = _notes_section_start(texts)
    if notes_start:
        windowed = [
            p
            for p in candidates
            if notes_start - 25 <= p < notes_start
        ]
        if windowed:
            candidates = windowed

    extra = 3 if statement_key in {"sofp", "cash_flows"} else 2
    best: dict[str, Any] | None = None
    best_score = -1
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for start in candidates[:4]:
                pages = _expand_pages(
                    texts, start, statement_key, max_pages=extra
                )
                header_rows, body_rows = _extract_pages_table(pdf, pages)
                if len(body_rows) < _MIN_BODY_ROWS:
                    continue
                score = _table_quality(statement_key, header_rows, body_rows)
                if score > best_score:
                    best_score = score
                    best = {
                        "header_rows": header_rows,
                        "body_rows": body_rows,
                        "pages": pages,
                        "score": score,
                    }
    except Exception:
        return None

    if not best or best_score < 10:
        return None
    if year:
        best["header_rows"] = _align_year_headers(
            best["header_rows"], best["body_rows"], year
        )
    return best


def _row_docs(body_rows: list[list[str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in body_rows:
        cells = [str(c) if c is not None else "" for c in row]
        if not any(c.strip() for c in cells):
            continue
        label = cells[0].strip() if cells else ""
        style = "total" if label.lower().startswith("total") else "data"
        rows.append({"cells": cells, "style": style})
    return rows


def _upsert_statement(
    db,
    *,
    company_slug: str,
    company_name: str,
    year: int,
    statement_key: str,
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    pdf_path: Path,
    pages: list[int],
) -> int:
    report_group = f"Annual Report {year}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = _row_docs(body_rows)
    doc = {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": year,
        "report_type": "annual",
        "report_group": report_group,
        "report_key": report_group,
        "quarter": None,
        "period_label": None,
        "statement_key": statement_key,
        "statement_title": STATEMENT_TITLES.get(
            statement_key, statement_key.replace("_", " ").title()
        ),
        "statement_label": STATEMENT_TITLES.get(statement_key),
        "table_index": 0,
        "caption": None,
        "preamble": "",
        "footnotes": "",
        "header_rows": [[str(c) for c in row] for row in header_rows],
        "rows": rows,
        "row_count": len(rows),
        "source_pdf": str(pdf_path),
        "source_page": pages[0] if pages else None,
        "source_pages": pages,
        "extraction_model": "pdfplumber-native",
        "extraction_status": "ok",
        "extracted_at": now,
        "uploaded_at": now,
    }
    db.financial_tables.delete_many(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "statement_key": statement_key,
        }
    )
    db.financial_tables.insert_one(doc)
    return len(rows)


def extract_native_annual(
    db,
    company_slug: str,
    year: int,
    *,
    use_note_extract: bool = True,
    force_note_capture: bool = True,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = True,
    skip_existing: bool = False,
) -> dict[str, Any]:
    """Extract native FS tables + notes for one company-year."""
    pdf_path = resolve_annual_pdf(db, company_slug, year)
    company_name = _company_display_name(db, company_slug)
    statements: dict[str, Any] = {}
    cells_filled = 0
    cells_missing = 0

    if not pdf_path or not pdf_path.exists():
        print(f"  [native] Annual PDF not found for {company_slug} {year}", flush=True)
        return {
            "ok": False,
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "cells_filled": 0,
            "cells_missing": 0,
            "cells_skipped_existing": 0,
            "error": "annual_pdf_not_found",
            "pdf_used": None,
            "validation": {},
        }

    print(f"  [native] {company_slug} {year} from {pdf_path.name}", flush=True)
    db.companies.update_one(
        {"slug": company_slug},
        {
            "$set": {
                "name": company_name,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            "$setOnInsert": {
                "slug": company_slug,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        },
        upsert=True,
    )

    for statement_key in NATIVE_STATEMENTS:
        extracted = extract_native_statement(pdf_path, statement_key, year=year)
        if not extracted:
            print(f"  [native] {statement_key}: not found", flush=True)
            statements[statement_key] = {"ok": False, "rows": 0}
            cells_missing += 1
            continue
        n_rows = _upsert_statement(
            db,
            company_slug=company_slug,
            company_name=company_name,
            year=year,
            statement_key=statement_key,
            header_rows=extracted["header_rows"],
            body_rows=extracted["body_rows"],
            pdf_path=pdf_path,
            pages=extracted["pages"],
        )
        filled_rows = _body_amount_rows(extracted["body_rows"])
        cells_filled += filled_rows
        print(
            f"  [native] {statement_key}: {n_rows} rows "
            f"(pages {extracted['pages']}, score {extracted['score']})",
            flush=True,
        )
        statements[statement_key] = {
            "ok": True,
            "rows": n_rows,
            "pages": extracted["pages"],
            "score": extracted["score"],
        }

    # Count values the preview indexer can actually look up.
    try:
        ext = DataExtractor(db, company_slug)
        index = ext.index_for_year(year)
        if index:
            cells_filled = len(index)
    except Exception:
        pass

    note_fill: dict[str, Any] = {}
    if use_note_extract:
        print(f"  [native] Capture + wrap-join notes for {year}…", flush=True)
        note_fill = capture_and_fill_company_notes(
            db,
            company_slug,
            year,
            force=force_note_capture,
            pdf_path=pdf_path,
        )
        print(
            f"  [native] Notes planned={note_fill.get('notes_planned', 0)} "
            f"captured={note_fill.get('notes_captured', 0)} "
            f"filled={note_fill.get('note_tables_filled', 0)}",
            flush=True,
        )

    ok = any(v.get("ok") for v in statements.values())
    return {
        "ok": ok,
        "company_slug": company_slug,
        "year": year,
        "report_type": "annual",
        "cells_filled": cells_filled,
        "cells_missing": cells_missing,
        "cells_skipped_existing": 0,
        "pdf_used": str(pdf_path),
        "statements": statements,
        "note_tables": note_fill,
        "note_capture": note_fill.get("capture") if note_fill else {},
        "validation": {},
        "native": True,
    }
