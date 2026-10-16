"""
Extract the primary financial statements as printed.

By default only the main statements are captured (income / profit or loss,
comprehensive income, financial position, cash flows). Pass ``--include-notes``
to also pull the note tables those statements point at.

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


# Ambeon 2024/25+ embeds a financial-section sidebar (titles + page numbers) on
# every FS page. PyMuPDF emits that nav *before* the real statement title, so
# naive TOC / auditor / notes checks reject the actual tables.
_FS_NAV_LISTED = re.compile(
    r"(?i)(?:financial calendar|independent auditor.?s?\s+report|"
    r"income statement|statement of comprehensive income|"
    r"statement of financial position|statement of changes in equity|"
    r"statement of cash flows|notes to the financial statements)"
    r"\s+\d{2,3}\b"
)
_FS_BODY_HEADING = re.compile(
    r"(?m)^(?:INCOME STATEMENT|STATEMENT OF COMPREHENSIVE INCOME|"
    r"STATEMENT OF FINANCIAL POSITION|STATEMENT OF CHANGES IN EQUITY|"
    r"STATEMENT OF CASH FLOWS|NOTES TO THE FINANCIAL STATEMENTS)\b"
)


def _has_fs_section_nav(text: str) -> bool:
    return len(_FS_NAV_LISTED.findall((text or "")[:2200])) >= 3


def _page_body_for_classify(text: str) -> str:
    """Drop repeated FS sidebar nav so classification sees the printed title.

    Prefer the chrome line ``Annual Report YYYY/YY`` (always near the top).
    Searching the whole page for ALL-CAPS titles is unsafe — footers sometimes
    repeat ``STATEMENT OF CASH FLOWS`` and would truncate the real body.
    """
    raw = text or ""
    if not _has_fs_section_nav(raw):
        return raw
    m2 = re.search(r"(?i)annual report\s+20\d{2}\s*/\s*\d{2}\s*", raw[:1000])
    if m2:
        return raw[m2.end() :]
    m = _FS_BODY_HEADING.search(raw[:1200])
    if m and m.start() > 60:
        return raw[m.start() :]
    return raw


def _looks_like_note_body(body: str) -> bool:
    """True when the page body is a numbered note, not a primary FS title."""
    head = (body or "").lstrip()[:200]
    if not head:
        return False
    if _FS_BODY_HEADING.match(head):
        return False
    if re.match(
        r"(?i)^(?:income statement|statement of (?:profit|comprehensive|"
        r"financial|cash|changes)|notes to the financial)",
        head,
    ):
        return False
    return bool(re.match(r"^\d{1,2}(?:\.\d+)?[\t .\-]", head))


def _is_toc_only_page(text: str) -> bool:
    """True for contents pages; false when a sidebar TOC rides on a real FS table."""
    if not text:
        return True
    body = _page_body_for_classify(text)
    amounts = len(_COMMA_AMT.findall(body))
    if amounts >= 8:
        return False
    bl = body.lstrip()
    if _FS_BODY_HEADING.match(bl) or re.match(
        r"(?i)^(?:income statement|statement of |notes to the financial)",
        bl[:120],
    ):
        return False
    return _looks_like_toc(body) or _looks_like_toc(text)


def _notes_section_page(texts: list[str]) -> int | None:
    """1-based page where FS numbered notes begin (not TOC / sustainability)."""
    fs_note_heads: list[int] = []
    reporting: list[int] = []
    for idx, raw in enumerate(texts):
        body = _page_body_for_classify(raw)
        head = body[:2200]
        low = head.lower()
        if "contents" in low[:500] or "table of contents" in low[:700]:
            continue
        if _is_toc_only_page(raw):
            continue
        # Sustainability "Basis of Preparation / 1. Reporting entity" must not
        # steal the FS window (COMB 2025 put that near p145; real FS ~p311).
        if "sustainability" in low[:900] and "notes to the financial" not in low[:500]:
            continue
        if re.search(
            r"(?i)^\s*notes to the financial statements\b",
            head[:500],
        ) or (
            low.lstrip().startswith("notes to the financial")
            or "\nnotes to the financial statements" in low[:700]
        ):
            fs_note_heads.append(idx + 1)
            continue
        if re.search(
            r"(?i)\b(?:0?1|1)(?:\.|\s)\s*(?:reporting entity|corporate information)\b",
            head,
        ):
            if "notes to the financial" in low[:900] or "financial statements" in low[:500]:
                reporting.append(idx + 1)
    # Prefer the first dedicated Notes-to-FS heading (usually right after cash flows).
    if fs_note_heads:
        return fs_note_heads[0]
    if reporting:
        return reporting[-1]
    return None


def _classify_page(text: str) -> str | None:
    """Primary statement printed on this page, if any."""
    if not text or _is_toc_only_page(text):
        return None
    body = _page_body_for_classify(text)
    if _looks_like_note_body(body):
        return None
    low = body.lower()
    head = low[:1600]
    if (
        "independent auditor" in head
        or "auditors' report" in head
        or "auditors’ report" in head
        or "key audit matter" in head
    ):
        return None
    # Interim / investor digest tables are not the audited primary statements.
    if (
        "summary of interim" in head
        or "interim financial statements" in head
        or "investor information" in head[:500]
        or "performance trends" in head[:500]
    ):
        return None
    if "financial performance" in head or "highlights" in head[:900]:
        return None
    if "sustainability" in head[:700] and "gross income" not in head:
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

    amounts = len(_COMMA_AMT.findall(body))
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
    if not text:
        return False
    body = _page_body_for_classify(text)
    if _is_toc_only_page(text):
        return False
    other = _classify_page(text)
    if other and other != kind:
        return False
    if "notes to the financial" in body.lower()[:400]:
        return False
    return len(_COMMA_AMT.findall(body)) >= 6


def _primary_statement_score(kind: str, text: str) -> int:
    """Higher = more like the audited primary FS table (not digests)."""
    body = _page_body_for_classify(text or "")
    low = body.lower()
    head = low[:2000]
    amounts = len(_COMMA_AMT.findall(body))
    score = amounts
    bl = body.lstrip()
    # Strongly prefer the audited primary title page over note / digest tables.
    if kind == "income_statement" and re.match(r"(?i)^income statement\b", bl):
        score += 200
    elif kind == "oci" and re.match(
        r"(?i)^statement of (?:profit or loss and )?comprehensive income\b", bl
    ):
        score += 200
    elif kind == "sofp" and re.match(r"(?i)^statement of financial position\b", bl):
        score += 200
    elif kind == "cash_flows" and re.match(r"(?i)^statement of cash flows\b", bl):
        score += 200
    if "group" in head and "bank" in head:
        score += 40
    if "note" in head and "page" in head:
        score += 15
    if kind == "income_statement":
        if "gross income" in head:
            score += 80
        if re.search(r"\bincome statement\b", head):
            score += 50
        if "interest income" in head and "interest expense" in low:
            score += 30
    elif kind == "oci":
        if "statement of profit or loss and other comprehensive income" in head:
            score += 90
        elif "comprehensive income" in head:
            score += 40
    elif kind == "sofp":
        if "statement of financial position" in head or "as at december" in head:
            score += 70
        if "total assets" in low and "total liabilities" in low:
            score += 40
        if "cash and cash equivalents" in head:
            score += 20
    elif kind == "cash_flows":
        if "statement of cash flows" in head:
            score += 80
        if "operating activities" in head and "investing activities" in low:
            score += 40
    return score


def _locate_in_range(
    texts: list[str], window_lo: int, window_hi: int
) -> dict[str, list[int]]:
    """Locate primary FS pages inside an inclusive 1-based page window."""
    found: dict[str, list[int]] = {}
    best_score: dict[str, int] = {}
    i = max(0, window_lo - 1)
    hi = min(len(texts), window_hi)
    while i < hi:
        kind = _classify_page(texts[i])
        if kind in STATEMENT_ORDER:
            score = _primary_statement_score(kind, texts[i])
            pages = [i + 1]
            j = i + 1
            while j < len(texts) and len(pages) < 3:
                if not _continues_statement(texts[j], kind):
                    break
                pages.append(j + 1)
                j += 1
            if kind not in found or score > best_score.get(kind, -1):
                found[kind] = pages
                best_score[kind] = score
            i = max(j, i + 1)
            continue
        i += 1
    return found


def locate_statement_pages(texts: list[str]) -> dict[str, list[int]]:
    notes_at = _notes_section_page(texts)
    if notes_at:
        # Wider window: long equity statements can sit 25+ pages before notes.
        window_lo = max(1, notes_at - 40)
        window_hi = notes_at - 1
        found = _locate_in_range(texts, window_lo, window_hi)
    else:
        found = {}

    # If notes detection failed (or pointed at sustainability), fall back to a
    # full-document scan and keep the strongest primary-statement hits.
    if len(found) < 3:
        full = _locate_in_range(texts, 1, len(texts))
        for kind, pages in full.items():
            if kind not in found:
                found[kind] = pages
            else:
                old = _primary_statement_score(kind, texts[found[kind][0] - 1])
                new = _primary_statement_score(kind, texts[pages[0] - 1])
                if new > old:
                    found[kind] = pages
    return found


def _looks_page_ref(value: str) -> bool:
    """True for a dedicated Page No. cell (not a note like 13.1 or an amount)."""
    text = _clean(value)
    if not text or _COMMA_AMT.search(text) or _looks_amount(text):
        return False
    # Page numbers are whole 2–3 digit ints; note refs may be "13.1".
    return bool(re.fullmatch(_PAGE_REF, text)) and "." not in text


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


def _peel_note_page(
    rest: list[str],
    *,
    expect_note: bool = False,
    expect_page: bool = False,
) -> tuple[str, str, list[str]]:
    """Peel Note (+ optional Page No.) off the leading amount cells.

    Bank ruled tables are Label | Note | Page | amounts…, but pdfplumber often
    leaves Note and Page as separate cells. Without peeling Page, the page
    number lands in the first amount slot and later shifts every year column.
    Empty Note/Page cells must still be consumed so amounts stay aligned.
    """
    if not rest:
        return "", "", []
    note, page, rem = _split_leading(rest[0])
    amounts: list[str] = []
    if note:
        if rem:
            amounts.append(rem)
        leftover = [_clean(c) for c in rest[1:]]
        if not page and leftover and (
            _looks_page_ref(leftover[0]) or (expect_page and not leftover[0])
        ):
            page = leftover[0]
            leftover = leftover[1:]
        amounts.extend(leftover)
        return note, page, amounts
    # Dedicated empty Note column (row has no note ref).
    if expect_note and (not rest[0] or rest[0].lower() in {"note", "-"}):
        leftover = [_clean(c) for c in rest[1:]]
        if expect_page and leftover and (
            not leftover[0] or _looks_page_ref(leftover[0])
        ):
            page = leftover[0] if _looks_page_ref(leftover[0]) else ""
            leftover = leftover[1:]
        return "", page, leftover
    # No note in first cell — still drop a lone page column when present.
    if _looks_page_ref(rest[0]) and len(rest) > 1:
        return "", _clean(rest[0]), [_clean(c) for c in rest[1:]]
    if expect_page and (not rest[0] or _looks_page_ref(rest[0])):
        page = rest[0] if _looks_page_ref(rest[0]) else ""
        return "", page, [_clean(c) for c in rest[1:]]
    return "", "", [_clean(c) for c in rest]


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


def _ruled_meta_columns(raw_rows: list[list[Any]]) -> tuple[bool, bool]:
    """Detect whether the ruled grid has dedicated Note / Page columns."""
    expect_note = False
    expect_page = False
    for raw in raw_rows[:6]:
        cells = [_clean(c).lower() for c in raw]
        blob = " ".join(cells)
        if "note" in blob:
            expect_note = True
        if re.search(r"\bpage\b", blob):
            expect_page = True
    if expect_note or expect_page:
        return expect_note, expect_page
    # Infer from body: many short note refs in column 1.
    note_hits = page_hits = 0
    scanned = 0
    for raw in raw_rows[1:40]:
        cells = [_clean(c) for c in raw]
        if len(cells) < 3 or not cells[0] or _has_comma_amount(cells[0]):
            continue
        scanned += 1
        if cells[1] and re.fullmatch(_NOTE_REF, cells[1]):
            note_hits += 1
        if len(cells) > 2 and _looks_page_ref(cells[2]):
            page_hits += 1
    if scanned >= 4:
        expect_note = note_hits >= max(2, scanned // 4)
        expect_page = page_hits >= max(2, scanned // 4)
    return expect_note, expect_page


def _parse_ruled_rows(raw_rows: list[list[Any]]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    width = 0
    expect_note, expect_page = _ruled_meta_columns(raw_rows)
    for raw in raw_rows:
        cells = [_clean(c) for c in raw]
        if not any(cells):
            continue
        label = cells[0] if cells else ""
        # Skip header furniture rows.
        low = label.lower()
        if low in {"", "note", "page", "page no.", "page no"} and not any(
            _has_comma_amount(c) for c in cells[1:]
        ):
            if any(c.lower() in {"note", "page", "page no.", "page no"} for c in cells):
                continue
        # If the label cell itself is an amount (column shift), recover text label.
        if _has_comma_amount(label) and not re.search(r"[A-Za-z]", label):
            for idx, cell in enumerate(cells):
                if idx == 0:
                    continue
                if cell and re.search(r"[A-Za-z]", cell) and not _looks_amount(cell):
                    label = cell
                    cells = [cell] + cells[1:idx] + cells[idx + 1 :]
                    break
        note, page, amounts = _peel_note_page(
            cells[1:],
            expect_note=expect_note,
            expect_page=expect_page,
        )
        # Drop leading note-like tokens that leaked into amounts (duplicate Note).
        while amounts and re.fullmatch(_NOTE_REF, amounts[0]) and not _looks_amount(
            amounts[0]
        ):
            if not note:
                note = amounts[0]
            amounts = amounts[1:]
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
    """Second entity next to Group: Company for holdings, Bank for banks."""
    head = (page_text or "").lower()[:2000]
    has_group = bool(re.search(r"\bgroup\b", head))
    has_company = bool(re.search(r"\bcompany\b", head))
    has_bank = bool(re.search(r"\bbank\b", head))
    # Prefer Company when both Group and Company headers appear (Ambeon-style).
    if has_group and has_company:
        return "Company"
    if has_group and has_bank:
        return "Bank"
    if has_company:
        return "Company"
    if has_bank:
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
    """Pick current/prior year headers near the report year.

    Rejects timestamp fragments (e.g. 2096 from PDF ids) and years far from
    the known report year so multi-year merges stay aligned.
    """
    lo = max(1990, fallback - 3)
    hi = min(2100, fallback + 2)
    years = [
        int(y)
        for y in re.findall(r"\b(20\d{2})\b", page_text[:2500])
        if lo <= int(y) <= hi
    ]
    uniq: list[int] = []
    for year in years:
        if year not in uniq:
            uniq.append(year)
    if fallback not in uniq:
        uniq.insert(0, fallback)
    if len(uniq) >= 2:
        # Prefer (fallback, fallback-1) when both appear; else closest pair.
        if fallback in uniq and (fallback - 1) in uniq:
            return fallback, fallback - 1
        current, prior = uniq[0], uniq[1]
        if prior > current:
            current, prior = prior, current
        # Guard: keep current within 1 year of the report year.
        if abs(current - fallback) > 1:
            return fallback, fallback - 1
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
    has_note: bool = True,
) -> list[list[str]]:
    """Build headers that match body width: Label [| Note] [| Page] | amounts.

    Always include BOTH current and prior year under Group and Company/Bank when
    enough amount columns exist (standard comparative note tables).
    """
    note_cols = 1 if has_note else 0
    page_cols = 1 if has_page else 0
    prefix = [""] + (["Note"] if has_note else []) + (["Page"] if has_page else [])
    prefix_pad = [""] * len(prefix)

    if has_page and has_change and amount_count >= 6:
        return [
            prefix_pad + ["Group", "", "", entity, "", ""],
            prefix
            + [
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
            prefix_pad + ["Group", "", entity, ""],
            prefix + [str(year), str(prior), str(year), str(prior)],
        ]

    # Default / note tables: Label [|Note] + Group(year,prior) + Entity(year,prior)
    labels = list(prefix)
    group = list(prefix_pad)
    rest = max(0, int(amount_count))
    entities = ["Group", entity]
    slot = 0
    while rest > 0:
        who = entities[min(slot, len(entities) - 1)]
        take = 3 if has_change and rest >= 3 else min(2, rest)
        # Prefer full year+prior pairs when we still have enough columns.
        if not has_change and rest >= 2:
            take = 2
        group.extend([who] + [""] * (take - 1))
        if take == 3:
            labels.extend([str(year), str(prior), "Change %"])
        elif take == 2:
            labels.extend([str(year), str(prior)])
        else:
            # Single leftover column — still label as current year.
            labels.append(str(year))
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
    has_note = any(bool(row.get("note")) for row in parsed)
    amount_count = max((len(row["amounts"]) for row in parsed), default=0)
    current, prior = _years_from_text(page_text, year)
    body: list[dict[str, Any]] = []
    for row in parsed:
        if _skip_ruled_row(row["label"], row["note"], row["amounts"]):
            continue
        cells = [row["label"]]
        if has_note:
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
    note_column = 1 if has_note else None
    headers = _build_ruled_headers(
        entity=_entity_name(page_text),
        year=current,
        prior=prior,
        has_page=has_page,
        has_change=has_change,
        amount_count=amount_count,
        has_note=has_note,
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


def _realign_amount_labels(rows: list[list[str]]) -> list[list[str]]:
    """When col 0 is mostly amounts, shift so the first text cell becomes the label."""
    if len(rows) < 5:
        return rows
    sample = rows[: min(40, len(rows))]
    amount_labels = sum(
        1
        for row in sample
        if row and _has_comma_amount(row[0]) and not re.search(r"[A-Za-z]", row[0])
    )
    if amount_labels < max(3, len(sample) // 3):
        return rows
    fixed: list[list[str]] = []
    for row in rows:
        cells = list(row)
        if not cells:
            fixed.append(cells)
            continue
        if re.search(r"[A-Za-z]", cells[0]) and not _has_comma_amount(cells[0]):
            fixed.append(cells)
            continue
        text_idx = next(
            (
                i
                for i, c in enumerate(cells)
                if c and re.search(r"[A-Za-z]", c) and not _looks_amount(c)
            ),
            None,
        )
        if text_idx is None:
            # Pure numeric row with no label — drop (usually a shifted footer).
            continue
        label = cells[text_idx]
        rest = [c for i, c in enumerate(cells) if i != text_idx]
        # Prefer Label | Note | amounts order after the shift.
        if rest and re.fullmatch(_NOTE_REF, rest[0]) and not _looks_amount(rest[0]):
            fixed.append([label, *rest])
        elif len(rest) >= 2 and re.fullmatch(_NOTE_REF, rest[1]) and not _looks_amount(
            rest[1]
        ):
            fixed.append([label, rest[1], rest[0], *rest[2:]])
        else:
            fixed.append([label, *rest])
    return fixed


def _word_body_rows(header_rows: list[list[str]], body_rows: list[list[str]], year: int) -> dict[str, Any] | None:
    if len(body_rows) < 5:
        return None
    body_rows = _realign_amount_labels([[_clean(c) for c in row] for row in body_rows])
    cleaned: list[dict[str, Any]] = _leading_section_rows(header_rows)
    staged: list[dict[str, Any]] = []
    for row in body_rows:
        cells = [_clean(c) for c in row]
        if not any(cells):
            continue
        blob = " ".join(cells)
        if _is_footer_blob(blob):
            continue
        label = cells[0]
        # Skip rows whose "label" is still a bare amount after realignment.
        if _has_comma_amount(label) and not re.search(r"[A-Za-z]", label):
            continue
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
        amount_cells = (
            cells[2:] if note_ref or (len(cells) > 1 and cells[1] == "") else cells[1:]
        )
        if note_ref and amount_cells and _looks_page_ref(amount_cells[0]):
            amount_cells = amount_cells[1:]
        staged.append(
            {
                "label": label,
                "note_ref": note_ref,
                "amounts": amount_cells,
                "style": _row_style(label, amount_cells, note_ref or ""),
            }
        )
    if len(staged) < 5:
        return None
    has_notes = any(r["note_ref"] for r in staged)
    for item in staged:
        cells = [item["label"]]
        if has_notes:
            cells.append(item["note_ref"] or "")
        cells.extend(item["amounts"])
        cleaned.append(
            {
                "cells": cells,
                "note_ref": item["note_ref"],
                "style": item["style"],
            }
        )
    if len(cleaned) < 5:
        return None
    width = max(len(r["cells"]) for r in cleaned)
    # Pad every row to width so Note/year columns stay aligned.
    for row in cleaned:
        row["cells"] = row["cells"] + [""] * (width - len(row["cells"]))
    note_column = 1 if has_notes else None
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


def _is_note_header_echo_row(label: str, amounts: list[str]) -> bool:
    """Drop repeated Group/Company/year header lines that land in the body."""
    lab = (label or "").strip().lower()
    if re.match(r"^(group|company|bank)$", lab) and not any(
        _has_comma_amount(a) for a in amounts
    ):
        return True
    joined = " ".join(str(a or "") for a in amounts).strip()
    if not lab and re.fullmatch(r"(?:20\d{2}\s*){2,}", joined):
        return True
    if not lab and all(
        (not str(a or "").strip()) or re.fullmatch(r"20\d{2}", str(a).strip())
        for a in amounts
    ):
        return True
    return False


def _note_from_ruled(raw_rows: list[list[Any]], *, year: int, page_text: str) -> dict[str, Any] | None:
    parsed = _parse_ruled_rows(raw_rows)
    if len(parsed) < 3:
        return None
    has_page = sum(1 for row in parsed if row["page"]) >= 2
    has_change = _header_has_change(raw_rows)
    has_note = any(bool(row.get("note")) for row in parsed)
    amount_count = max((len(row["amounts"]) for row in parsed), default=0)
    current, prior = _years_from_text(page_text, year)
    body: list[dict[str, Any]] = []
    for row in parsed:
        if _skip_ruled_row(row["label"], row["note"], row["amounts"]):
            continue
        if not row["label"] and not any(row["amounts"]):
            continue
        if _is_note_header_echo_row(row["label"], row["amounts"]):
            continue
        cells = [row["label"]]
        if has_note:
            cells.append(row["note"])
        if has_page:
            cells.append(row["page"])
        # Keep every amount column (current + prior × Group + Company/Bank).
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
            has_note=has_note,
        ),
        "rows": body,
        "note_column": 1 if has_note else None,
        "unit": _unit_label(page_text),
    }


def _note_from_words(
    pdf,
    pages: list[int],
    *,
    ref: str,
    title: str,
    year: int | None = None,
) -> dict[str, Any] | None:
    combined: list[list[str]] = []
    page_text_bits: list[str] = []
    for page_num in pages:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        page = pdf.pages[page_num - 1]
        try:
            page_text_bits.append(page.extract_text() or "")
        except Exception:
            pass
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
        amounts = cells[1:]
        blob = " ".join(cells)
        if label.lower().startswith("segment information"):
            break
        if _FURNITURE.match(re.sub(r"\s+", " ", label).strip()):
            continue
        if _is_footer_blob(blob):
            continue
        if not any(cells):
            continue
        if _is_note_header_echo_row(label, amounts):
            continue
        note = ""
        if len(cells) > 1 and re.fullmatch(_NOTE_REF, cells[1]):
            note = cells[1]
            amounts = cells[2:]
        body.append(
            {
                "cells": ([label, note] + amounts) if note else ([label] + amounts),
                "note_ref": note or None,
                "style": _row_style(label, amounts, note),
            }
        )
    if len(body) < 1:
        return None
    matrix = _drop_empty_columns([r["cells"] for r in body])
    for row, cells in zip(body, matrix):
        row["cells"] = cells

    page_text = "\n".join(page_text_bits)
    has_note = any(r.get("note_ref") for r in body)
    amount_count = max(
        (
            len(r["cells"]) - (2 if has_note else 1)
            for r in body
            if isinstance(r.get("cells"), list)
        ),
        default=0,
    )
    # Rebuild comparative headers so Group/Company × current+prior always align.
    report_year = int(year) if year else _years_from_text(page_text, 2020)[0]
    current, prior = _years_from_text(page_text, report_year)
    headers = _build_ruled_headers(
        entity=_entity_name(page_text),
        year=current,
        prior=prior,
        has_page=False,
        has_change=False,
        amount_count=max(amount_count, 4 if amount_count >= 3 else amount_count),
        has_note=has_note,
    )
    # Fall back to OCR headers only when rebuild produced nothing useful.
    if not headers or max((len(h) for h in headers), default=0) < 3:
        headers = _drop_empty_columns(
            [[_clean(c) for c in row] for row in header_rows if any(_clean(c) for c in row)]
        )
    return {
        "header_rows": headers,
        "rows": body,
        "note_column": 1 if has_note else None,
        "unit": _unit_label(page_text),
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
        table = _note_from_words(
            pdf, pages, ref=resolved, title=title, year=year
        )
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
        note_col = statement.get("note_column")
        for row in statement.get("rows") or []:
            ref = str(row.get("note_ref") or "").strip()
            if ref and re.fullmatch(_NOTE_REF, ref) and ref not in seen:
                seen.append(ref)
                continue
            cells = row.get("cells") or []
            # Prefer explicit note column (usually index 1 after the label).
            idx = note_col if isinstance(note_col, int) else 1
            if isinstance(idx, int) and 0 <= idx < len(cells):
                text = _clean(cells[idx])
                if re.fullmatch(_NOTE_REF, text) and text not in seen:
                    seen.append(text)
    return seen


def collect_note_refs(statements: list[dict[str, Any]]) -> list[str]:
    """Public: note numbers referenced by main FS tables (from Extracted Tables)."""
    return _collect_note_refs(statements)


def _repair_statement_columns(statement: dict[str, Any]) -> dict[str, Any]:
    """Last-chance fix: peel note/page tokens out of amount slots."""
    rows = statement.get("rows") or []
    if not rows:
        return statement
    note_col = statement.get("note_column")
    fixed_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = [_clean(c) for c in (row.get("cells") or [])]
        if not cells:
            fixed_rows.append(row)
            continue
        label = cells[0]
        rest = cells[1:]
        note, page, amounts = _peel_note_page(rest)
        # Prefer existing note_ref when peel found nothing.
        note_ref = note or (
            str(row.get("note_ref") or "").strip()
            if re.fullmatch(_NOTE_REF, str(row.get("note_ref") or "").strip())
            else ""
        )
        while amounts and re.fullmatch(_NOTE_REF, amounts[0]) and not _looks_amount(
            amounts[0]
        ):
            if not note_ref:
                note_ref = amounts[0]
            amounts = amounts[1:]
        new_cells = [label]
        if note_col is not None or note_ref:
            new_cells.append(note_ref)
        if page and statement.get("header_rows"):
            # Keep page only when headers already advertise a Page column.
            bottom = (statement.get("header_rows") or [[]])[-1]
            if any(re.match(r"(?i)^page", str(c)) for c in bottom):
                new_cells.append(page)
        new_cells.extend(amounts)
        fixed_rows.append(
            {
                **row,
                "cells": new_cells,
                "note_ref": note_ref or None,
            }
        )
    statement = {**statement, "rows": fixed_rows}
    if any(r.get("note_ref") for r in fixed_rows):
        statement["note_column"] = 1
    return statement


def extract_notes_only(
    pdf_path: Path,
    statements: list[dict[str, Any]],
    *,
    year: int,
) -> dict[str, Any]:
    """Extract note tables referenced by already-extracted main FS statements.

    Used by Run DB Annual: FS come from Extracted Tables; only notes are
    pulled from the PDF for the DB page clickable note popups.
    """
    texts = _page_texts(pdf_path)
    notes_start = 1
    for statement in statements:
        for page in statement.get("pages") or []:
            try:
                notes_start = max(notes_start, int(page) + 1)
            except (TypeError, ValueError):
                continue
    notes: dict[str, Any] = {}
    refs = _collect_note_refs(statements)
    if not refs:
        return notes
    with pdfplumber.open(str(pdf_path)) as pdf:
        headings = _index_note_headings(texts, notes_start)
        for ref in refs:
            note = extract_note(pdf, texts, headings, ref, year=year)
            if not note:
                continue
            key = str(note.get("note_ref") or ref)
            notes[key] = note
            if key != ref:
                notes[ref] = note
    return notes


def extract_printed_report(
    pdf_path: Path,
    *,
    company_slug: str,
    company_name: str,
    year: int,
    include_notes: bool = False,
) -> dict[str, Any]:
    from extraction_validation import double_check_printed_payload

    texts = _page_texts(pdf_path)
    located = locate_statement_pages(texts)
    statements: list[dict[str, Any]] = []
    notes: dict[str, Any] = {}
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

    if include_notes:
        notes = extract_notes_only(pdf_path, statements, year=year)

    units = [s.get("unit") for s in statements if s.get("unit")]
    payload: dict[str, Any] = {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": year,
        "source_pdf": str(pdf_path),
        "unit": units[0] if units else "",
        "statements": statements,
        "notes": notes,
    }

    # Double-check column placement before the payload is saved / uploaded.
    check = double_check_printed_payload(payload)
    if not check.ok:
        repaired: list[dict[str, Any]] = []
        for statement in statements:
            if statement.get("ok"):
                repaired.append(_repair_statement_columns(statement))
            else:
                repaired.append(statement)
        payload["statements"] = repaired
        check = double_check_printed_payload(payload)
    payload["validation"] = check.to_dict()
    if check.notes:
        payload["validation_notes"] = list(check.notes)
    return payload


def default_output_path(company_slug: str, year: int) -> Path:
    root = Path(__file__).resolve().parents[1] / "extracted" / "printed_statements"
    return root / company_slug / f"{year}.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract printed main FS tables (optionally notes)"
    )
    parser.add_argument("--company-slug", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--pdf", default="")
    parser.add_argument("--company-name", default="")
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--include-notes",
        action="store_true",
        help="Also extract note tables referenced by the main statements",
    )
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
        include_notes=bool(args.include_notes),
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
