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
    for row in rows[:20]:
        sample = row.get("cells") if isinstance(row, dict) else None
        if not sample or len(sample) < 2:
            continue
        token = str(sample[1]).strip().replace(" ", "")
        if token and re.match(r"^[\d\.]+$", token) and _is_plausible_note_ref(token):
            return 1
    return None


NOTE_REF_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")
PAGE_NO_RE = re.compile(r"^\d{2,3}$")
NOTE_REF_EXTRACT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)")
_AMOUNT_CELL_RE = re.compile(
    r"^[\(\-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?$|^[\(\-]?\d{5,}(?:\.\d+)?\)?$"
)
_YEAR_CELL_RE = re.compile(r"^(19|20)\d{2}$")
_STATEMENT_PAGE_MARKERS = {
    "income_statement": ("income statement", "statement of profit"),
    "sofp": ("statement of financial position",),
    "cash_flows": ("statement of cash flows",),
    "oci": ("other comprehensive income",),
}


def _normalize_note_raw(note: str) -> str:
    """Strip common prefixes/suffixes from Note column OCR text."""
    text = str(note or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\(?\s*note\s*", "", text, flags=re.I)
    text = text.rstrip(")").strip()
    return text


def _is_plausible_note_ref(token: str) -> bool:
    """True for COMB notes (12, 13.1), not Change % (21.82) or amounts."""
    text = str(token or "").strip()
    if not NOTE_REF_RE.match(text):
        return False
    if "." not in text:
        try:
            return 1 <= int(text) <= 80
        except ValueError:
            return False
    left, right = text.split(".", 1)
    try:
        major = int(left)
    except ValueError:
        return False
    if not (1 <= major <= 80):
        return False
    if len(right) >= 2:
        # 21.82 / 2.07 / 11.00 are YoY Change % cells, not note numbers.
        if right[0] == "0":
            return False
        try:
            frac = int(right)
        except ValueError:
            return False
        if frac == 0 or frac > 19:
            return False
    return True


def _extract_note_ref_token(note: str) -> str | None:
    """Pull a note ref from free-form Note column text."""
    raw = _normalize_note_raw(note)
    if not raw:
        return None
    compact = raw.replace(" ", "")
    # Amounts and years are not note numbers (e.g. 341,566,200 or 2023).
    if _AMOUNT_CELL_RE.match(compact) or _YEAR_CELL_RE.match(compact):
        return None
    if "," in raw or raw.count("0") >= 4:
        return None
    if NOTE_REF_RE.match(compact) and _is_plausible_note_ref(compact):
        return compact
    match = NOTE_REF_EXTRACT_RE.search(raw)
    if not match:
        return None
    token = match.group(1)
    return token if _is_plausible_note_ref(token) else None


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
    Recover note + page when OCR merged a 3-digit page (``12.302`` -> 12, 302).

    Two-digit suffixes like ``21.82`` are Change % values, not page 82.
    """
    raw = str(note or "").replace(" ", "").strip()
    match = re.fullmatch(r"(\d{1,2})\.(\d{3})", raw)
    if not match:
        return raw, None
    suffix = int(match.group(2))
    if 100 <= suffix <= 500:
        return match.group(1), suffix
    return raw, None


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


def _page_header_blob(rows: list[list[str]], n: int = 10) -> str:
    parts: list[str] = []
    for row in rows[:n]:
        parts.extend(str(c) for c in row)
    return " ".join(parts).lower()


def _page_matches_statement_key(rows: list[list[str]], statement_key: str) -> bool:
    blob = _page_header_blob(rows)
    markers = _STATEMENT_PAGE_MARKERS.get(statement_key) or ()
    return any(marker in blob for marker in markers)


def _page_looks_like_statement_grid(
    rows: list[list[str]],
    note_ci: int | None,
    page_ci: int | None,
) -> bool:
    """True when the page has a Note column on a primary-statement grid."""
    if note_ci is None:
        return False
    blob = _page_header_blob(rows, 8)
    if "note" not in blob:
        return False
    if page_ci is not None and "page" in blob:
        hits = 0
        for row in rows[:55]:
            if len(row) <= max(note_ci, page_ci):
                continue
            token = _extract_note_ref_token(str(row[note_ci]))
            if not token:
                continue
            page_txt = str(row[page_ci]).strip()
            if not PAGE_NO_RE.match(page_txt):
                continue
            try:
                page_no = int(page_txt)
            except ValueError:
                continue
            if page_no >= 100:
                hits += 1
        if hits >= 4:
            return True
    # Holding-company statements often have a Note column but no Page No.
    note_hits = 0
    amount_hits = 0
    for row in rows[:55]:
        if len(row) <= note_ci:
            continue
        if _extract_note_ref_token(str(row[note_ci])):
            note_hits += 1
        for cell in row[note_ci + 1 :]:
            v = parse_number(cell)
            if v is not None and abs(v) >= 1_000:
                amount_hits += 1
                break
    return note_hits >= 4 and amount_hits >= 4


def _note_info_quality(info: dict[str, Any]) -> int:
    """Higher is better. Prefers real note+page pairs over Change % leftovers."""
    score = 0
    note = str(info.get("note_ref") or "")
    if _is_plausible_note_ref(note):
        score += 10
    else:
        score -= 20
    try:
        page_i = int(info["page_no"]) if info.get("page_no") is not None else None
    except (TypeError, ValueError):
        page_i = None
    if page_i is not None and page_i >= 100:
        score += 25
    elif page_i is not None and 40 <= page_i <= 99:
        score += 2
    sk = str(info.get("statement_key") or "")
    score += {
        "income_statement": 6,
        "sofp": 5,
        "oci": 3,
        "cash_flows": 2,
    }.get(sk, 0)
    return score


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
        from generate_comb_model import COMMERCIAL_BANK_SLUG

        generic = company_slug != COMMERCIAL_BANK_SLUG
        for nl, info in build_fs_label_note_index_from_pdf(
            pdf_path, generic=generic
        ).items():
            existing = index.get(nl)
            if existing and _note_info_quality(existing) > _note_info_quality(info):
                continue
            if existing:
                # Printed statement Note + Page No. columns win over OCR leftovers.
                if info.get("note_ref"):
                    existing["note_ref"] = info["note_ref"]
                    if info.get("statement_key"):
                        existing["statement_key"] = info["statement_key"]
                if info.get("page_no"):
                    existing["page_no"] = info["page_no"]
                if info.get("label"):
                    existing["label"] = info["label"]
            else:
                index[nl] = info
    return index


def build_fs_label_note_index_from_pdf(
    pdf_path: Path,
    *,
    generic: bool = False,
) -> dict[str, dict[str, Any]]:
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
        pages = find_statement_pages(
            pdf_path, statement_key, max_pages=8, generic=generic
        )
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
                    if not _page_matches_statement_key(rows, statement_key):
                        continue
                    note_ci, page_ci = _detect_note_page_columns(rows)
                    if note_ci is None:
                        note_ci = 1
                        page_ci = None
                        for row in rows[:25]:
                            if len(row) < 3:
                                continue
                            if _is_plausible_note_ref(str(row[1]).strip()):
                                if PAGE_NO_RE.match(str(row[2]).strip()):
                                    page_ci = 2
                                break
                    if not _page_looks_like_statement_grid(rows, note_ci, page_ci):
                        continue
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
                        if not note or not _is_plausible_note_ref(note):
                            continue
                        nl = norm_label(label)
                        info = {
                            "note_ref": note,
                            "page_no": page_no,
                            "statement_key": statement_key,
                            "label": label,
                        }
                        prev = index.get(nl)
                        if prev and _note_info_quality(prev) >= _note_info_quality(info):
                            continue
                        index[nl] = info
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
        candidate = {
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
        if existing and _note_info_quality(existing) > _note_info_quality(candidate):
            continue
        by_ref[note_ref] = candidate

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
