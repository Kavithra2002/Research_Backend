"""
Resolve which FS / Drivers line items have note tables and their note references.

Note refs come from the Note column already captured in financial_tables
(income statement, SoFP, etc.) — the same data Demo_run extracted from the
annual report.  Each Drivers parent group inherits its parent's note ref
(e.g. Interest income → 13.1) so children are not confused with SoFP lines.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from comb_manifest import load_manifest
from generate_comb_model import norm_label, parse_number

NOTE_STMT_PRIORITY = ("income_statement", "sofp", "cash_flows", "oci")


def _note_col_index(doc: dict) -> int | None:
    """Return column index of the Note column, if present."""
    for hrow in doc.get("header_rows") or []:
        for ci, cell in enumerate(hrow):
            if str(cell).strip().lower() in {"note", "notes", "ref"}:
                return ci
    rows = doc.get("rows") or []
    if not rows:
        return None
    sample = rows[0].get("cells") if isinstance(rows[0], dict) else None
    if not sample or len(sample) < 2:
        return None
    # Demo_run income statement: col 1 is Note when col 0 is label
    if str(sample[1]).strip() and re.match(
        r"^[\d\.]+$", str(sample[1]).strip().replace(" ", "")
    ):
        return 1
    return None


NOTE_REF_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")
PAGE_NO_RE = re.compile(r"^\d{2,3}$")
NOTE_REF_EXTRACT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)")


def _normalize_note_raw(note: str) -> str:
    """Strip common prefixes/suffixes from Note column OCR text."""
    text = str(note or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\(?\s*note\s*", "", text, flags=re.I)
    text = text.rstrip(")").strip()
    return text


def _extract_note_ref_token(note: str) -> str | None:
    """Pull a note ref from free-form Note column text."""
    raw = _normalize_note_raw(note)
    if not raw:
        return None
    compact = raw.replace(" ", "")
    if NOTE_REF_RE.match(compact):
        return compact
    match = NOTE_REF_EXTRACT_RE.search(raw)
    if not match:
        return None
    token = match.group(1)
    return token if NOTE_REF_RE.match(token) else None


def _detect_note_page_columns(rows: list[list[str]]) -> tuple[int | None, int | None]:
    """Infer Note and Page No. column indices from PDF word rows."""
    if not rows:
        return None, None
    max_cols = max(len(row) for row in rows[:40]) if rows else 0
    best_note_ci: int | None = None
    best_page_ci: int | None = None
    best_hits = 0
    for note_ci in range(1, min(5, max_cols)):
        note_hits = 0
        page_hits = 0
        for row in rows[:45]:
            if len(row) <= note_ci:
                continue
            token = _extract_note_ref_token(str(row[note_ci]))
            if not token:
                continue
            note_hits += 1
            if note_ci + 1 < len(row) and PAGE_NO_RE.match(str(row[note_ci + 1]).strip()):
                page_hits += 1
        if note_hits > best_hits:
            best_hits = note_hits
            best_note_ci = note_ci
            best_page_ci = note_ci + 1 if page_hits >= max(2, note_hits // 3) else None
    if best_hits < 2:
        return None, None
    return best_note_ci, best_page_ci


def _split_glued_note_page(note: str) -> tuple[str, int | None]:
    """
    Recover note + page when OCR merged them (e.g. ``6.52`` -> note ``6``, page ``52``).
    """
    raw = str(note or "").replace(" ", "").strip()
    match = re.fullmatch(r"(\d{1,2})\.(\d{2})", raw)
    if not match:
        return raw, None
    suffix = int(match.group(2))
    if suffix < 10:
        return raw, None
    return match.group(1), suffix


def _parse_note_and_page(note_raw: str, page_raw: str | None = None) -> tuple[str | None, int | None]:
    """Parse Note column text into (note_ref, printed_page)."""
    note = _normalize_note_raw(note_raw)
    if not note or note.lower() in {"note", "notes", "ref"}:
        return None, None

    page_no: int | None = None
    if page_raw and PAGE_NO_RE.match(str(page_raw).strip()):
        try:
            page_no = int(str(page_raw).strip())
        except ValueError:
            page_no = None

    spaced = re.match(r"^(\d{1,2}(?:\.\d{1,2})?)\s+(\d{2,3})$", note)
    if spaced:
        return spaced.group(1), int(spaced.group(2))

    compact = note.replace(" ", "")
    token = _extract_note_ref_token(note)
    if not token:
        return None, None

    split_note, glued_page = _split_glued_note_page(compact)
    if glued_page is not None and page_no is None:
        return split_note, glued_page
    return token, page_no


def _page_col_index(doc: dict) -> int | None:
    """Return column index of the Page No. column, if present."""
    for hrow in doc.get("header_rows") or []:
        for ci, cell in enumerate(hrow):
            low = str(cell).strip().lower()
            if low in {"page no.", "page no", "page", "pg"}:
                return ci
    note_ci = _note_col_index(doc)
    if note_ci is None:
        return None
    rows = doc.get("rows") or []
    for row in rows[:40]:
        cells = row.get("cells") if isinstance(row, dict) else []
        if not cells or note_ci + 1 >= len(cells):
            continue
        candidate = str(cells[note_ci + 1]).strip()
        if PAGE_NO_RE.match(candidate):
            return note_ci + 1
    return None


def _label_note_page_from_doc(doc: dict) -> dict[str, dict[str, Any]]:
    """Map normalized label -> note ref, optional page no., and statement key."""
    out: dict[str, dict[str, Any]] = {}
    note_ci = _note_col_index(doc)
    if note_ci is None:
        return out
    page_ci = _page_col_index(doc)
    statement_key = str(doc.get("statement_key") or "")
    for row in doc.get("rows") or []:
        cells = row.get("cells") if isinstance(row, dict) else []
        if not cells or note_ci >= len(cells):
            continue
        label = str(cells[0]).strip()
        note_raw = str(cells[note_ci]).strip()
        if not label:
            continue
        page_raw = None
        if page_ci is not None and page_ci < len(cells):
            page_raw = str(cells[page_ci]).strip()
        note, page_no = _parse_note_and_page(note_raw, page_raw)
        if not note:
            continue
        out[norm_label(label)] = {
            "note_ref": note,
            "page_no": page_no,
            "statement_key": statement_key,
            "label": label,
        }
    return out


def _label_note_from_doc(doc: dict) -> dict[str, str]:
    """Map normalized label -> note ref from one financial_tables document."""
    return {
        nl: info["note_ref"]
        for nl, info in _label_note_page_from_doc(doc).items()
    }


def build_label_note_index(
    db,
    company_slug: str,
    year: int,
    *,
    pdf_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """norm_label -> {note_ref, page_no, statement_key, label} from FS tables."""
    index: dict[str, dict[str, Any]] = {}
    docs = list(
        db.financial_tables.find(
            {
                "company_slug": company_slug,
                "year": year,
                "report_type": "annual",
            }
        )
    )
    for stmt in NOTE_STMT_PRIORITY:
        for doc in docs:
            if doc.get("statement_key") != stmt:
                continue
            for nl, info in _label_note_page_from_doc(doc).items():
                if nl not in index:
                    index[nl] = {**info, "label": _original_label(doc, nl)}
    for doc in docs:
        sk = doc.get("statement_key") or ""
        if sk in NOTE_STMT_PRIORITY or sk.startswith("note_"):
            continue
        for nl, info in _label_note_page_from_doc(doc).items():
            if nl not in index:
                index[nl] = {**info, "label": _original_label(doc, nl)}

    if pdf_path and pdf_path.exists():
        for nl, info in build_fs_label_note_index_from_pdf(pdf_path).items():
            existing = index.get(nl)
            if existing:
                if info.get("page_no") and not existing.get("page_no"):
                    existing["page_no"] = info["page_no"]
                if info.get("note_ref") and not existing.get("note_ref"):
                    existing["note_ref"] = info["note_ref"]
                if (
                    info.get("page_no")
                    and info.get("note_ref")
                    and existing.get("note_ref") == info.get("note_ref")
                ):
                    existing["page_no"] = info["page_no"]
            else:
                index[nl] = info
    return index


def build_fs_label_note_index_from_pdf(pdf_path: Path) -> dict[str, dict[str, Any]]:
    """
    Authoritative Note + Page No. from annual report financial statement tables.

    Parses income statement, SoFP, cash flows, and OCI pages via pdfplumber.
    """
    from comb_fs_pdf_extract import find_statement_pages
    from comb_note_extractor import _words_table_rows

    index: dict[str, dict[str, Any]] = {}
    if not pdf_path.exists():
        return index

    try:
        import pdfplumber
    except ImportError:
        return index

    for statement_key in NOTE_STMT_PRIORITY:
        pages = find_statement_pages(pdf_path, statement_key, max_pages=8)
        if not pages:
            continue
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page_num in pages:
                    if page_num < 1 or page_num > len(pdf.pages):
                        continue
                    rows = _words_table_rows(pdf.pages[page_num - 1])
                    if not rows:
                        continue
                    note_ci, page_ci = _detect_note_page_columns(rows)
                    if note_ci is None:
                        note_ci = 1
                        page_ci = None
                        for row in rows[:25]:
                            if len(row) < 3:
                                continue
                            if NOTE_REF_RE.match(str(row[1]).strip()):
                                if PAGE_NO_RE.match(str(row[2]).strip()):
                                    page_ci = 2
                                break
                    for row in rows:
                        if not row or not str(row[0]).strip():
                            continue
                        if len(row) <= note_ci:
                            continue
                        note_raw = str(row[note_ci]).strip()
                        label = str(row[0]).strip()
                        if not label:
                            continue
                        page_raw = None
                        if page_ci is not None and page_ci < len(row):
                            page_raw = str(row[page_ci]).strip()
                        note, page_no = _parse_note_and_page(note_raw, page_raw)
                        if not note:
                            continue
                        nl = norm_label(label)
                        index[nl] = {
                            "note_ref": note,
                            "page_no": page_no,
                            "statement_key": statement_key,
                            "label": label,
                        }
        except Exception:
            continue
    return index


def build_fs_row_note_map(
    manifest: dict,
    label_notes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map each manifest FS data label -> note metadata (ref, page, has_note_table)."""
    out: dict[str, dict[str, Any]] = {}
    for row in manifest.get("fs", {}).get("rows") or []:
        if row.get("kind") != "data":
            continue
        label = str(row["label"])
        info = label_notes.get(norm_label(label))
        if not info or not info.get("note_ref"):
            continue
        out[label] = {
            "note_ref": str(info["note_ref"]),
            "page_no": info.get("page_no"),
            "statement_key": info.get("statement_key"),
            "fs_label": info.get("label") or label,
            "has_note_table": True,
            "note_statement_key": _note_statement_key(str(info["note_ref"])),
        }
    return out


