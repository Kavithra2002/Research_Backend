"""
Extract the primary financial statements as printed, plus the note tables
those statements point at.

Layouts differ by issuer:
  * Bank reports (Commercial Bank): ruled tables with Note, Page No.,
    Group / Bank, year columns, and change %.
  * Other issuers (Ambeon Holdings): word-position tables with Note and
    Group / Company year columns.

Usage:
    python printed_statements.py --company-slug Ambeon_Holdings_PLC --year 2020
    python printed_statements.py --company-slug Commercial_Bank_of_Ceylon_PLC --year 2020
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pdfplumber

from comb_fs_pdf_extract import _looks_like_toc
from comb_note_extractor import (
    _split_header_body,
    _words_table_rows,
    resolve_annual_pdf,
)
from comb_workbook_store import get_db
from extract_native_annual import _extract_pages_table

try:
    import fitz
except ImportError:
    fitz = None

STATEMENT_ORDER = ("income_statement", "oci", "sofp", "cash_flows")
STATEMENT_TITLES = {
    "income_statement": "Income Statement / Statement of Profit or Loss",
    "oci": "Statement of Comprehensive Income",
    "sofp": "Statement of Financial Position",
    "cash_flows": "Statement of Cash Flows",
}

_NOTE_REF = r"\d{1,2}(?:\.\d{1,2})?"
_PAGE_REF = r"\d{2,3}"
_COMMA_AMT = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?")
_AMOUNTISH = re.compile(
    r"^(?:[–—\-]|[\(\-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|[\(\-]?\d+\.\d+\)?)$"
)
_GLUE_NOTE_PAGE_AMT = re.compile(
    rf"^({_NOTE_REF})\s+({_PAGE_REF})\s+(.+)$"
)
_GLUE_NOTE_AMT = re.compile(rf"^({_NOTE_REF})\s+(.+)$")
_NOTE_HEADING = re.compile(
    rf"(?m)^[ \t]*({_NOTE_REF})(?:\.|\s)\s*([A-Z][^\n]{{2,90}})"
)
_FOOTER = re.compile(
    r"(figures in brackets|integral part of|annual report|independent auditor|"
    r"the board of directors|accounting policies and notes|form an integral)",
    re.I,
)
_FURNITURE = re.compile(
    r"^(notes to the financial statements?|statements|lkr(?:\s+lkr)+|"
    r"rs\.?\s*'?0+|rs\.?\s*000)$",
    re.I,
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\n", " ")
    text = text.replace("\u2013", "–").replace("\u2014", "—")
    return re.sub(r"\s+", " ", text).strip()


def _looks_amount(value: str) -> bool:
    text = _clean(value).replace(" ", "")
    if not text:
        return False
    return bool(_AMOUNTISH.match(text) or _COMMA_AMT.search(text))


def _has_comma_amount(value: str) -> bool:
    return bool(_COMMA_AMT.search(_clean(value)))


def _note_key(ref: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in str(ref).split("."):
        if part.isdigit():
            parts.append(int(part))
    return tuple(parts) or (0,)


def _is_descendant(parent: str, child: str) -> bool:
    pp, cp = _note_key(parent), _note_key(child)
    return len(cp) > len(pp) and cp[: len(pp)] == pp


def _page_texts(pdf_path: Path) -> list[str]:
    if fitz is None:
        return []
    with fitz.open(str(pdf_path)) as doc:
        return [doc[i].get_text("text") or "" for i in range(len(doc))]


def _notes_section_page(texts: list[str]) -> int | None:
    """1-based page where numbered notes begin (not the contents list)."""
    hits: list[int] = []
    for idx, raw in enumerate(texts):
        head = raw[:2200]
        low = head.lower()
        if re.search(r"(?i)\b1\.\s+(reporting entity|corporate information)\b", head):
            hits.append(idx + 1)
            continue
        if "notes to the financial" in low[:700] and re.search(
            r"(?m)^\s*1\.\s+[A-Z]", head
        ):
            if "contents" in low[:400]:
                continue
            hits.append(idx + 1)
    return hits[-1] if hits else None


def _classify_page(text: str) -> str | None:
    """Primary statement printed on this page, if any."""
    if not text or _looks_like_toc(text):
        return None
    low = text.lower()
    head = low[:1600]
    if (
        "independent auditor" in head
        or "auditors' report" in head
        or "auditors’ report" in head
        or "key audit matter" in head
    ):
        return None
    if "financial performance" in head or "highlights" in head[:900]:
        return None
    if "notes to the financial" in head[:500]:
        return "notes"
    if "statement of changes in equity" in head or (
        "changes in equity" in head[:400] and "stated capital" in low[:2500]
    ):
        return "soce"
    if "statement of cash flows" in head or "cash flow statement" in head:
        return "cash_flows"
    if "statement of profit or loss and other comprehensive income" in low[:2200]:
        return "oci"
    if re.search(r"statement of\s+(other\s+)?comprehensive income", head):
        if "profit or loss" not in head[:500]:
            return "oci"
    if re.search(r"\bincome statement\b", head) and "other comprehensive" not in head[:700]:
        return "income_statement"
    if "statement of profit or loss" in head and "comprehensive" not in head[:800]:
        return "income_statement"
    if "statement of financial position" in head or re.search(r"\bbalance sheet\b", head):
        return "sofp"

    amounts = len(_COMMA_AMT.findall(text))
    if amounts < 8:
        return None
    if (
        "investing activities" in head
        or "financing activities" in head
        or (
            "operating activities" in head
            and (
                "investing activities" in low
                or "financing activities" in low
                or "profit before tax" in low
            )
        )
    ):
        return "cash_flows"
    if "as at" in head and "cash and cash equivalents" in head and "page" in head:
        return "sofp"
    if (
        ("non-current assets" in head or re.search(r"\bassets\b", head[:500]))
        and "property, plant" in low
        and "revenue" not in head[:700]
    ):
        return "sofp"
    if "other comprehensive income" in head and "revenue" not in head[:800]:
        return "oci"
    if "note" in head and "revenue" in head and (
        "cost of sales" in low or "gross profit" in low or "continuing operations" in head
    ):
        return "income_statement"
    if "gross income" in head and "interest income" in head:
        return "income_statement"
    if "statement of financial position" in low and "total assets" in low and "as at" in head:
        return "sofp"
    return None


def _continues_statement(text: str, kind: str) -> bool:
    if not text or _looks_like_toc(text):
        return False
    other = _classify_page(text)
    if other and other != kind:
        return False
    if "notes to the financial" in text.lower()[:400]:
        return False
    return len(_COMMA_AMT.findall(text)) >= 6


def locate_statement_pages(texts: list[str]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    notes_at = _notes_section_page(texts)
    window_lo = (notes_at - 20) if notes_at else 1
    window_hi = (notes_at - 1) if notes_at else len(texts)
    i = max(0, window_lo - 1)
    while i < window_hi:
        kind = _classify_page(texts[i])
        if kind in STATEMENT_ORDER and kind not in found:
            pages = [i + 1]
            j = i + 1
            while j < len(texts) and len(pages) < 3:
                if not _continues_statement(texts[j], kind):
                    break
                pages.append(j + 1)
                j += 1
            found[kind] = pages
            i = max(j, i + 1)
            continue
        i += 1
    return found


def _split_leading(cell: str) -> tuple[str, str, str]:
    """Split a cell that may contain 'note page amount'."""
    text = _clean(cell)
    if not text:
        return "", "", ""
    glued = _GLUE_NOTE_PAGE_AMT.match(text)
    if glued and _looks_amount(glued.group(3)):
        return glued.group(1), glued.group(2), _clean(glued.group(3))
    glued_note = _GLUE_NOTE_AMT.match(text)
    if glued_note and _looks_amount(glued_note.group(2)):
        return glued_note.group(1), "", _clean(glued_note.group(2))
    if re.fullmatch(_NOTE_REF, text) and not _COMMA_AMT.search(text):
        return text, "", ""
    return "", "", text


def _section_like(label: str) -> bool:
    text = _clean(label)
    if not text or re.search(r"\d", text):
        return False
    if text.endswith(":"):
        return True
    if re.match(
        r"(?i)^(assets|liabilities|equity|adjustments for|cash flows from|"
        r"less\b|add\b|continuing operations|discontinued|other comprehensive income|"
        r"items that |earnings per share|memorandum information)\b",
        text,
    ):
        return True
    return False


def _parse_ruled_rows(raw_rows: list[list[Any]]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    width = 0
    for raw in raw_rows:
        cells = [_clean(c) for c in raw]
        if not any(cells):
            continue
        label = cells[0] if cells else ""
        note, page, amounts = "", "", []
        rest = cells[1:]
        if rest:
            note, page, rem = _split_leading(rest[0])
            if note:
                if rem:
                    amounts.append(rem)
                amounts.extend(_clean(c) for c in rest[1:])
            else:
                amounts = [_clean(c) for c in rest]
        width = max(width, len(amounts))
        parsed.append(
            {"label": label, "note": note, "page": page, "amounts": amounts}
        )

    for row in parsed:
        row["amounts"] = row["amounts"] + [""] * (width - len(row["amounts"]))

    for idx in range(len(parsed) - 1):
        cur = parsed[idx]
        nxt = parsed[idx + 1]
        if not nxt["label"] or not _section_like(cur["label"]):
            continue
        moved = kept = 0
        for a_val, b_val in zip(cur["amounts"], nxt["amounts"]):
            if a_val and not b_val:
                moved += 1
            elif a_val and b_val:
                kept += 1
        if moved < 1 or kept or not any(nxt["amounts"]):
            continue
        for pos, (a_val, b_val) in enumerate(zip(cur["amounts"], nxt["amounts"])):
            if a_val and not b_val:
                nxt["amounts"][pos] = a_val
                cur["amounts"][pos] = ""
        if cur["note"] and not nxt["note"]:
            nxt["note"], cur["note"] = cur["note"], ""
        if cur["page"] and not nxt["page"]:
            nxt["page"], cur["page"] = cur["page"], ""
    return parsed


def _row_style(label: str, amounts: list[str], note: str) -> str:
    text = label.strip().lower()
    if not text and not note and not any(amounts):
        return "blank"
    if text.startswith("total") or text.startswith("profit for the year") or text.startswith(
        "profit for the period"
    ):
        return "total"
    if not any(amounts) and not note:
        return "section"
    return "data"


def _is_footer_blob(blob: str) -> bool:
    return bool(_FOOTER.search(blob))


def _skip_ruled_row(label: str, note: str, amounts: list[str]) -> bool:
    blob = " ".join([label, note, *amounts]).strip()
    low = blob.lower()
    if not blob:
        return True
    if _is_footer_blob(low):
        return True
    if re.match(r"(?i)^(for the year ended|as at )\b", label):
        return True
    if label.lower() in {"note", "page no.", "page no", "page"} and not any(amounts):
        return True
    if re.fullmatch(r"(?i)(group|bank|company)", label) and not any(amounts):
        return True
    compact = re.sub(r"[^a-z0-9]", "", low)
    if compact and not _COMMA_AMT.search(blob) and re.fullmatch(r"(?:rs|lkr|000)+", compact):
        return True
    return False


def _entity_name(page_text: str) -> str:
    low = page_text.lower()
    if re.search(r"\bbank\b", low):
        return "Bank"
    return "Company"


def _unit_label(page_text: str) -> str:
    low = page_text.lower().replace("’", "'").replace("‘", "'")
    if re.search(r"rs\.?\s*'?0{3}|'000|rs\s*000", low):
        return "Rs. '000"
    if "lkr" in low:
        return "LKR"
    if "rs" in low:
        return "Rs."
    return ""


def _years_from_text(page_text: str, fallback: int) -> tuple[int, int]:
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", page_text[:2500])]
    uniq: list[int] = []
    for year in years:
        if year not in uniq:
            uniq.append(year)
    if len(uniq) >= 2:
        current, prior = uniq[0], uniq[1]
        if prior > current:
            current, prior = prior, current
        return current, prior
    return fallback, fallback - 1


def _header_has_change(raw_rows: list[list[Any]]) -> bool:
    blob = " ".join(_clean(c) for row in raw_rows[:3] for c in row).lower()
    return "change" in blob


def _build_ruled_headers(
    *,
    entity: str,
    year: int,
    prior: int,
    has_page: bool,
    has_change: bool,
    amount_count: int,
) -> list[list[str]]:
    if has_page and has_change and amount_count >= 6:
        return [
            ["", "", "", "Group", "", "", entity, "", ""],
            [
                "",
                "Note",
                "Page",
                str(year),
                str(prior),
                "Change %",
                str(year),
                str(prior),
                "Change %",
            ],
        ]
    if has_page and amount_count >= 4:
        return [
            ["", "", "", "Group", "", entity, ""],
            ["", "Note", "Page", str(year), str(prior), str(year), str(prior)],
        ]
    labels = ["", "Note"]
    group = ["", ""]
    # Pair remaining amounts as year, prior, [change] repeating.
    rest = amount_count
    entities = ["Group", entity]
    slot = 0
    while rest > 0:
        who = entities[min(slot, len(entities) - 1)]
        take = 3 if has_change and rest >= 3 else min(2, rest)
        group.extend([who] + [""] * (take - 1))
        if take == 3:
            labels.extend([str(year), str(prior), "Change %"])
        else:
            labels.extend([str(year), str(prior)][:take])
        rest -= take
        slot += 1
    return [group, labels]


def ruled_statement_from_pages(
    pdf,
    pages: list[int],
    *,
    year: int,
    page_text: str,
) -> dict[str, Any] | None:
    raw_rows: list[list[Any]] = []
    for page_num in pages:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        page = pdf.pages[page_num - 1]
        tables = page.extract_tables() or []
        if not tables and len(page.lines) < 40:
            return None
        best = max(tables, key=lambda t: len(t), default=None)
        if best:
            raw_rows.extend(best)
    if len(raw_rows) < 8:
        return None
    parsed = _parse_ruled_rows(raw_rows)
    has_page = sum(1 for row in parsed if row["page"]) >= 3
    has_change = _header_has_change(raw_rows)
    amount_count = max((len(row["amounts"]) for row in parsed), default=0)
    current, prior = _years_from_text(page_text, year)
    body: list[dict[str, Any]] = []
    for row in parsed:
        if _skip_ruled_row(row["label"], row["note"], row["amounts"]):
            continue
        cells = [row["label"]]
        if any(r["note"] or r["page"] for r in parsed):
            cells.append(row["note"])
        if has_page:
            cells.append(row["page"])
        cells.extend(row["amounts"])
        style = _row_style(row["label"], row["amounts"], row["note"])
        if style == "blank":
            continue
        body.append(
            {
                "cells": cells,
                "note_ref": row["note"] or None,
                "style": style,
            }
        )
    if len(body) < 6:
        return None
    note_column = 1 if any(r.get("note_ref") for r in body) else None
    headers = _build_ruled_headers(
        entity=_entity_name(page_text),
        year=current,
        prior=prior,
        has_page=has_page,
        has_change=has_change,
        amount_count=amount_count,
    )
    return {
        "header_rows": headers,
        "rows": body,
        "note_column": note_column,
        "unit": _unit_label(page_text),
        "pages": pages,
    }


def _drop_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(cell.strip() for cell in (r[i] for r in padded))]
    if not keep:
        return padded
    return [[r[i] for i in keep] for r in padded]


def _leading_section_rows(header_rows: list[list[str]]) -> list[dict[str, Any]]:
    """Section labels that the grid parser parked in the header block."""
    found: list[dict[str, Any]] = []
    for row in header_rows:
        cells = [_clean(c) for c in row]
        label = cells[0] if cells else ""
        if not label or any(_has_comma_amount(c) for c in cells):
            continue
        if _section_like(label) or re.match(
            r"(?i)^(continuing operations|discontinued|operating activities|assets|equity and liabilities)$",
            label,
        ):
            width = max(len(cells), 1)
            found.append(
                {
                    "cells": [label] + [""] * (width - 1),
                    "note_ref": None,
                    "style": "section",
                }
            )
    return found


def _word_body_rows(header_rows: list[list[str]], body_rows: list[list[str]], year: int) -> dict[str, Any] | None:
    if len(body_rows) < 5:
        return None
    cleaned: list[dict[str, Any]] = _leading_section_rows(header_rows)
    for row in body_rows:
        cells = [_clean(c) for c in row]
        if not any(cells):
            continue
        blob = " ".join(cells)
        if _is_footer_blob(blob):
            continue
        label = cells[0]
        if re.fullmatch(r"\d{2,3}", label) and not any(_has_comma_amount(c) for c in cells[1:]):
            continue
        if re.search(r"\bplc\b", label, re.I) and not any(_has_comma_amount(c) for c in cells):
            continue
        if re.match(r"^\d{2,3}\s+", label) and not any(_has_comma_amount(c) for c in cells):
            continue
        if _FURNITURE.match(re.sub(r"\s+", " ", label).strip()):
            continue
        if re.match(r"(?i)^(group|company|bank)$", label) and not any(
            _has_comma_amount(c) for c in cells[1:]
        ):
            continue
        note = cells[1].strip() if len(cells) > 1 else ""
        note_ref = note if re.fullmatch(_NOTE_REF, note) else None
        amounts = cells[2:] if note_ref or (len(cells) > 1 and cells[1] == "") else cells[1:]
        style = _row_style(label, amounts, note_ref or "")
        cleaned.append({"cells": cells, "note_ref": note_ref, "style": style})
    if len(cleaned) < 5:
        return None
    width = max(len(r["cells"]) for r in cleaned)
    note_column = 1 if any(r["note_ref"] for r in cleaned) else None
    group = [""] * width
    years = [""] * width
    if note_column is not None and width > note_column:
        years[note_column] = "Note"
    amount_start = (note_column + 1) if note_column is not None else 1
    pairs = ["Group", "Company"]
    cursor = amount_start
    pair_i = 0
    while cursor < width and pair_i < len(pairs):
        group[cursor] = pairs[pair_i]
        years[cursor] = str(year)
        if cursor + 1 < width:
            years[cursor + 1] = str(year - 1)
        cursor += 2
        pair_i += 1
    return {
        "header_rows": [group, years],
        "rows": cleaned,
        "note_column": note_column,
    }


def word_statement_from_pages(pdf, pages: list[int], *, year: int) -> dict[str, Any] | None:
    header_rows, body_rows = _extract_pages_table(pdf, pages)
    parsed = _word_body_rows(header_rows, body_rows, year)
    if not parsed:
        return None
    parsed["pages"] = pages
    return parsed


def extract_statement(
    pdf,
    texts: list[str],
    kind: str,
    pages: list[int],
    *,
    year: int,
) -> dict[str, Any] | None:
    page_text = "\n".join(texts[p - 1] for p in pages if 1 <= p <= len(texts))
    ruled = ruled_statement_from_pages(pdf, pages, year=year, page_text=page_text)
    chosen = ruled
    if chosen is None:
        chosen = word_statement_from_pages(pdf, pages, year=year)
        if chosen is not None:
            chosen["unit"] = _unit_label(page_text)
    if chosen is None:
        return None
    chosen["key"] = kind
    chosen["title"] = STATEMENT_TITLES[kind]
    chosen.setdefault("unit", _unit_label(page_text))
    return chosen


def _index_note_headings(texts: list[str], start_page: int) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for idx in range(max(0, start_page - 1), len(texts)):
        for match in _NOTE_HEADING.finditer(texts[idx]):
            ref = match.group(1)
            title = _clean(match.group(2)).rstrip(" .")
            if len(title) < 3:
                continue
            headings.append(
                {
                    "note_ref": ref,
                    "title": title,
                    "page": idx + 1,
                    "pos": match.start(),
                }
            )
    # First occurrence of each ref wins (later repeats are continuations).
    first: dict[str, dict[str, Any]] = {}
    for item in headings:
        first.setdefault(item["note_ref"], item)
    return list(first.values())


def _note_page_span(
    headings: list[dict[str, Any]], ref: str, page_count: int
) -> tuple[list[int], str]:
    current = next((h for h in headings if h["note_ref"] == ref), None)
    if current is None:
        parent = ref.split(".")[0]
        current = next((h for h in headings if h["note_ref"] == parent), None)
        ref = parent if current else ref
    if current is None:
        return [], ref
    title = str(current["title"])
    start = int(current["page"])
    end = min(page_count, start + 5)
    for other in headings:
        if int(other["page"]) < start:
            continue
        other_ref = str(other["note_ref"])
        if other_ref == ref or _is_descendant(ref, other_ref):
            continue
        if _note_key(other_ref) <= _note_key(ref):
            continue
        end = min(end, int(other["page"]))
        break
    if end == start:
        end = start
    pages = list(range(start, end + (0 if end > start else 1)))
    # If the next note begins on `end`, keep that page only when it is the start
    # page; otherwise stop on the previous page.
    if pages and pages[-1] != start:
        next_on_last = any(
            h["page"] == pages[-1] and h["note_ref"] != ref and not _is_descendant(ref, h["note_ref"])
            for h in headings
        )
        if next_on_last:
            pages = pages[:-1] or [start]
    return pages[:6], ref


def _trim_note_rows(rows: list[dict[str, Any]], ref: str, headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boundaries = []
    for item in headings:
        other = str(item["note_ref"])
        if other == ref or _is_descendant(ref, other):
            continue
        if _note_key(other) > _note_key(ref):
            boundaries.append(other)
    if not boundaries:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        label = _clean((row.get("cells") or [""])[0])
        hit = False
        for other in boundaries:
            if re.match(rf"^{re.escape(other)}\b", label):
                hit = True
                break
        if hit and kept:
            break
        kept.append(row)
    return kept


def _note_from_ruled(raw_rows: list[list[Any]], *, year: int, page_text: str) -> dict[str, Any] | None:
    parsed = _parse_ruled_rows(raw_rows)
    if len(parsed) < 3:
        return None
    has_page = sum(1 for row in parsed if row["page"]) >= 2
    has_change = _header_has_change(raw_rows)
    amount_count = max((len(row["amounts"]) for row in parsed), default=0)
    current, prior = _years_from_text(page_text, year)
    body: list[dict[str, Any]] = []
    for row in parsed:
        if _skip_ruled_row(row["label"], row["note"], row["amounts"]):
            continue
        if not row["label"] and not any(row["amounts"]):
            continue
        cells = [row["label"]]
        if any(r["note"] for r in parsed):
            cells.append(row["note"])
        if has_page:
            cells.append(row["page"])
        cells.extend(row["amounts"])
        body.append(
            {
                "cells": cells,
                "note_ref": row["note"] or None,
                "style": _row_style(row["label"], row["amounts"], row["note"]),
            }
        )
    if len(body) < 2:
        return None
    return {
        "header_rows": _build_ruled_headers(
            entity=_entity_name(page_text),
            year=current,
            prior=prior,
            has_page=has_page,
            has_change=has_change,
            amount_count=amount_count,
        ),
        "rows": body,
        "note_column": 1 if any(r.get("note_ref") for r in body) else None,
        "unit": _unit_label(page_text),
    }


def _note_from_words(pdf, pages: list[int], *, ref: str, title: str) -> dict[str, Any] | None:
    combined: list[list[str]] = []
    for page_num in pages:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        page = pdf.pages[page_num - 1]
        words = page.extract_words(use_text_flow=False) or []
        y0 = 0.0
        y1 = float(page.height)
        lines: list[list[dict]] = []
        for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
            if lines and abs(word["top"] - lines[-1][0]["top"]) < 3:
                lines[-1].append(word)
            else:
                lines.append([word])
        for line in lines:
            text = " ".join(w["text"] for w in line)
            if re.search(rf"(?i)\b{re.escape(ref)}\b", text) and title.split()[0].lower() in text.lower():
                y0 = max(0.0, float(line[0]["top"]) - 2)
                break
        for line in lines:
            text = " ".join(w["text"] for w in line).lower()
            if float(line[0]["top"]) <= y0 + 8:
                continue
            if "segment information" in text:
                y1 = float(line[0]["top"]) - 2
                break
        if y1 - y0 < 40:
            y1 = float(page.height)
        try:
            band = page.crop((0, y0, float(page.width), min(y1, float(page.height))))
        except Exception:
            band = page
        rows = _words_table_rows(band)
        combined.extend(rows)
    if not combined:
        return None
    tightened = _drop_empty_columns([[_clean(c) for c in row] for row in combined])
    header_rows, body_rows = _split_header_body(tightened)
    body: list[dict[str, Any]] = []
    for row in body_rows:
        cells = [_clean(c) for c in row]
        label = cells[0] if cells else ""
        blob = " ".join(cells)
        if label.lower().startswith("segment information"):
            break
        if _FURNITURE.match(re.sub(r"\s+", " ", label).strip()):
            continue
        if _is_footer_blob(blob):
            continue
        if not any(cells):
            continue
        note = ""
        if len(cells) > 1 and re.fullmatch(_NOTE_REF, cells[1]):
            note = cells[1]
        body.append(
            {
                "cells": cells,
                "note_ref": note or None,
                "style": _row_style(label, cells[1:], note),
            }
        )
    if len(body) < 1:
        return None
    matrix = _drop_empty_columns([r["cells"] for r in body])
    for row, cells in zip(body, matrix):
        row["cells"] = cells
    headers = _drop_empty_columns([[_clean(c) for c in row] for row in header_rows if any(_clean(c) for c in row)])
    return {
        "header_rows": headers,
        "rows": body,
        "note_column": None,
        "unit": "",
    }


def extract_note(
    pdf,
    texts: list[str],
    headings: list[dict[str, Any]],
    ref: str,
    *,
    year: int,
) -> dict[str, Any] | None:
    pages, resolved = _note_page_span(headings, ref, len(texts))
    if not pages:
        return None
    heading = next((h for h in headings if h["note_ref"] == resolved), None)
    title = str(heading["title"]) if heading else f"Note {resolved}"
    page_text = "\n".join(texts[p - 1] for p in pages if 1 <= p <= len(texts))
    ruled_rows: list[list[Any]] = []
    ruled_ok = False
    for page_num in pages[:1]:
        page = pdf.pages[page_num - 1]
        tables = page.extract_tables() or []
        substantial = [
            t
            for t in tables
            if len(t) >= 3 and max((len(r) for r in t), default=0) >= 4
        ]
        if substantial and len(page.lines) >= 30:
            ruled_ok = True
            # The first ruled grid under the note heading is the note table.
            # Later grids on the same page are often the next disclosure.
            ruled_rows.extend(substantial[0])
    table: dict[str, Any] | None = None
    if ruled_ok and ruled_rows:
        table = _note_from_ruled(ruled_rows, year=year, page_text=page_text)
    if table is None:
        table = _note_from_words(pdf, pages, ref=resolved, title=title)
    if table is None:
        return {
            "note_ref": resolved,
            "title": title,
            "pages": pages,
            "header_rows": [],
            "rows": [],
            "note_column": None,
            "ok": False,
        }
    table["rows"] = _trim_note_rows(table["rows"], resolved, headings)
    table["note_ref"] = resolved
    table["title"] = title
    table["pages"] = pages
    table["ok"] = len(table["rows"]) > 0
    if not table.get("unit"):
        table["unit"] = _unit_label(page_text)
    return table


def _collect_note_refs(statements: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for statement in statements:
        for row in statement.get("rows") or []:
            ref = str(row.get("note_ref") or "").strip()
            if ref and ref not in seen:
                seen.append(ref)
            for cell in row.get("cells") or []:
                text = _clean(cell)
                if re.fullmatch(_NOTE_REF, text) and text not in seen:
                    # Only the dedicated note column should add refs. Cells are
                    # scanned below only when the row already marked a note.
                    pass
    return seen


def extract_printed_report(
    pdf_path: Path,
    *,
    company_slug: str,
    company_name: str,
    year: int,
) -> dict[str, Any]:
    texts = _page_texts(pdf_path)
    located = locate_statement_pages(texts)
    statements: list[dict[str, Any]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for kind in STATEMENT_ORDER:
            pages = located.get(kind) or []
            if not pages:
                statements.append(
                    {
                        "key": kind,
                        "title": STATEMENT_TITLES[kind],
                        "pages": [],
                        "header_rows": [],
                        "rows": [],
                        "note_column": None,
                        "unit": "",
                        "ok": False,
                    }
                )
                continue
            extracted = extract_statement(pdf, texts, kind, pages, year=year)
            if extracted is None:
                statements.append(
                    {
                        "key": kind,
                        "title": STATEMENT_TITLES[kind],
                        "pages": pages,
                        "header_rows": [],
                        "rows": [],
                        "note_column": None,
                        "unit": "",
                        "ok": False,
                    }
                )
                continue
            extracted["ok"] = True
            statements.append(extracted)

        notes_start = 1
        for statement in statements:
            for page in statement.get("pages") or []:
                notes_start = max(notes_start, int(page) + 1)
        headings = _index_note_headings(texts, notes_start)
        notes: dict[str, Any] = {}
        for ref in _collect_note_refs(statements):
            note = extract_note(pdf, texts, headings, ref, year=year)
            if note:
                notes[str(note.get("note_ref") or ref)] = note
                if str(note.get("note_ref")) != ref:
                    notes[ref] = note

    units = [s.get("unit") for s in statements if s.get("unit")]
    return {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": year,
        "source_pdf": str(pdf_path),
        "unit": units[0] if units else "",
        "statements": statements,
        "notes": notes,
    }


def default_output_path(company_slug: str, year: int) -> Path:
    root = Path(__file__).resolve().parents[1] / "extracted" / "printed_statements"
    return root / company_slug / f"{year}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract printed FS tables and notes")
    parser.add_argument("--company-slug", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--pdf", default="")
    parser.add_argument("--company-name", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    pdf_path: Path | None
    if args.pdf:
        pdf_path = Path(args.pdf)
    else:
        db, client = get_db()
        try:
            pdf_path = resolve_annual_pdf(db, args.company_slug, args.year)
        finally:
            client.close()
    if pdf_path is None or not pdf_path.exists():
        print(f"Annual PDF not found for {args.company_slug} {args.year}", file=sys.stderr)
        return 1

    name = args.company_name.strip() or args.company_slug.replace("_", " ")
    payload = extract_printed_report(
        pdf_path,
        company_slug=args.company_slug,
        company_name=name,
        year=args.year,
    )
    out = Path(args.out) if args.out else default_output_path(args.company_slug, args.year)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for statement in payload["statements"]:
        print(
            f"{statement['key']}: rows={len(statement.get('rows') or [])} "
            f"pages={statement.get('pages')} ok={statement.get('ok')}",
            file=sys.stderr,
        )
    print(
        f"notes={len(payload['notes'])} -> {out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
