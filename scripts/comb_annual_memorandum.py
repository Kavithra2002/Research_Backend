"""
Memorandum information rows (employees, customer service centres).

These appear at the bottom of the annual SoFP with GROUP often blank and
COMPANY/BANK populated. Values are small integers (not Rs.'000), so they
need dedicated parsing — separate from standard FS amount extraction.
"""
from __future__ import annotations

from typing import Any

from comb_note_extractor import (
    find_annual_bank_body_col,
    find_annual_group_body_col,
    norm_label,
)
from extraction_aliases_store import patterns_for_label
from generate_comb_model import DataExtractor, LABEL_ALIASES, parse_number

MEMORANDUM_FS_LABELS = frozenset(
    {
        "number of employees",
        "number of customer service centres",
    }
)

MEMORANDUM_ENTITY_COLUMNS = ("group", "bank")


def is_memorandum_fs_label(label: str) -> bool:
    return norm_label(label) in MEMORANDUM_FS_LABELS


def is_plausible_memorandum_count(label: str, value: float | None) -> bool:
    if value is None:
        return False
    av = abs(value)
    if av <= 0 or av >= 10_000_000:
        return False
    nl = norm_label(label)
    if "employee" in nl:
        return 10 <= av <= 500_000
    if "customer service" in nl or "service centre" in nl:
        return 1 <= av <= 20_000
    return 1 <= av <= 1_000_000


def lookup_label_in_memorandum_index(
    index: dict[str, float],
    template_label: str,
) -> float | None:
    patterns = patterns_for_label(
        template_label, LABEL_ALIASES, "fs", default_to_label=True
    )
    for pat in patterns:
        np = norm_label(pat)
        if np in index:
            return index[np]
        for key, val in index.items():
            if np in key or key in np:
                return val
    nt = norm_label(template_label)
    if nt in index:
        return index[nt]
    for key, val in index.items():
        if nt in key or key in nt:
            return val
    return None


def index_memorandum_values_from_rows(
    body_rows: list[list[str]],
    value_col: int,
    *,
    unit_scale: float = 1.0,
) -> dict[str, float]:
    """Label→value index with multi-line rows and small-integer amounts."""
    index: dict[str, float] = {}
    pending_label = ""
    in_memorandum = False

    for row in body_rows:
        if not row:
            continue
        col0 = (row[0] or "").strip()

        if col0:
            nl0 = norm_label(col0)
            if nl0 == norm_label("Memorandum information"):
                in_memorandum = True
                pending_label = ""
                continue
            if is_memorandum_fs_label(col0):
                pending_label = col0
            elif in_memorandum:
                # Inside memorandum block — keep pending_label until a value is stored.
                pass
            else:
                pending_label = ""

        if value_col >= len(row):
            continue
        val = parse_number(row[value_col])
        if val is None or not pending_label:
            continue
        if not is_plausible_memorandum_count(pending_label, val):
            continue

        nl = norm_label(pending_label)
        if nl and nl not in index:
            index[nl] = val * unit_scale
        pending_label = ""

    return index


def _doc_body_rows(doc: dict) -> tuple[list[list[str]], list[list[str]]]:
    headers = doc.get("header_rows") or []
    body_rows: list[list[str]] = []
    for row in doc.get("rows") or []:
        cells = row.get("cells") if isinstance(row, dict) else row
        if cells:
            body_rows.append([str(c) for c in cells])
    return headers, body_rows


def extract_memorandum_from_doc(
    doc: dict,
    year: int,
    template_label: str,
    *,
    entity_column: str = "group",
) -> float | None:
    headers, body_rows = _doc_body_rows(doc)
    if entity_column.lower() == "bank":
        col = find_annual_bank_body_col(headers, body_rows, year)
    else:
        col = find_annual_group_body_col(headers, body_rows, year, entity_column)
    if col is None:
        return None

    stmt = doc.get("statement_key") or ""
    unit_scale = 1000.0 if stmt == "ten_year_summary" else 1.0
    index = index_memorandum_values_from_rows(body_rows, col, unit_scale=unit_scale)
    val = lookup_label_in_memorandum_index(index, template_label)
    if val is not None and is_plausible_memorandum_count(template_label, val):
        return val
    return None


def lookup_memorandum_from_tables(
    fs_ext: DataExtractor,
    year: int,
    template_label: str,
) -> float | None:
    """
    Find memorandum count from stored tables — try GROUP then COMPANY/BANK,
    across all annual statement documents (SoFP is the usual source).
    """
    if not is_memorandum_fs_label(template_label):
        return None

    docs = fs_ext._docs_for_year(year)
    # Prefer balance sheet / SoFP pages where memorandum block lives.
    docs = sorted(
        docs,
        key=lambda d: (
            0 if str(d.get("statement_key") or "").startswith("sofp") else 1,
            -len(d.get("rows") or []),
        ),
    )

    for entity in MEMORANDUM_ENTITY_COLUMNS:
        for doc in docs:
            val = extract_memorandum_from_doc(
                doc, year, template_label, entity_column=entity
            )
            if val is not None:
                return val
    return None


def lookup_memorandum_from_pdf_index(
    pdf_index,
    template_label: str,
    section: str | None = None,
) -> float | None:
    """Search GROUP and COMPANY/BANK columns across statement PDF indexes."""
    if not is_memorandum_fs_label(template_label):
        return None

    sections: list[str | None]
    if section:
        sections = [section, None]
    else:
        sections = [None]

    for entity in MEMORANDUM_ENTITY_COLUMNS:
        for sec in sections:
            val = pdf_index.lookup(
                template_label, section=sec, entity_column=entity
            )
            if val is not None and is_plausible_memorandum_count(
                template_label, val
            ):
                return val
    return None


def memorandum_plumber_index_from_pages(
    pdf_path,
    page_nums: list[int],
    year: int,
    entity_column: str,
) -> dict[str, float]:
    """PDF word-grid index for memorandum counts (small integers, multi-line rows)."""
    try:
        import pdfplumber
    except ImportError:
        return {}

    from comb_note_extractor import (
        _split_header_body,
        _words_table_rows,
    )

    if not page_nums or pdf_path is None:
        return {}

    combined: list[list[str]] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num in page_nums:
                if page_num < 1 or page_num > len(pdf.pages):
                    continue
                raw = _words_table_rows(pdf.pages[page_num - 1])
                if raw:
                    combined.extend(raw)
    except Exception:
        return {}

    if not combined:
        return {}

    header_rows, body_rows = _split_header_body(combined)
    if entity_column.lower() == "bank":
        col = find_annual_bank_body_col(header_rows, body_rows, year)
    else:
        col = find_annual_group_body_col(header_rows, body_rows, year, entity_column)
    if col is None:
        return {}
    return index_memorandum_values_from_rows(body_rows, col)


def merge_memorandum_into_pdf_index(
    base_index: dict[str, float],
    pdf_path,
    pages: list[int],
    year: int,
    entity_column: str,
) -> dict[str, float]:
    """Overlay memorandum label counts onto a pdfplumber statement index."""
    out = dict(base_index or {})
    mem = memorandum_plumber_index_from_pages(pdf_path, pages, year, entity_column)
    for key, val in mem.items():
        if key in MEMORANDUM_FS_LABELS or any(m in key for m in MEMORANDUM_FS_LABELS):
            out[key] = val
    return out