def _original_label(doc: dict, norm_lbl: str) -> str:
    note_ci = _note_col_index(doc)
    if note_ci is None:
        return norm_lbl
    for row in doc.get("rows") or []:
        cells = row.get("cells") if isinstance(row, dict) else []
        if cells and norm_label(str(cells[0])) == norm_lbl:
            return str(cells[0]).strip()
    return norm_lbl


def _drivers_row_to_fs_label(manifest: dict) -> dict[int, str]:
    links = manifest.get("fs", {}).get("driver_links") or {}
    return {int(row): lbl for lbl, row in links.items()}


def build_note_capture_plan(
    db,
    company_slug: str,
    year: int,
    *,
    manifest: dict | None = None,
    pdf_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    One entry per unique note ref found on FS statement lines (Note + Page No.).

    Uses financial_tables and, when provided, the annual PDF statement tables.
    Drivers parent groups are kept for breakdown metadata when they match a note.
    """
    manifest = manifest or load_manifest()
    label_notes = build_label_note_index(
        db, company_slug, year, pdf_path=pdf_path
    )
    row_to_fs = _drivers_row_to_fs_label(manifest)
    drivers_by_ref: dict[str, dict[str, Any]] = {}

    for group in manifest.get("drivers", {}).get("groups") or []:
        parent_label = group["parent_label"]
        parent_row = int(group.get("parent_row") or 0)
        children = group.get("children") or []
        if not children:
            continue
        fs_label = row_to_fs.get(parent_row) or parent_label
        note_info = label_notes.get(norm_label(fs_label)) or label_notes.get(
            norm_label(parent_label)
        )
        if note_info and note_info.get("note_ref"):
            drivers_by_ref.setdefault(str(note_info["note_ref"]), {
                "parent_label": parent_label,
                "parent_row": parent_row,
                "children": children,
                "fs_label": fs_label,
            })

    by_ref: dict[str, dict[str, Any]] = {}
    for info in label_notes.values():
        note_ref = str(info.get("note_ref") or "").strip()
        if not note_ref:
            continue
        fs_label = str(info.get("label") or "")
        page_no = info.get("page_no")
        existing = by_ref.get(note_ref)
        if existing and existing.get("page_no") and not page_no:
            continue
        if existing and page_no and not existing.get("page_no"):
            pass
        elif existing and not page_no:
            continue
        drv = drivers_by_ref.get(note_ref, {})
        by_ref[note_ref] = {
            "parent_label": drv.get("parent_label") or fs_label,
            "fs_label": drv.get("fs_label") or fs_label,
            "parent_row": drv.get("parent_row"),
            "children": drv.get("children") or [],
            "note_ref": note_ref,
            "page_no": page_no,
            "statement_key": info.get("statement_key"),
            "has_note_table": True,
            "note_statement_key": _note_statement_key(note_ref),
        }

    plan = sorted(by_ref.values(), key=lambda item: str(item.get("note_ref") or ""))

    for group in manifest.get("drivers", {}).get("groups") or []:
        parent_label = group["parent_label"]
        parent_row = int(group.get("parent_row") or 0)
        children = group.get("children") or []
        if not children:
            continue
        fs_label = row_to_fs.get(parent_row) or parent_label
        note_info = label_notes.get(norm_label(fs_label)) or label_notes.get(
            norm_label(parent_label)
        )
        if note_info and note_info.get("note_ref"):
            continue
        plan.append(
            {
                "parent_label": parent_label,
                "fs_label": fs_label,
                "parent_row": parent_row,
                "children": children,
                "note_ref": None,
                "statement_key": None,
                "has_note_table": False,
            }
        )

    return plan


def _note_statement_key(note_ref: str) -> str:
    safe = re.sub(r"[^\w\.]+", "_", note_ref.strip()).strip("_").replace(".", "_")
    return f"note_{safe}"


def parent_value_from_statements(
    db,
    company_slug: str,
    year: int,
    fs_label: str,
    *,
    entity_column: str = "group",
) -> float | None:
    """Read parent total from income_statement / SoFP Bank column for year."""
    from generate_comb_model import DataExtractor, find_value_col

    target = norm_label(fs_label)
    if not target:
        return None

    # Exact / careful statement match first — avoids fuzzy DataExtractor hits
    # like "financial assets" matching the wrong FS line.
    for stmt in NOTE_STMT_PRIORITY:
        doc = db.financial_tables.find_one(
            {
                "company_slug": company_slug,
                "year": year,
                "report_type": "annual",
                "statement_key": stmt,
            }
        )
        if not doc:
            continue
        col = _bank_year_col(doc, year, entity_column)
        if col is None:
            col = find_value_col(doc, year)
        if col is None:
            continue
        exact: float | None = None
        fuzzy: float | None = None
        for row in doc.get("rows") or []:
            cells = row.get("cells") or []
            if not cells:
                continue
            row_norm = norm_label(str(cells[0]))
            if col >= len(cells):
                continue
            val = parse_number(cells[col])
            if val is None:
                continue
            if row_norm == target:
                exact = val
                break
            # Only allow long-label containment (avoids short false positives).
            if (
                fuzzy is None
                and len(target) >= 24
                and (target in row_norm or row_norm in target)
            ):
                fuzzy = val
        if exact is not None:
            return exact
        if fuzzy is not None:
            return fuzzy

    # Fallback: template alias lookup (longer labels only).
    if len(target) >= 18:
        ext = DataExtractor(db, company_slug)
        val = ext.lookup(year, fs_label)
        if val is not None:
            return val
    return None


def _entity_year_col_from_statement(
    doc: dict, year: int, entity_column: str = "group"
) -> int | None:
    """Find entity + year column in statement tables (GROUP/BANK layout)."""
    from comb_note_extractor import _entity_year_col

    headers = doc.get("header_rows") or []
    body_rows = [
        [str(c) for c in (row.get("cells") if isinstance(row, dict) else row or [])]
        for row in doc.get("rows") or []
    ]
    return _entity_year_col(headers, body_rows, year, entity_column)


def _bank_year_col(doc: dict, year: int, entity_column: str) -> int | None:
    return _entity_year_col_from_statement(doc, year, entity_column)
