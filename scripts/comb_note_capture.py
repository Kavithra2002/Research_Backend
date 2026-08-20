"""
Capture note tables from annual PDFs into financial_tables (no OpenAI).

Uses FS-derived note references (e.g. 13.1 from income statement Note column),
locates the matching page in the PDF notes section, extracts the table with
pdfplumber coordinate parsing, and upserts to financial_tables as note_13_1.

The default DB annual run uses image-only capture: rasterise note pages to
``Demo_Data_captures`` without parsing table rows (faster, UI shows captures).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEMO_CAPTURES_OUT = BACKEND_DIR / "Demo_Data_captures"

try:
    import fitz
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from comb_note_extractor import (
    _entity_year_col,
    _index_table,
    _split_header_body,
    _words_table_rows,
    extract_note_breakdown_from_rows,
    map_labels_to_values,
    resolve_annual_pdf,
)
from comb_note_registry import (
    _note_statement_key,
    build_note_capture_plan,
    parent_value_from_statements,
)
from comb_reconcile import parse_number
from generate_comb_model import norm_label

NOTE_REF_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})?)$")
NOTE_CAPTURE_DPI = 150
PAGE_HEADER_MARGIN = 42.0
PAGE_FOOTER_MARGIN = 52.0
CROP_PAD_X = 24.0
CROP_PAD_Y = 8.0
TABLE_HEADER_LOOKAHEAD = 80
# Prefer the first GROUP/BANK header soon after a note heading (avoids later
# sibling tables on the same page winning by score — e.g. note 13.2 vs 13.3).
# Keep this wide enough to skip accounting-policy paragraphs before the grid.
NEAR_TABLE_HEADER_WINDOW = 40
SPLIT_TOTAL_MAX_GAP = 18.0

# pdf_path → printed page → PDF page (1-based). Built once per capture run.
_PRINTED_PAGE_MAP_CACHE: dict[str, dict[int, int]] = {}
_NOTE_PAGES_INDEX_CACHE: dict[str, dict[str, list[int]]] = {}
_FITZ_PAGE_TEXT_CACHE: dict[str, list[str]] = {}
# (id(pdfplumber.pdf), page_number) → grouped word lines. Cleared with caches.
_PAGE_LINES_CACHE: dict[tuple[int, int], list[dict[str, Any]]] = {}


def _pdf_cache_key(pdf_path: Path) -> str:
    try:
        st = pdf_path.stat()
        return f"{pdf_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return str(pdf_path)


def _fitz_page_texts(pdf_path: Path) -> list[str]:
    key = _pdf_cache_key(pdf_path)
    cached = _FITZ_PAGE_TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    texts: list[str] = []
    if fitz is None:
        return texts
    try:
        with fitz.open(str(pdf_path)) as doc:
            texts = [(doc[i].get_text() or "") for i in range(len(doc))]
    except Exception:
        texts = []
    _FITZ_PAGE_TEXT_CACHE[key] = texts
    # Keep memory bounded across multi-year runs.
    if len(_FITZ_PAGE_TEXT_CACHE) > 4:
        for old in list(_FITZ_PAGE_TEXT_CACHE.keys())[:-2]:
            _FITZ_PAGE_TEXT_CACHE.pop(old, None)
            _PRINTED_PAGE_MAP_CACHE.pop(old, None)
            _NOTE_PAGES_INDEX_CACHE.pop(old, None)
            # Page-line cache keys are pdfplumber object ids; drop wholesale.
            _PAGE_LINES_CACHE.clear()
    return texts

_DISQUALIFIED_PAGE_MARKERS = (
    "gri content index",
    "gri standard",
    "general disclosures",
    "stakeholder engagement",
    "reporting practice",
    "annex 3:",
    "annex 11:",
    "index of figures",
    "index of tables",
    "corporate governance report",
    "chairman's message",
    "board of directors' profiles",
)

_TABLE_SETTINGS = [
    {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    {
        "vertical_strategy": "lines",
        "horizontal_strategy": "text",
        "intersection_tolerance": 6,
    },
    {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "snap_tolerance": 4,
        "join_tolerance": 4,
        "intersection_tolerance": 5,
        "min_words_vertical": 2,
        "min_words_horizontal": 2,
    },
]


def _hint_tokens(hint: str) -> list[str]:
    return [
        tok
        for tok in re.split(r"[^a-z0-9]+", (hint or "").lower())
        if len(tok) > 2
    ]


def _is_notes_section_page(low: str) -> bool:
    return any(
        marker in low
        for marker in (
            "notes to",
            "notes forming",
            "notes to the consolidated",
            "notes to the financial",
            "notes to and forming",
        )
    )


def _is_disqualified_note_page(low: str) -> bool:
    return any(marker in low for marker in _DISQUALIFIED_PAGE_MARKERS)


def _page_is_primary_financial_statement(low: str) -> bool:
    """True for income statement / SoFP / cash-flow / contents grids, not Notes."""
    head = low[:1600]
    if re.search(r"(?:^|\n)\s*\d{1,2}\.\s+[a-z]", head) and "accounting policy" in head:
        return False
    if re.search(r"(?:^|\n)\s*notes to the financial", head):
        return False
    stmt_titles = (
        "income statement" in head,
        "statement of financial position" in head,
        "statement of cash flows" in head,
        "statement of profit or loss" in head,
    )
    # Contents page lists several primary statements together.
    if sum(bool(v) for v in stmt_titles) >= 2:
        return True
    # Income statement / SoFP grids always print Page No. beside Note.
    if "page no" in head and "gross income" in head and "interest income" in head:
        return True
    if "page no" in head and "total assets" in head and "total liabilities" in head:
        return True
    if "page no" not in head:
        return False
    return bool(
        re.search(
            r"(?:^|\n)\s*(income statement|statement of financial position|"
            r"statement of cash flows|statement of profit or loss)\b",
            head,
            re.I,
        )
    )


def _score_note_page_text(text: str, low: str, note_ref: str, hint: str) -> int:
    """Prefer real Notes-to-FS pages with a numbered heading + financial table."""
    if _is_disqualified_note_page(low):
        return 0
    if not _is_notes_section_page(low):
        return 0
    # Exclude primary FS summary pages (not note pages that mention Income Statement in policy).
    has_note_heading = bool(
        re.search(r"(?:^|\n)\s*\d{1,2}\.\s+[A-Za-z]", text)
        or re.search(r"accounting policy", low)
    )
    if not has_note_heading and "page no" in low and (
        "income statement" in low
        or "statement of financial position" in low
        or "statement of cash flows" in low
    ):
        return 0
    if (
        not has_note_heading
        and "gross income" in low
        and "total operating income" in low
        and "page no" in low
    ):
        return 0

    ref = re.escape(note_ref.strip())
    hint_tokens = _hint_tokens(hint)
    score = 0

    heading = re.search(
        rf"(?:^|\n)\s*{ref}(?:\s+|\.\s+)([^\n]+)",
        text,
        re.IGNORECASE,
    )
    if heading:
        title_part = heading.group(1).lower()
        if hint_tokens:
            hits = sum(1 for tok in hint_tokens if tok in title_part)
            if hits >= min(2, len(hint_tokens)):
                score += 60
            elif hits >= 1:
                score += 35
        else:
            score += 30

    # Embedded heading style: "12 13.1 Interest income"
    if not heading:
        embedded = re.search(
            rf"(?:^|\n)\s*\d{{1,2}}\s+{ref}(?:\s+|\.\s+)([^\n]+)",
            text,
            re.IGNORECASE,
        )
        if embedded:
            title_part = embedded.group(1).lower()
            if hint_tokens:
                hits = sum(1 for tok in hint_tokens if tok in title_part)
                if hits >= 1:
                    score += 55
            else:
                score += 25

    if re.search(rf"(?:^|\n)\s*{ref}\s*\.\s+", text, re.IGNORECASE):
        score += 12

    main_ref, sub_ref = _note_ref_parts(note_ref.strip())
    if not sub_ref:
        if re.search(rf"(?:^|\n)\s*{re.escape(main_ref)}\.\d+", text, re.IGNORECASE):
            score += 35

    if "group" in low and "bank" in low and ("rs." in low or "'000" in low):
        score += 18
    if hint_tokens and all(tok in low for tok in hint_tokens[:2]):
        score += 10
    # Tables whose heading sits on the previous page (e.g. note 20 summary on page 180).
    if not heading and hint_tokens:
        body_hits = sum(1 for tok in hint_tokens if tok in low)
        if body_hits >= min(2, len(hint_tokens)) and "group" in low and "bank" in low:
            score += 45
        elif body_hits >= 1 and "for the year ended december 31" in low:
            score += 28
        if body_hits >= 2 and (
            "depreciation of property" in low
            or "amortisation of computer" in low
            or "amortization of computer" in low
        ):
            score += 50
    if re.search(rf"(?:^|\n)\s*{ref}\s*$", text, re.MULTILINE):
        score += 2
    return score


def _group_page_lines(page) -> list[dict[str, Any]]:
    if page is None:
        return []
    try:
        page_num = int(getattr(page, "page_number", 0) or 0)
        pdf_obj = getattr(page, "pdf", None)
        cache_key = (id(pdf_obj) if pdf_obj is not None else id(page), page_num)
    except Exception:
        cache_key = None
    if cache_key is not None:
        cached = _PAGE_LINES_CACHE.get(cache_key)
        if cached is not None:
            return cached
    try:
        words = page.extract_words(
            x_tolerance=1.5,
            y_tolerance=2,
            keep_blank_chars=False,
            use_text_flow=False,
        )
    except Exception:
        return []
    if not words:
        if cache_key is not None:
            _PAGE_LINES_CACHE[cache_key] = []
        return []

    words.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))
    lines: list[dict[str, Any]] = []
    for word in words:
        top = float(word["top"])
        if lines and abs(top - float(lines[-1]["top"])) < 3.0:
            lines[-1]["words"].append(word)
            lines[-1]["bottom"] = max(float(lines[-1]["bottom"]), float(word["bottom"]))
        else:
            lines.append(
                {
                    "top": top,
                    "bottom": float(word["bottom"]),
                    "words": [word],
                }
            )
    for line in lines:
        line["words"].sort(key=lambda w: float(w["x0"]))
        line["text"] = " ".join(str(w["text"]) for w in line["words"]).strip()
        line["x0"] = min(float(w["x0"]) for w in line["words"])
        line["x1"] = max(float(w["x1"]) for w in line["words"])
    if cache_key is not None:
        _PAGE_LINES_CACHE[cache_key] = lines
        # Bound memory across long annual note runs (~100+ notes).
        if len(_PAGE_LINES_CACHE) > 500:
            for old_key in list(_PAGE_LINES_CACHE.keys())[:200]:
                _PAGE_LINES_CACHE.pop(old_key, None)
    return lines


def _line_has_big_numbers(text: str) -> bool:
    return bool(re.search(r"\d{1,3}(?:,\d{3})+|\(\d{1,3}(?:,\d{3})+\)", text))


def _note_ref_parts(note_ref: str) -> tuple[str, str | None]:
    ref = note_ref.strip()
    if "." in ref:
        main, sub = ref.split(".", 1)
        return main, sub
    return ref, None


def _heading_note_ref(text: str) -> str | None:
    match = re.match(r"^(\d{1,2}(?:\.\d{1,2})?)", text.strip())
    return match.group(1) if match else None


def _heading_belongs_to_note(heading_ref: str, note_ref: str) -> bool:
    heading_ref = heading_ref.strip()
    note_ref = note_ref.strip()
    if heading_ref == note_ref:
        return True
    main, _ = _note_ref_parts(note_ref)
    if "." not in note_ref and heading_ref.startswith(f"{main}."):
        return True
    return False


def _heading_note_ref_for_capture(text: str, note_ref: str) -> str | None:
    """Note ref token used for boundary logic on this heading line."""
    stripped = text.strip()
    main_ref, sub_ref = _note_ref_parts(note_ref)
    if sub_ref:
        if re.search(rf"(?:^|\s){re.escape(note_ref.strip())}(?:\s+|\.\s+)", stripped, re.I):
            return note_ref.strip()
        return None
    match = re.search(rf"(?:^|\s)({re.escape(main_ref)}\.\d+)", stripped, re.I)
    if match:
        return match.group(1)
    if re.search(rf"(?:^|\s){re.escape(main_ref)}(?:\s+|\.\s+)", stripped, re.I):
        return main_ref
    return _heading_note_ref(stripped)


def _line_relevant_to_note(text: str, note_ref: str) -> bool:
    """True when the line contains the target note ref as a heading (not inline page no.)."""
    stripped = text.strip()
    ref = re.escape(note_ref.strip())
    main_ref, sub_ref = _note_ref_parts(note_ref)
    if sub_ref:
        return bool(
            re.search(rf"(?:^|\s){ref}(?:\s+|\.\s+)", stripped, re.IGNORECASE)
        )
    return bool(
        re.search(
            rf"(?:^|\s){re.escape(main_ref)}(?:\.\d+)?(?:\s+|\.\s+)",
            stripped,
            re.IGNORECASE,
        )
    )


def _extract_note_heading_title(text: str, note_ref: str) -> str | None:
    """Parse note title from a section heading line (not a data row)."""
    stripped = text.strip()
    if _line_has_big_numbers(stripped):
        return None
    if not _line_relevant_to_note(stripped, note_ref):
        return None

    ref = re.escape(note_ref.strip())
    main_ref, sub_ref = _note_ref_parts(note_ref)
    patterns = (
        rf"^{ref}(?:\s+|\.\s+)(.+)$",
        rf"^\d+\s+{ref}(?:\s+|\.\s+)(.+)$",
        rf"(?:^|\s){ref}(?:\s+|\.\s+)([^\d(].+)$",
    )
    if not sub_ref:
        patterns += (
            rf"^{re.escape(main_ref)}\.(\d+)\s+(.+)$",
            rf"^{re.escape(main_ref)}\.\s+(.+)$",
            rf"^\d+\s+{re.escape(main_ref)}\.(\d+)\s+(.+)$",
        )
    for pat in patterns:
        match = re.search(pat, stripped, re.IGNORECASE)
        if not match:
            continue
        title_part = match.group(match.lastindex).strip()
        if not title_part or re.match(r"^\d", title_part):
            continue
        return title_part
    return None


def _title_hint_matches(title_part: str, title_hint: str, *, note_ref: str, heading_ref: str | None) -> bool:
    tokens = _hint_tokens(title_hint)
    if not tokens:
        return True
    title_low = title_part.lower()
    hits = sum(1 for tok in tokens if tok in title_low)
    relaxed = bool(
        heading_ref
        and "." not in note_ref.strip()
        and heading_ref.startswith(f"{note_ref.strip()}.")
    )
    if relaxed:
        needed = 1
    elif len(tokens) == 1:
        needed = 1
    else:
        needed = min(2, len(tokens))
    return hits >= needed


def _line_matches_note_heading(text: str, note_ref: str, title_hint: str) -> bool:
    title_part = _extract_note_heading_title(text, note_ref)
    if not title_part:
        return False
    heading_ref = _heading_note_ref(text)
    if heading_ref and not _heading_belongs_to_note(heading_ref, note_ref):
        # Embedded refs: "12 13.1 Interest income" — trust title extraction.
        if not _line_relevant_to_note(text, note_ref):
            return False
        heading_ref = note_ref.strip()
    return _title_hint_matches(
        title_part,
        title_hint,
        note_ref=note_ref,
        heading_ref=heading_ref,
    )


def _line_is_group_bank_header(text: str) -> bool:
    up = text.upper()
    return "GROUP" in up and "BANK" in up


def _line_is_stage_table_header(text: str) -> bool:
    up = text.upper()
    return "STAGE 1" in up and "STAGE 3" in up and "TOTAL" in up


def _line_is_balance_sheet_date_header(text: str) -> bool:
    up = text.upper()
    if "AS AT DECEMBER 31" not in up:
        return False
    # Column headers repeat years (2019 2018 2019 2018). Narrative policy
    # lines usually have a single year and trailing prose.
    years = re.findall(r"\b20\d{2}\b", text)
    if len(years) < 2:
        return False
    # Reject long narrative sentences.
    if len(text) > 80 and not re.search(r"\bRs\.?\b|'000|NOTE", text, re.I):
        return False
    return True


def _line_is_amount_year_header(text: str) -> bool:
    up = text.upper()
    return ("RS." in up or "'000" in text) and bool(re.search(r"\b20\d{2}\b", text))


def _line_is_financial_table_header(text: str) -> bool:
    return (
        _line_is_group_bank_header(text)
        or _line_is_stage_table_header(text)
        or _line_is_balance_sheet_date_header(text)
        or _line_is_amount_year_header(text)
    )


def _line_is_useful_life_table_header(text: str) -> bool:
    up = text.upper()
    return "CLASS OF ASSET" in up and (
        "DEPRECIATION" in up or "AMORTISATION" in up or "AMORTIZATION" in up
    )


def _line_is_for_year_ended_header(text: str) -> bool:
    return "FOR THE YEAR ENDED" in text.upper() and bool(
        re.search(r"\b20\d{2}\b", text)
    )


def _line_is_note_page_table_header(lines: list[dict[str, Any]], idx: int) -> bool:
    """GROUP/BANK breakdown grid with Note/Page columns (not useful-life tables)."""
    if idx < 0 or idx >= len(lines):
        return False
    if not _line_is_group_bank_header(lines[idx]["text"]):
        return False
    for j in range(max(0, idx - 2), min(len(lines), idx + 5)):
        text = lines[j]["text"]
        up = text.upper()
        if _line_is_useful_life_table_header(text):
            return False
        if _line_is_for_year_ended_header(text):
            return True
        # Balance-sheet / SoFP note tables use "As at December 31,".
        if _line_is_balance_sheet_date_header(text):
            return True
        if _line_is_amount_year_header(text) and (
            "AS AT" in up or "FOR THE YEAR" in up or "NOTE" in up
        ):
            return True
        if "NOTE" in up and "PAGE NO" in up:
            return True
    return False


def _find_table_block_top(lines: list[dict[str, Any]], group_idx: int) -> int:
    """First line of the visible table block (For the year ended / Note row)."""
    top = group_idx
    for idx in range(group_idx - 1, max(-1, group_idx - 5), -1):
        text = lines[idx]["text"]
        if _line_is_for_year_ended_header(text):
            return idx
        if _line_is_balance_sheet_date_header(text):
            return idx
        up = text.upper()
        if "NOTE" in up and "PAGE NO" in up:
            return idx
        if _line_is_group_bank_header(text):
            top = idx
            continue
        if _line_is_simple_year_column_header(text) or _line_is_rs_unit_header(text):
            top = idx
            continue
        if re.search(r"accounting policy", text, re.I):
            break
        if len(text) > 110 and not _line_has_big_numbers(text):
            break
        break
    return top


def _table_body_matches_hint(
    lines: list[dict[str, Any]],
    start_idx: int,
    end_idx: int,
    title_hint: str,
) -> bool:
    tokens = _hint_tokens(title_hint)
    if not tokens:
        return True
    body = " ".join(lines[i]["text"] for i in range(start_idx, min(end_idx + 1, len(lines))))
    body_low = body.lower()
    hits = sum(1 for tok in tokens if tok in body_low)
    return hits >= min(2, len(tokens)) or (len(tokens) == 1 and hits >= 1)


def _line_is_note_summary_total(text: str, title_hint: str) -> bool:
    """Closing row that repeats the note title (e.g. 'Net interest income')."""
    if not _line_has_big_numbers(text):
        return False
    label = re.sub(r"[\d,\.\(\)\-\+%']+", " ", text, flags=re.I)
    label = re.sub(r"\s+", " ", label).strip()
    if not label:
        return False
    label_norm = norm_label(label)
    hint_norm = norm_label(title_hint)
    if not hint_norm:
        return False
    # Exact match only — avoid child rows like "Interest income" ⊂ "Net interest income".
    return label_norm == hint_norm


def _is_note_section_heading_line(text: str) -> bool:
    """True for section headings like '13.2 Interest expense', not inline '34.2 (a)'."""
    stripped = text.strip()
    match = re.match(
        r"^(\d{1,2}(?:\.\d{1,2})?)\s*\.?\s+([^\d(].+)$",
        stripped,
    )
    if not match:
        return False
    title = match.group(2).strip()
    if not title or title.startswith("("):
        return False
    if _line_has_big_numbers(stripped):
        alpha = sum(1 for ch in stripped if ch.isalpha())
        if alpha < 10:
            return False
    return True


def _line_is_next_note_boundary(text: str, note_ref: str) -> bool:
    if not _is_note_section_heading_line(text):
        return False
    match = re.match(r"^(\d{1,2}(?:\.\d{1,2})?)", text.strip())
    if not match:
        return False
    found = match.group(1)
    current = note_ref.strip()
    if found == current:
        return False
    if "." in current:
        cur_main, _, cur_sub = current.partition(".")
        if "." in found:
            f_main, _, f_sub = found.partition(".")
            if f_main == cur_main:
                try:
                    return int(f_sub) > int(cur_sub)
                except ValueError:
                    return True
            return False
        try:
            return int(found) > int(cur_main)
        except ValueError:
            return True
    if found.isdigit():
        try:
            return int(found) > int(current)
        except ValueError:
            return True
    return False


def _strip_sidebar_noise(text: str) -> str:
    """Drop trailing margin/sidebar page tokens glued onto PDF text lines."""
    cleaned = re.sub(r"\s+\d{3,4}$", "", text.strip())
    # Also drop lone 3–4 digit tokens that sit after a Total label.
    cleaned = re.sub(r"(?i)^(Total)\s+\d{3,4}$", r"\1", cleaned)
    return cleaned.strip()


def _line_is_total_label_only(text: str) -> bool:
    stripped = _strip_sidebar_noise(text)
    return bool(
        re.match(r"^Total\b", stripped, re.IGNORECASE)
        and not _line_has_big_numbers(stripped)
    )


def _line_is_sidebar_only(text: str) -> bool:
    return bool(re.fullmatch(r"\d{3,4}", text.strip()))


def _line_is_split_total_at(lines: list[dict[str, Any]], idx: int) -> bool:
    if idx < 0 or idx >= len(lines) or not _line_is_total_label_only(lines[idx]["text"]):
        return False
    # Values may sit on the next line, or one sidebar-only line later.
    for nxt in (idx + 1, idx + 2):
        if nxt >= len(lines):
            break
        if nxt == idx + 2 and not _line_is_sidebar_only(lines[idx + 1]["text"]):
            break
        gap = float(lines[nxt]["top"]) - float(lines[idx]["bottom"])
        if gap > SPLIT_TOTAL_MAX_GAP * (2 if nxt > idx + 1 else 1):
            continue
        if _line_has_big_numbers(lines[nxt]["text"]):
            return True
    return False


def _split_total_values_idx(lines: list[dict[str, Any]], idx: int) -> int | None:
    """Index of the numeric values line for a split Total row, else None."""
    if not _line_is_split_total_at(lines, idx):
        return None
    for nxt in (idx + 1, idx + 2):
        if nxt >= len(lines):
            break
        if nxt == idx + 2 and not _line_is_sidebar_only(lines[idx + 1]["text"]):
            break
        if _line_has_big_numbers(lines[nxt]["text"]):
            return nxt
    return None


def _line_is_balance_closing_row(
    text: str,
    lines: list[dict[str, Any]] | None = None,
    idx: int | None = None,
) -> bool:
    stripped = text.strip()
    if not re.match(r"^Balance as at\b", stripped, re.IGNORECASE):
        return False
    if not _line_has_big_numbers(stripped):
        return False
    if re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November)\s+1\b",
        stripped,
        re.I,
    ):
        return False
    # Opening b/f is often labelled "Balance as at December 31" of the prior year.
    # Only treat it as the table end when no further movement rows follow.
    if lines is not None and idx is not None:
        for j in range(idx + 1, min(idx + 10, len(lines))):
            nxt = str(lines[j].get("text") or "").strip()
            if not nxt:
                continue
            if _is_note_section_heading_line(nxt):
                break
            if re.search(
                r"(?i)profit for the year|dividends paid|adjustment for|"
                r"adjusted balance as at january|other comprehensive income|"
                r"unclaimed dividend|acquisition of subsidiary|"
                r"reinstatement of non-controlling",
                nxt,
            ):
                return False
    return True


def _line_is_net_book_value_row(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.match(r"^Net book value as at\b", stripped, re.IGNORECASE)
        and _line_has_big_numbers(stripped)
    )


def _line_is_per_share_row(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.search(r"\bRs\.\)|per (?:ordinary )?share\b", stripped, re.I)
        and re.search(r"\d+\.\d{2}", stripped)
    )


def _line_is_table_end_at(
    lines: list[dict[str, Any]], idx: int, *, title_hint: str = ""
) -> bool:
    if idx < 0 or idx >= len(lines):
        return False
    text = lines[idx]["text"]
    if title_hint and _line_is_note_summary_total(text, title_hint):
        return True
    if _line_is_total_row(text):
        return True
    if _line_is_split_total_at(lines, idx):
        return True
    if _line_is_balance_closing_row(text, lines, idx):
        return True
    if _line_is_net_book_value_row(text):
        return True
    if _line_is_per_share_row(text):
        return True
    if _line_is_net_gross_subtotal_row(text):
        # Gross → Less: impairment → Net is common on SoFP notes.
        # Prefer the Net closing row when it follows shortly after Gross.
        if re.match(r"^Gross\b", text.strip(), re.I):
            for j in range(idx + 1, min(len(lines), idx + 14)):
                nxt = lines[j]["text"].strip()
                if re.match(r"^Net\b", nxt, re.I) and _line_has_big_numbers(nxt):
                    return False
                if _line_is_total_row(nxt) or _line_is_total_label_only(nxt):
                    return False
                if _is_note_section_heading_line(nxt):
                    break
                # Impairment bridge between Gross and Net.
                if re.match(r"^Less:\s*Provision for impairment\b", nxt, re.I):
                    continue
        return True
    return False


def _find_table_end_line(
    lines: list[dict[str, Any]],
    start_idx: int,
    note_ref: str,
    *,
    active_heading_ref: str | None = None,
    title_hint: str = "",
) -> int:
    end_idx = len(lines) - 1
    for idx in range(start_idx + 2, len(lines)):
        text = lines[idx]["text"]
        if _line_is_table_end_at(lines, idx, title_hint=title_hint):
            split_vals = _split_total_values_idx(lines, idx)
            if split_vals is not None:
                return split_vals
            return idx
        if _line_is_sibling_sub_note_boundary(text, note_ref, active_heading_ref):
            return max(start_idx, idx - 1)
        if _line_is_sub_note_start(text, note_ref, active_heading_ref=active_heading_ref):
            return max(start_idx, idx - 1)
        if _line_is_next_note_boundary(text, note_ref):
            return max(start_idx, idx - 1)
    return end_idx


def _table_end_is_valid(
    lines: list[dict[str, Any]], end_idx: int, *, title_hint: str = ""
) -> bool:
    """True when end_idx is a closing Total/summary (including split-Total values)."""
    if _line_is_table_end_at(lines, end_idx, title_hint=title_hint):
        return True
    if end_idx > 0 and _line_is_split_total_at(lines, end_idx - 1):
        return True
    if end_idx > 1 and _line_is_split_total_at(lines, end_idx - 2):
        return True
    return False


def _line_is_total_row(text: str) -> bool:
    return bool(
        re.match(r"^Total\b", text.strip(), re.IGNORECASE)
        and _line_has_big_numbers(text)
    )


def _line_is_net_gross_subtotal_row(text: str) -> bool:
    """
    Net/Gross subtotal rows that close a note table (e.g. 'Net placements').

    Excludes P&L line items like 'Net gains/(losses) from trading' or
    'Net other operating income' that appear mid-table.
    """
    stripped = text.strip()
    if not _line_has_big_numbers(stripped):
        return False
    label = re.sub(r"[\d,\.\(\)\-\+%']+", " ", stripped, flags=re.I)
    label = re.sub(r"\s+", " ", label).strip()
    if not re.match(r"^(Net|Gross)\b", label, re.I):
        return False
    if re.match(r"^(Net|Gross) cash\b", label, re.I):
        return len(label.split()) <= 8
    if re.search(
        r"\bfrom\b|/|\(|\)|\bgain|\bloss|\bfee|\bcommission|\bexpense"
        r"|\bderecognition|\btrading|\brecognis|\baccrued|\bimpairment"
        r"|\bfinancial\b|\bcustomers\b|\binstruments\b",
        label,
        re.I,
    ):
        return False
    # Allow short note totals like "Net interest income" (exclude long P&L lines).
    if re.search(r"\bincome\b", label, re.I) and len(label.split()) <= 4:
        if re.search(r"\bother\b|\boperating\b|\bfrom\b|\btrading\b", label, re.I):
            return False
        return True
    return len(label.split()) <= 6


def _line_is_table_end_row(text: str) -> bool:
    """Last row of a note breakdown table (Total or Net/Gross subtotal)."""
    if _line_is_total_row(text):
        return True
    if _line_is_balance_closing_row(text):
        return True
    if _line_is_net_book_value_row(text):
        return True
    if _line_is_per_share_row(text):
        return True
    return _line_is_net_gross_subtotal_row(text)


def _line_is_sibling_sub_note_boundary(text: str, note_ref: str, active_heading_ref: str | None) -> bool:
    """Next sub-note under the same parent (e.g. 18.2 while capturing 18.1)."""
    if not _is_note_section_heading_line(text):
        return False
    found = _heading_note_ref(text)
    if not found or found == active_heading_ref:
        return False
    if "." not in note_ref.strip():
        main, _ = _note_ref_parts(note_ref)
        if found.startswith(f"{main}."):
            return found != active_heading_ref
    main, sub = _note_ref_parts(note_ref)
    if sub and found.startswith(f"{main}."):
        try:
            return int(found.split(".", 1)[1]) > int(sub)
        except ValueError:
            return True
    return False


def _line_is_sub_note_start(
    text: str,
    note_ref: str,
    *,
    active_heading_ref: str | None = None,
) -> bool:
    """Child note heading while capturing a parent note table (30.1 under note 30)."""
    if not _is_note_section_heading_line(text):
        return False
    found = _heading_note_ref(text)
    if not found:
        return False
    note_ref = note_ref.strip()
    if found == note_ref or found == active_heading_ref:
        return False
    if "." not in note_ref:
        if not found.startswith(f"{note_ref}."):
            return False
        if active_heading_ref and active_heading_ref.startswith(f"{note_ref}."):
            return False
        return True
    return False


def _pick_note_heading(
    lines: list[dict[str, Any]],
    note_ref: str,
    title_hint: str,
) -> tuple[int | None, int | None]:
    """Pick the breakdown-table heading (note title + financial grid below)."""
    best_heading: int | None = None
    best_group: int | None = None
    best_score = -1

    for idx, line in enumerate(lines):
        if not _line_matches_note_heading(line["text"], note_ref, title_hint):
            continue
        active_ref = _heading_note_ref_for_capture(line["text"], note_ref)
        group_idx = _find_financial_table_header_line(
            lines, idx, title_hint=title_hint, note_ref=note_ref
        )
        if group_idx is None:
            continue
        end_idx = _find_table_end_line(
            lines,
            group_idx,
            note_ref,
            active_heading_ref=active_ref,
            title_hint=title_hint,
        )
        score = (end_idx - group_idx) + max(0, 12 - (group_idx - idx))
        if end_idx < len(lines) and _line_is_table_end_at(
            lines, end_idx, title_hint=title_hint
        ):
            score += 50
        if _table_body_matches_hint(lines, group_idx, end_idx, title_hint):
            score += 40
        if score > best_score:
            best_score = score
            best_heading = idx
            best_group = group_idx

    return best_heading, best_group


def _resolve_note_table_start(
    pdf_obj,
    page_num: int,
    heading_idx: int,
    lines: list[dict[str, Any]],
    note_ref: str,
    *,
    title_hint: str = "",
) -> tuple[int, int, int, int, str | None] | None:
    """
    Map a note heading to its financial grid.

    Returns (heading_page, heading_idx, data_page, data_start_idx, active_ref).
    """
    active_ref = _heading_note_ref_for_capture(lines[heading_idx]["text"], note_ref)
    group_idx = _find_financial_table_header_line(
        lines, heading_idx, title_hint=title_hint, note_ref=note_ref
    )
    if group_idx is not None:
        return page_num, heading_idx, page_num, group_idx, active_ref

    for delta in (1, 2):
        next_page = page_num + delta
        if next_page > len(pdf_obj.pages):
            break
        next_lines = _group_page_lines(pdf_obj.pages[next_page - 1])
        if not next_lines:
            continue
        if any(
            _line_is_next_note_boundary(ln["text"], note_ref)
            for ln in next_lines[:8]
        ):
            continue
        group_idx = _find_breakdown_table_after(
            next_lines, 0, note_ref, title_hint=title_hint
        )
        if group_idx is not None:
            return page_num, heading_idx, next_page, group_idx, active_ref
    return None


def _list_sub_note_table_starts_on_page(
    lines: list[dict[str, Any]], note_ref: str
) -> list[tuple[int, int, str | None]]:
    """Sub-note tables for a parent note (41.1 under note 41)."""
    if "." in note_ref.strip():
        return []
    main_ref, _ = _note_ref_parts(note_ref)
    starts: list[tuple[int, int, str | None]] = []
    for idx, line in enumerate(lines):
        heading_ref = _heading_note_ref(line["text"])
        if not heading_ref or not heading_ref.startswith(f"{main_ref}."):
            continue
        if not _is_note_section_heading_line(line["text"]):
            continue
        group_idx = _find_financial_table_header_line(
            lines, idx, title_hint="", note_ref=note_ref
        )
        if group_idx is None:
            continue
        end_idx = _find_table_end_line(
            lines, group_idx, note_ref, active_heading_ref=heading_ref, title_hint=""
        )
        if end_idx <= group_idx or not _line_is_table_end_at(lines, end_idx):
            continue
        starts.append((idx, group_idx, heading_ref))
    return starts


def _list_note_table_starts(
    lines: list[dict[str, Any]],
    note_ref: str,
    title_hint: str,
) -> list[tuple[int, int, str | None]]:
    """All (heading_idx, table_header_idx, heading_ref) on one page."""
    starts: list[tuple[int, int, str | None]] = []
    heading_idxs: list[int] = []
    for idx, line in enumerate(lines):
        if not _line_matches_note_heading(line["text"], note_ref, title_hint):
            continue
        heading_idxs.append(idx)
        active_ref = _heading_note_ref_for_capture(line["text"], note_ref)
        group_idx = _find_financial_table_header_line(
            lines, idx, title_hint=title_hint, note_ref=note_ref
        )
        if group_idx is None:
            continue
        end_idx = _find_table_end_line(
            lines,
            group_idx,
            note_ref,
            active_heading_ref=active_ref,
            title_hint=title_hint,
        )
        if end_idx <= group_idx:
            continue
        if not _table_end_is_valid(lines, end_idx, title_hint=title_hint):
            continue
        if not _table_body_matches_hint(lines, group_idx, end_idx, title_hint):
            # Summary tables (e.g. note 12 Gross income) list child line items
            # without repeating the parent title in every row.
            if active_ref != note_ref.strip():
                continue
        starts.append((idx, group_idx, active_ref))

    # Orphan tables only when this note has no heading on the page (continuation).
    if not starts and not heading_idxs and title_hint:
        orphan_idx = _find_breakdown_table_after(
            lines, 0, note_ref, title_hint=title_hint
        )
        if orphan_idx is not None:
            starts.append((orphan_idx, orphan_idx, note_ref.strip()))
    return starts


def _find_continuation_start(lines: list[dict[str, Any]]) -> int:
    for idx, line in enumerate(lines):
        if float(line["top"]) < PAGE_HEADER_MARGIN:
            continue
        text = line["text"]
        if _line_is_financial_table_header(text):
            return idx
        if _line_has_big_numbers(text):
            return idx
    return 0


def _find_continuation_end(
    lines: list[dict[str, Any]],
    note_ref: str,
    *,
    active_heading_ref: str | None = None,
) -> int | None:
    for idx, line in enumerate(lines):
        text = line["text"]
        if _line_is_table_end_at(lines, idx):
            if _line_is_split_total_at(lines, idx):
                return idx + 1
            return idx
        if _line_is_sibling_sub_note_boundary(text, note_ref, active_heading_ref):
            return max(0, idx - 1)
        if _line_is_sub_note_start(text, note_ref, active_heading_ref=active_heading_ref):
            return max(0, idx - 1)
        if _line_is_next_note_boundary(text, note_ref):
            return max(0, idx - 1)
    if lines and any(_line_has_big_numbers(ln["text"]) for ln in lines[:30]):
        return len(lines) - 1
    return None


def _rect_from_lines(
    page,
    lines: list[dict[str, Any]],
    start_idx: int,
    end_idx: int,
) -> tuple[float, float, float, float]:
    block = lines[start_idx : end_idx + 1]
    label_x0 = min(
        (
            float(word["x0"])
            for ln in block
            for word in ln.get("words") or []
            if re.match(r"^[A-Za-z(]", str(word.get("text") or ""))
        ),
        default=min(ln["x0"] for ln in block),
    )
    x0 = max(0.0, min(label_x0, min(ln["x0"] for ln in block)) - CROP_PAD_X)
    x1 = min(float(page.width), max(ln["x1"] for ln in block) + CROP_PAD_X)
    y0 = max(0.0, float(block[0]["top"]) - CROP_PAD_Y)
    y1 = min(float(page.height), float(block[-1]["bottom"]) + CROP_PAD_Y)
    return x0, y0, x1, y1


def _line_is_simple_year_column_header(text: str) -> bool:
    years = re.findall(r"\b20\d{2}\b", text)
    if len(set(years)) < 2:
        return False
    return not _line_has_big_numbers(text)


def _line_is_rs_unit_header(text: str) -> bool:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return bool(
        re.fullmatch(r"(?i)((rs\.?|lkr)(\s*'?0{3})?\s*){1,8}", compact)
    )


def _line_is_note_amount_table_header(
    lines: list[dict[str, Any]], idx: int
) -> bool:
    """GROUP/BANK grids or simple two-year amount tables (NCI, reserves)."""
    if _line_is_note_page_table_header(lines, idx):
        return True
    if idx < 0 or idx >= len(lines):
        return False
    text = lines[idx]["text"]
    if _line_is_for_year_ended_header(text) or _line_is_amount_year_header(text):
        return True
    if _line_is_simple_year_column_header(text):
        return True
    if _line_is_rs_unit_header(text) and idx > 0 and _line_is_simple_year_column_header(
        lines[idx - 1]["text"]
    ):
        return True
    return False


def _score_breakdown_table_candidate(
    lines: list[dict[str, Any]],
    group_idx: int,
    note_ref: str,
    title_hint: str,
) -> int:
    end_idx = _find_table_end_line(
        lines, group_idx, note_ref, title_hint=title_hint
    )
    score = end_idx - group_idx
    if end_idx < len(lines) and _line_is_table_end_at(
        lines, end_idx, title_hint=title_hint
    ):
        score += 80
    if _table_body_matches_hint(lines, group_idx, end_idx, title_hint):
        score += 60
    if _line_is_note_page_table_header(lines, group_idx):
        score += 25
    return score


def _find_breakdown_table_after(
    lines: list[dict[str, Any]],
    start_idx: int,
    note_ref: str,
    *,
    title_hint: str = "",
    prefer_nearest: bool = False,
) -> int | None:
    """Best GROUP/BANK note breakdown table after start_idx."""
    # After a note heading, take the first header before the next sub-note.
    if prefer_nearest:
        near_limit = min(start_idx + max(NEAR_TABLE_HEADER_WINDOW, TABLE_HEADER_LOOKAHEAD), len(lines))
        for idx in range(start_idx, near_limit):
            text = lines[idx]["text"]
            if idx > start_idx + 1 and _line_is_sub_note_start(
                text, note_ref, active_heading_ref=note_ref.strip() or None
            ):
                break
            if idx > start_idx + 1 and _line_is_next_note_boundary(text, note_ref):
                break
            if _line_is_note_amount_table_header(lines, idx):
                return idx

    limit = min(start_idx + TABLE_HEADER_LOOKAHEAD, len(lines))
    best_idx: int | None = None
    best_score = -1
    for idx in range(start_idx, limit):
        if not _line_is_note_amount_table_header(lines, idx):
            continue
        score = _score_breakdown_table_candidate(
            lines, idx, note_ref, title_hint
        )
        # Prefer nearer headers when scores are close.
        score -= max(0, idx - start_idx) // 4
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx


def _find_financial_table_header_line(
    lines: list[dict[str, Any]],
    start_idx: int,
    *,
    title_hint: str = "",
    note_ref: str = "",
) -> int | None:
    return _find_breakdown_table_after(
        lines,
        start_idx,
        note_ref,
        title_hint=title_hint,
        prefer_nearest=True,
    )


def _find_group_bank_line(lines: list[dict[str, Any]], start_idx: int) -> int | None:
    return _find_financial_table_header_line(lines, start_idx)


def clear_extraction_caches() -> None:
    """Drop in-process PDF/text caches (safe before a timed cold run)."""
    _PRINTED_PAGE_MAP_CACHE.clear()
    _NOTE_PAGES_INDEX_CACHE.clear()
    _FITZ_PAGE_TEXT_CACHE.clear()
    _PAGE_LINES_CACHE.clear()
    try:
        from comb_fs_pdf_extract import _FITZ_TEXT_CACHE

        _FITZ_TEXT_CACHE.clear()
    except Exception:
        pass


# --- Value verification helpers -------------------------------------------
_VALUE_TOKEN_RE = re.compile(r"^[\(\-\u2013\u2014]?\d[\d,]*(?:\.\d+)?\)?$")


def _word_center(word: dict[str, Any]) -> float:
    return (float(word["x0"]) + float(word["x1"])) / 2.0


def _values_close(
    a: float | None,
    b: float | None,
    *,
    rel: float = 0.01,
    abs_tol: float = 100.0,
) -> bool:
    """True when two amounts match within OCR tolerance (handles transpositions)."""
    if a is None or b is None:
        return False
    if a == b:
        return True
    diff = abs(a - b)
    if diff <= abs_tol:
        return True
    return diff <= rel * max(abs(a), abs(b))


def _line_current_year_columns(line: dict[str, Any], year: int) -> list[float]:
    """x-centres of the current-year value columns from a year-header line."""
    cols: list[tuple[float, int]] = []
    for word in line.get("words") or []:
        token = str(word.get("text") or "").strip()
        m = re.fullmatch(r"(20\d{2})", token)
        if m:
            cols.append((_word_center(word), int(m.group(1))))
    cols.sort()
    return [x for x, yr in cols if yr == year]


def _line_numeric_tokens(line: dict[str, Any]) -> list[tuple[float, float]]:
    """(x-centre, value) for numeric amount tokens on a line."""
    out: list[tuple[float, float]] = []
    for word in line.get("words") or []:
        token = str(word.get("text") or "").strip()
        if not _VALUE_TOKEN_RE.match(token):
            continue
        val = parse_number(token)
        if val is None:
            continue
        out.append((_word_center(word), val))
    return out


def _values_at_columns(
    line: dict[str, Any], col_xs: list[float]
) -> list[float | None]:
    """Value nearest each column x-centre (for reading a total row)."""
    nums = _line_numeric_tokens(line)
    out: list[float | None] = []
    for col_x in col_xs:
        best: float | None = None
        best_dx = 18.0
        for x, val in nums:
            dx = abs(x - col_x)
            if dx < best_dx:
                best_dx = dx
                best = val
        out.append(best)
    return out


def _block_total_values(
    block: list[dict[str, Any]],
    end_line: dict[str, Any],
    year: int,
) -> tuple[float | None, float | None]:
    """Return (group_current, bank_current) from a table block's total row."""
    col_xs: list[float] = []
    for line in block:
        cols = _line_current_year_columns(line, year)
        if len(cols) >= len(col_xs):
            col_xs = cols
        if len(col_xs) >= 2:
            break
    if not col_xs:
        nums = _line_numeric_tokens(end_line)
        if not nums:
            return None, None
        nums.sort()
        group = nums[0][1] if nums else None
        bank = nums[len(nums) // 2][1] if len(nums) >= 3 else group
        return group, bank
    values = _values_at_columns(end_line, col_xs)
    group = values[0] if values else None
    bank = values[1] if len(values) >= 2 and values[1] is not None else group
    return group, bank


def _block_contains_expected(
    block: list[dict[str, Any]], expected: float | None
) -> bool:
    """True when any amount in the table matches the FS line (NCI profit, not closing balance)."""
    if expected is None:
        return False
    for line in block:
        for _x, val in _line_numeric_tokens(line):
            if _values_close(val, expected):
                return True
        # Fallback when words were merged into the line text.
        for token in re.findall(
            r"[\(\-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?", str(line.get("text") or "")
        ):
            val = parse_number(token)
            if val is not None and _values_close(val, expected):
                return True
    return False


def _pages_with_note_heading(
    pdf_path: Path, note_ref: str, title_hint: str
) -> list[int]:
    """PDF pages whose text contains the note heading (catches cross-page tables)."""
    if fitz is None:
        return []
    ref = re.escape(note_ref.strip())
    main_ref, sub_ref = _note_ref_parts(note_ref.strip())
    if sub_ref:
        pat = re.compile(rf"(?:^|\n)\s*{ref}(?:\s+|\.\s+)([^\n]+)", re.I)
    else:
        pat = re.compile(
            rf"(?:^|\n)\s*{re.escape(main_ref)}(?:\.\d+)?\.?\s+([^\n]+)",
            re.I,
        )
    tokens = _hint_tokens(title_hint)
    out: list[int] = []
    for i, text in enumerate(_fitz_page_texts(pdf_path)):
        low = text.lower()
        if not _is_notes_section_page(low) or _is_disqualified_note_page(low):
            continue
        if _page_is_primary_financial_statement(low):
            continue
        match = pat.search(text)
        if not match:
            continue
        title_part = match.group(1) if match.lastindex else ""
        if tokens:
            hits = sum(1 for tok in tokens if tok in title_part.lower())
            if hits < min(2, len(tokens)) and not all(
                tok in low for tok in tokens[:2]
            ):
                continue
        out.append(i + 1)
    return out


def locate_note_table_crops(
    pdf_path: Path,
    note_ref: str,
    title_hint: str,
    *,
    year: int | None = None,
    printed_page: int | None = None,
    pdf=None,
    pages: list[int] | None = None,
    expected_group: float | None = None,
    expected_bank: float | None = None,
) -> list[dict[str, Any]]:
    """
    Return crop rectangles for a note's financial table (heading through Total).

    Spans multiple PDF pages when the table continues past a page break.
    When ``expected_group``/``expected_bank`` (the FS total for this note) are
    given, candidate tables are verified by matching their total row so the
    correct table is selected even if several pages mention the note number.
    ``crops[0]`` carries a ``verify`` dict with the matched totals.
    """
    if pdfplumber is None or fitz is None:
        return []

    if pages is None:
        pages = find_note_pages_by_ref(
            pdf_path,
            note_ref,
            title_hint,
            max_pages=8,
            printed_page=printed_page,
            pdf=pdf,
        )
    pages = list(pages or [])

    # Broaden candidates: printed-page hub and any page carrying the note heading
    # (cross-page tables put the heading on the page before the grid). Value
    # verification below then selects the correct table.
    extra: list[int] = []
    if printed_page:
        hub = map_printed_page_to_pdf_page(pdf_path, int(printed_page))
        if hub:
            extra.extend([hub, hub + 1])
    extra.extend(_pages_with_note_heading(pdf_path, note_ref, title_hint))
    heading_pages = [
        p
        for p in extra
        if isinstance(p, int) and p > 0
    ]
    for p in extra:
        if isinstance(p, int) and p > 0 and p not in pages:
            pages.append(p)
    texts = _fitz_page_texts(pdf_path)
    filtered = [
        p
        for p in pages
        if isinstance(p, int)
        and 1 <= p <= len(texts)
        and not _page_is_primary_financial_statement(texts[p - 1].lower())
        and not _is_disqualified_note_page(texts[p - 1].lower())
    ]
    pages = filtered or [
        p
        for p in heading_pages
        if 1 <= p <= len(texts)
        and not _page_is_primary_financial_statement(texts[p - 1].lower())
    ]
    if not pages:
        return []

    def _build_crops_for_start(
        pdf_obj,
        data_page: int,
        data_start_idx: int,
        active_heading_ref: str | None,
    ) -> tuple[list[dict[str, Any]], float | None, float | None]:
        """Build crop segments from the table start to its closing row.

        Returns (crops, total_group, total_bank) where the totals come from the
        table's terminal row (used for value verification).
        """
        crops: list[dict[str, Any]] = []
        segment = 0
        table_x0 = table_x1 = None
        found_end = False
        page_num = data_page
        max_page = min(len(pdf_obj.pages), data_page + 3)
        total_group: float | None = None
        total_bank: float | None = None

        while page_num <= max_page and not found_end:
            page = pdf_obj.pages[page_num - 1]
            lines = _group_page_lines(page)
            if not lines:
                page_num += 1
                continue

            if page_num == data_page:
                start_idx = _find_table_block_top(lines, data_start_idx)
            else:
                start_idx = _find_continuation_start(lines)

            end_idx: int | None = None
            scan_from = data_start_idx if page_num == data_page else start_idx
            for idx in range(scan_from, len(lines)):
                text = lines[idx]["text"]
                if _line_is_table_end_at(lines, idx, title_hint=title_hint):
                    split_vals = _split_total_values_idx(lines, idx)
                    end_idx = split_vals if split_vals is not None else idx
                    found_end = True
                    break
                if _line_is_sibling_sub_note_boundary(
                    text, note_ref, active_heading_ref
                ):
                    end_idx = max(start_idx, idx - 1)
                    found_end = True
                    break
                if _line_is_sub_note_start(
                    text, note_ref, active_heading_ref=active_heading_ref
                ):
                    end_idx = max(start_idx, idx - 1)
                    found_end = True
                    break
                if page_num > data_page and _line_is_next_note_boundary(
                    text, note_ref
                ):
                    end_idx = max(start_idx, idx - 1)
                    found_end = True
                    break

            if end_idx is None:
                end_idx = len(lines) - 1

            if found_end and year is not None:
                block = lines[start_idx : end_idx + 1]
                g, b = _block_total_values(block, lines[end_idx], year)
                if g is not None:
                    total_group, total_bank = g, b

            x0, y0, x1, y1 = _rect_from_lines(page, lines, start_idx, end_idx)
            if table_x0 is None:
                table_x0, table_x1 = x0, x1
            else:
                x0, x1 = table_x0, table_x1

            if not found_end:
                # Extend toward the footer, but never clip rows already in end_idx
                # (PAGE_FOOTER_MARGIN alone can cut the last data line on short pages).
                y1 = max(y1, float(page.height) - PAGE_FOOTER_MARGIN)
                y1 = max(y1, float(lines[end_idx]["bottom"]) + CROP_PAD_Y)
                y1 = min(y1, float(page.height) - 4.0)
            elif page_num > data_page:
                y0 = max(y0, PAGE_HEADER_MARGIN)

            segment += 1
            crops.append(
                {
                    "page": page_num,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "segment": segment,
                }
            )

            if found_end:
                break
            page_num += 1
        return crops, total_group, total_bank

    def _locate_on_pdf(pdf_obj) -> list[dict[str, Any]]:
        # heading_page, heading_idx, data_page, data_start_idx, active_ref
        table_starts: list[tuple[int, int, int, int, str | None]] = []
        seen_start_keys: set[tuple[int, int, str | None]] = set()

        def _add_start(
            heading_page: int,
            heading_idx: int,
            data_page: int,
            data_start_idx: int,
            active_ref: str | None,
        ) -> None:
            key = (data_page, data_start_idx, active_ref)
            if key in seen_start_keys:
                return
            seen_start_keys.add(key)
            table_starts.append(
                (heading_page, heading_idx, data_page, data_start_idx, active_ref)
            )

        for page_num in pages:
            if page_num < 1 or page_num > len(pdf_obj.pages):
                continue
            lines = _group_page_lines(pdf_obj.pages[page_num - 1])
            for idx, line in enumerate(lines):
                if not _line_matches_note_heading(line["text"], note_ref, title_hint):
                    continue
                resolved = _resolve_note_table_start(
                    pdf_obj,
                    page_num,
                    idx,
                    lines,
                    note_ref,
                    title_hint=title_hint,
                )
                if resolved:
                    _add_start(*resolved)

            for heading_idx, group_idx, active_ref in _list_note_table_starts(
                lines, note_ref, title_hint
            ):
                _add_start(page_num, heading_idx, page_num, group_idx, active_ref)

        if not table_starts:
            return []

        note_key = note_ref.strip()
        page_rank = {int(p): i for i, p in enumerate(pages)}
        want_value = expected_group is not None or expected_bank is not None
        hub_pages: set[int] = set()
        if printed_page:
            hub = map_printed_page_to_pdf_page(pdf_path, int(printed_page))
            if hub:
                hub_pages = {hub, hub + 1}

        best_crops: list[dict[str, Any]] | None = None
        best_score = -1.0
        best_verify: dict[str, Any] = {}

        for heading_page, heading_idx, data_page, data_start_idx, active_ref in table_starts:
            if "." not in note_key and active_ref and active_ref != note_key:
                if active_ref.startswith(f"{note_key}."):
                    continue
            # Sub-notes must match their own heading (13.1 not 13).
            if "." in note_key and active_ref and active_ref != note_key:
                continue
            lines = _group_page_lines(pdf_obj.pages[data_page - 1])
            preview = " ".join(
                ln["text"] for ln in lines[data_start_idx : data_start_idx + 8]
            ).lower()
            if "gross income" in preview and (
                "interest income" in preview or "page no" in preview
            ):
                continue
            struct_score = _score_breakdown_table_candidate(
                lines, data_start_idx, note_ref, title_hint
            )
            end_idx = _find_table_end_line(
                lines,
                data_start_idx,
                note_ref,
                active_heading_ref=active_ref,
                title_hint=title_hint,
            )
            row_span = max(0, end_idx - data_start_idx)
            struct_score += row_span * 4
            # Deprioritise one-line summary references in a parent note table.
            if "." in note_key and row_span < 5:
                struct_score -= 400
            struct_score += max(0, 30 - page_rank.get(data_page, 99) * 6)
            if heading_page == data_page and heading_idx < data_start_idx:
                hlines = _group_page_lines(pdf_obj.pages[heading_page - 1])
                if heading_idx < len(hlines) and _line_matches_note_heading(
                    hlines[heading_idx]["text"], note_ref, title_hint
                ):
                    struct_score += 35
            # Hard prefer the printed Page No. hub (FS Note/Page column).
            if hub_pages:
                if data_page in hub_pages:
                    struct_score += 6000
                else:
                    struct_score -= 2500
            # Prefer starts that sit under an exact note heading on the same page.
            if (
                heading_page == data_page
                and heading_idx < data_start_idx
                and active_ref == note_key
            ):
                struct_score += 800

            crops, tot_group, tot_bank = _build_crops_for_start(
                pdf_obj, data_page, data_start_idx, active_ref
            )
            if not crops:
                continue

            group_ok = _values_close(tot_group, expected_group)
            bank_ok = _values_close(tot_bank, expected_bank)
            if not group_ok and expected_group is not None:
                group_ok = _crops_contain_expected(pdf_obj, crops, expected_group)
            if not bank_ok and expected_bank is not None:
                bank_ok = _crops_contain_expected(pdf_obj, crops, expected_bank)
            value_score = 0.0
            if group_ok:
                value_score += 10000.0
            if bank_ok:
                value_score += 10000.0
            # Closeness bonus so the nearest total wins when nothing matches.
            if want_value and expected_group and tot_group:
                rel = abs(tot_group - expected_group) / max(1.0, abs(expected_group))
                value_score += max(0.0, 2000.0 * (1.0 - min(rel, 1.0)))
            # When printed page is known, reject far-off wrong totals harder.
            if hub_pages and want_value and not (group_ok or bank_ok):
                if data_page not in hub_pages:
                    value_score -= 5000.0

            score = value_score + struct_score
            if score > best_score:
                best_score = score
                best_crops = crops
                best_verify = {
                    "expected_group": expected_group,
                    "expected_bank": expected_bank,
                    "captured_group": tot_group,
                    "captured_bank": tot_bank,
                    "group_ok": group_ok,
                    "bank_ok": bank_ok,
                    "value_checked": want_value,
                    "value_verified": bool(group_ok or bank_ok)
                    if want_value
                    else None,
                }

        if not best_crops:
            return []
        best_crops[0]["verify"] = best_verify
        return best_crops

    if pdf is not None:
        try:
            return _locate_on_pdf(pdf)
        except Exception:
            return []

    try:
        with pdfplumber.open(str(pdf_path)) as pdf_obj:
            return _locate_on_pdf(pdf_obj)
    except Exception:
        return []


def build_all_note_pages_index(
    pdf_path: Path,
    items: list[tuple[str, str]],
    *,
    max_pages_per_ref: int = 2,
) -> dict[str, list[int]]:
    """Scan the PDF once and locate pages for every note reference."""
    if fitz is None or not items:
        return {}

    cache_key = _pdf_cache_key(pdf_path)
    # Merge into a per-PDF cache so successive single-ref calls reuse the full scan.
    cached = _NOTE_PAGES_INDEX_CACHE.get(cache_key)
    needed = {(note_ref.strip(), title_hint or "") for note_ref, title_hint in items if note_ref.strip()}
    if cached is not None and all(ref in cached for ref, _ in needed):
        return {
            ref: list(cached.get(ref) or [])[:max_pages_per_ref]
            for ref, _ in needed
        }

    hits: dict[str, list[tuple[int, int]]] = {}
    for note_ref, _ in items:
        key = note_ref.strip()
        if key and key not in hits:
            hits[key] = []

    try:
        texts = _fitz_page_texts(pdf_path)
        for i, text in enumerate(texts):
            low = text.lower()
            page_num = i + 1
            for note_ref, title_hint in items:
                key = note_ref.strip()
                if not key:
                    continue
                score = _score_note_page_text(text, low, key, title_hint or "")
                if score:
                    hits.setdefault(key, []).append((score, page_num))
    except Exception:
        return {}

    out: dict[str, list[int]] = {}
    for key, page_hits in hits.items():
        page_hits.sort(reverse=True)
        out[key] = [p for _, p in page_hits[: max(max_pages_per_ref, 12)]]

    merged = dict(cached or {})
    for key, pages in out.items():
        # Keep highest-scoring pages when extending the cache.
        prev = merged.get(key) or []
        merged[key] = list(dict.fromkeys(list(pages) + list(prev)))[:24]
    _NOTE_PAGES_INDEX_CACHE[cache_key] = merged
    return {k: list(merged.get(k) or [])[:max_pages_per_ref] for k in out}


def _page_has_note_breakdown_table(
    page,
    note_ref: str,
    title_hint: str,
) -> bool:
    """True when pdfplumber sees the note heading plus a financial grid on this page."""
    if page is None:
        return False
    try:
        lines = _group_page_lines(page)
        return bool(_list_note_table_starts(lines, note_ref, title_hint))
    except Exception:
        return False


def _crop_region_has_table_end(
    pdf_page,
    crop: dict[str, Any],
    *,
    title_hint: str = "",
) -> bool:
    lines = _group_page_lines(pdf_page)
    y0 = float(crop.get("y0") or 0)
    y1 = float(crop.get("y1") or 0)
    for idx, line in enumerate(lines):
        if float(line["bottom"]) < y0 or float(line["top"]) > y1:
            continue
        if _line_is_table_end_at(lines, idx, title_hint=title_hint):
            return True
    return False


def _crop_region_has_table_header(pdf_page, crop: dict[str, Any]) -> bool:
    lines = _group_page_lines(pdf_page)
    y0 = float(crop.get("y0") or 0)
    y1 = float(crop.get("y1") or 0)
    for idx, line in enumerate(lines):
        if float(line["bottom"]) < y0 or float(line["top"]) > y1:
            continue
        if _line_is_note_amount_table_header(lines, idx):
            return True
        if _line_is_for_year_ended_header(line["text"]):
            return True
        if _line_is_simple_year_column_header(line["text"]):
            return True
    return False


def _crop_region_has_total(pdf_page, crop: dict[str, Any]) -> bool:
    return _crop_region_has_table_end(pdf_page, crop)


def validate_note_table_crops(
    pdf_path: Path,
    note_ref: str,
    title_hint: str,
    crops: list[dict[str, Any]],
    *,
    pdf=None,
) -> dict[str, Any]:
    """Check crop regions include a note breakdown table and end on Total."""
    issues: list[str] = []
    if not crops:
        return {"ok": False, "issues": ["no_crops"]}
    if pdfplumber is None:
        return {"ok": True, "issues": []}

    def _validate_on_pdf(pdf_obj) -> dict[str, Any]:
        local_issues: list[str] = []
        first_page = int(crops[0]["page"])
        if first_page < 1 or first_page > len(pdf_obj.pages):
            local_issues.append("invalid_first_page")
        elif not any(
            _crop_region_has_table_header(pdf_obj.pages[int(c["page"]) - 1], c)
            for c in crops[:2]
        ):
            local_issues.append("table_header_not_in_first_segment")

        last = crops[-1]
        last_page = int(last["page"])
        if last_page < 1 or last_page > len(pdf_obj.pages):
            local_issues.append("invalid_last_page")
        elif not any(
            _crop_region_has_table_end(
                pdf_obj.pages[int(c["page"]) - 1], c, title_hint=title_hint
            )
            for c in crops
        ):
            local_issues.append("missing_total_row")
        return {"ok": not local_issues, "issues": local_issues}

    try:
        if pdf is not None:
            return _validate_on_pdf(pdf)
        with pdfplumber.open(str(pdf_path)) as pdf_obj:
            return _validate_on_pdf(pdf_obj)
    except Exception:
        issues.append("validation_error")

    return {"ok": not issues, "issues": issues}


def _crops_contain_expected(pdf_obj, crops: list[dict[str, Any]], expected: float | None) -> bool:
    if expected is None or not crops:
        return False
    for crop in crops:
        page_num = int(crop.get("page") or 0)
        if page_num < 1 or page_num > len(pdf_obj.pages):
            continue
        lines = _group_page_lines(pdf_obj.pages[page_num - 1])
        y0 = float(crop.get("y0") or 0)
        y1 = float(crop.get("y1") or 0)
        block = [
            ln
            for ln in lines
            if float(ln["bottom"]) >= y0 - 1 and float(ln["top"]) <= y1 + 1
        ]
        if _block_contains_expected(block, expected):
            return True
    return False


def verify_note_table_crops(
    pdf_path: Path,
    crops: list[dict[str, Any]],
    *,
    year: int,
    expected_group: float | None,
    expected_bank: float | None,
    pdf=None,
) -> dict[str, Any]:
    """Check a captured note table's total row equals the FS total for the note.

    Reads the last crop segment's terminal row and compares GROUP/BANK totals
    against the expected values from the financial statements.
    """
    result: dict[str, Any] = {
        "expected_group": expected_group,
        "expected_bank": expected_bank,
        "captured_group": None,
        "captured_bank": None,
        "group_ok": False,
        "bank_ok": False,
        "value_verified": None,
    }
    if not crops:
        return result

    def _check(pdf_obj) -> dict[str, Any]:
        last = crops[-1]
        page_num = int(last["page"])
        if page_num < 1 or page_num > len(pdf_obj.pages):
            return result
        lines = _group_page_lines(pdf_obj.pages[page_num - 1])
        y0, y1 = float(last.get("y0") or 0), float(last.get("y1") or 0)
        block = [
            ln
            for ln in lines
            if float(ln["bottom"]) >= y0 - 1 and float(ln["top"]) <= y1 + 1
        ]
        if not block:
            return result
        end_line = None
        for ln in reversed(block):
            if _line_has_big_numbers(ln["text"]):
                end_line = ln
                break
        if end_line is None:
            return result
        g, b = _block_total_values(block, end_line, year)
        result["captured_group"] = g
        result["captured_bank"] = b
        result["group_ok"] = _values_close(g, expected_group) or _crops_contain_expected(
            pdf_obj, crops, expected_group
        )
        result["bank_ok"] = _values_close(b, expected_bank) or _crops_contain_expected(
            pdf_obj, crops, expected_bank
        )
        if expected_group is not None or expected_bank is not None:
            result["value_verified"] = bool(result["group_ok"] or result["bank_ok"])
        return result

    try:
        if pdf is not None:
            return _check(pdf)
        with pdfplumber.open(str(pdf_path)) as pdf_obj:
            return _check(pdf_obj)
    except Exception:
        return result


def map_printed_page_to_pdf_page(pdf_path: Path, printed_page: int) -> int | None:
    """
    Map a printed report page number (from FS Note/Page No. column) to a PDF page index.

    Matches footer/header patterns like ``Annual Report 2019 172``.
    """
    if fitz is None or printed_page < 1:
        return None
    cache_key = _pdf_cache_key(pdf_path)
    page_map = _PRINTED_PAGE_MAP_CACHE.get(cache_key)
    if page_map is None:
        page_map = {}
        texts = _fitz_page_texts(pdf_path)
        for i, text in enumerate(texts):
            low = text.lower()
            if _page_is_primary_financial_statement(low) or _is_disqualified_note_page(low):
                continue
            m = re.search(
                r"annual report\s+20\d{2}[ \t]+(\d{2,3})\b",
                text,
                re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r"(?:^|\n)\s*(\d{2,3})\s+commercial bank of ceylon",
                    text,
                    re.IGNORECASE,
                )
            if not m:
                # Common footer: bare page number alone on last non-empty line.
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if lines and re.fullmatch(r"\d{1,4}", lines[-1]):
                    m = re.match(r"(\d{1,4})", lines[-1])
            if not m:
                continue
            try:
                printed = int(m.group(1))
            except Exception:
                continue
            # Prefer first strong match; footer mapping is usually unique.
            page_map.setdefault(printed, i + 1)
        _PRINTED_PAGE_MAP_CACHE[cache_key] = page_map

    if printed_page in page_map:
        return page_map[printed_page]

    # Do not search the body for a bare number — that matches the FS "Page No."
    # column (e.g. Non-controlling interest → 243) and lands on the income statement.
    return None


def _full_scan_note_pages(
    pdf_path: Path,
    note_ref: str,
    title_hint: str,
    *,
    pdf=None,
    exclude: set[int] | None = None,
    min_score: int = 5,
    max_hits: int = 8,
) -> list[int]:
    """Scan all notes-section pages when printed-page / index lookup fails."""
    if fitz is None or pdfplumber is None:
        return []
    exclude = exclude or set()
    scan_hits: list[tuple[int, int]] = []
    try:
        texts = _fitz_page_texts(pdf_path)
        pdf_obj = pdf
        if pdf_obj is None:
            with pdfplumber.open(str(pdf_path)) as opened:
                for i, text in enumerate(texts):
                    low = text.lower()
                    if not _is_notes_section_page(low) or _is_disqualified_note_page(low):
                        continue
                    page_num = i + 1
                    if page_num in exclude:
                        continue
                    score = _score_note_page_text(text, low, note_ref.strip(), title_hint)
                    if score < min_score:
                        continue
                    if page_num < 1 or page_num > len(opened.pages):
                        continue
                    if _page_has_note_breakdown_table(
                        opened.pages[page_num - 1], note_ref, title_hint
                    ):
                        scan_hits.append((score, page_num))
        else:
            for i, text in enumerate(texts):
                low = text.lower()
                if not _is_notes_section_page(low) or _is_disqualified_note_page(low):
                    continue
                page_num = i + 1
                if page_num in exclude:
                    continue
                score = _score_note_page_text(text, low, note_ref.strip(), title_hint)
                if score < min_score:
                    continue
                if page_num < 1 or page_num > len(pdf_obj.pages):
                    continue
                if _page_has_note_breakdown_table(
                    pdf_obj.pages[page_num - 1], note_ref, title_hint
                ):
                    scan_hits.append((score, page_num))
    except Exception:
        return []
    scan_hits.sort(reverse=True)
    out: list[int] = []
    for _, page_num in scan_hits:
        if page_num not in out:
            out.append(page_num)
        if len(out) >= max_hits:
            break
    return out


def find_note_pages_by_ref(
    pdf_path: Path,
    note_ref: str,
    title_hint: str = "",
    *,
    max_pages: int = 4,
    printed_page: int | None = None,
    allow_full_scan: bool = False,
    pdf=None,
) -> list[int]:
    """Find PDF pages containing a specific note number (e.g. 13.1)."""
    if pdfplumber is None:
        return []

    key = note_ref.strip()
    verified: list[int] = []
    candidates: list[int] = []

    if printed_page:
        pdf_page = map_printed_page_to_pdf_page(pdf_path, int(printed_page))
        if pdf_page:
            candidates.append(pdf_page)
            for delta in (1, 2, -1):
                candidates.append(pdf_page + delta)

    found = build_all_note_pages_index(
        pdf_path,
        [(key, title_hint)],
        max_pages_per_ref=max(8, max_pages),
    )
    candidates.extend(found.get(key, []))

    ordered: list[int] = []
    for p in candidates:
        if isinstance(p, int) and p > 0 and p not in ordered:
            ordered.append(p)

    def _rank_note_pages(pdf_obj) -> list[int]:
        ranked: list[tuple[int, int]] = []
        seen: set[int] = set()

        def _rank_page(
            page_num: int, base_score: int = 0, *, force: bool = False
        ) -> None:
            if page_num in seen or page_num < 1 or page_num > len(pdf_obj.pages):
                return
            if not force and not _page_has_note_breakdown_table(
                pdf_obj.pages[page_num - 1], key, title_hint
            ):
                return
            seen.add(page_num)
            lines = _group_page_lines(pdf_obj.pages[page_num - 1])
            table_idx = _find_breakdown_table_after(
                lines, 0, key, title_hint=title_hint
            )
            table_score = (
                _score_breakdown_table_candidate(lines, table_idx, key, title_hint)
                if table_idx is not None
                else 0
            )
            text = _fitz_page_texts(pdf_path)[page_num - 1]
            text_score = _score_note_page_text(
                text, text.lower(), key, title_hint
            )
            ranked.append((base_score + text_score + table_score, page_num))

        for rank, page_num in enumerate(ordered):
            # Printed-page hub pages get a large base score so they win ranking.
            base = max(0, 24 - rank * 3)
            if printed_page:
                hub = map_printed_page_to_pdf_page(pdf_path, int(printed_page))
                if hub and page_num in (hub, hub + 1):
                    base += 80
            _rank_page(page_num, base_score=base)

        for page_num in _pages_with_note_heading(pdf_path, key, title_hint):
            _rank_page(page_num, base_score=12)

        if printed_page:
            hub = map_printed_page_to_pdf_page(pdf_path, int(printed_page))
            if hub:
                texts = _fitz_page_texts(pdf_path)
                hub_low = (
                    texts[hub - 1].lower() if 1 <= hub <= len(texts) else ""
                )
                if not _page_is_primary_financial_statement(hub_low):
                    # Always keep FS Page No. hub in the candidate list.
                    _rank_page(hub, base_score=500, force=True)
                    _rank_page(hub + 1, base_score=400, force=True)
                    _rank_page(hub - 1, base_score=200, force=True)

        ranked.sort(reverse=True)
        out: list[int] = []
        # Lead with hub pages even when ranking prefers elsewhere.
        if printed_page:
            hub = map_printed_page_to_pdf_page(pdf_path, int(printed_page))
            if hub:
                texts = _fitz_page_texts(pdf_path)
                hub_low = (
                    texts[hub - 1].lower() if 1 <= hub <= len(texts) else ""
                )
                if not _page_is_primary_financial_statement(hub_low):
                    for p in (hub, hub + 1, hub - 1):
                        if p > 0 and p not in out:
                            out.append(p)
        for _, page_num in ranked:
            if page_num not in out:
                out.append(page_num)
            if len(out) >= max_pages:
                break
        return out

    def _verify_pages(pdf_obj) -> list[int]:
        return _rank_note_pages(pdf_obj)

    try:
        if pdf is not None:
            verified = _verify_pages(pdf)
            if verified:
                return verified
            if allow_full_scan:
                fallback = _full_scan_note_pages(
                    pdf_path,
                    key,
                    title_hint,
                    pdf=pdf,
                    exclude=set(ordered),
                )
                if fallback:
                    return fallback[:max_pages]
            if printed_page and ordered:
                return ordered[:max_pages]
            return ordered[:max_pages]

        with pdfplumber.open(str(pdf_path)) as pdf_obj:
            verified = _verify_pages(pdf_obj)
            if verified:
                return verified

            if allow_full_scan:
                verified = _full_scan_note_pages(
                    pdf_path,
                    key,
                    title_hint,
                    pdf=pdf_obj,
                    exclude=set(ordered),
                )
                if verified:
                    return verified[:max_pages]

            verified = _full_scan_note_pages(
                pdf_path,
                key,
                title_hint,
                pdf=pdf_obj,
                exclude=set(ordered),
                min_score=8,
            )
            if verified:
                return verified[:max_pages]

            if printed_page and ordered:
                return ordered[:max_pages]
    except Exception:
        return ordered[:max_pages]

    if verified:
        merged = list(dict.fromkeys(verified + ordered))
        return merged[:max_pages]
    return ordered[:max_pages]


def _raw_to_financial_table(
    raw_rows: list[list[str]],
    *,
    page_num: int,
    note_ref: str,
    title: str,
) -> dict[str, Any]:
    header_rows, body_rows = _split_header_body(raw_rows)
    rows: list[dict[str, Any]] = []
    for row in body_rows:
        if not any(str(c).strip() for c in row):
            continue
        rows.append({"cells": [str(c) for c in row], "style": {}})
    for row in header_rows:
        if not any(str(c).strip() for c in row):
            continue
    return {
        "header_rows": [[str(c) for c in r] for r in header_rows],
        "rows": rows,
        "statement_title": title,
        "note_ref": note_ref,
        "page": page_num,
    }


def extract_note_table_from_pdf(
    pdf_path: Path,
    note_ref: str,
    title_hint: str,
    year: int,
    *,
    entity_column: str = "group",
    expected_labels: list[str] | None = None,
) -> dict[str, Any] | None:
    """Extract one note table; return financial_tables-shaped payload."""
    pages = find_note_pages_by_ref(pdf_path, note_ref, title_hint)
    if not pages or pdfplumber is None:
        return None

    best: dict[str, Any] | None = None
    best_filled = 0

    for page_num in pages:
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                if page_num < 1 or page_num > len(pdf.pages):
                    continue
                page_sets: list[tuple[list[list[str]], int]] = []
                raw = _words_table_rows(pdf.pages[page_num - 1])
                if raw:
                    page_sets.append((raw, page_num))
                if page_num < len(pdf.pages):
                    raw_next = _words_table_rows(pdf.pages[page_num])
                    if raw_next:
                        page_sets.append((raw + raw_next, page_num))

                for combined_raw, src_page in page_sets:
                    index = extract_note_breakdown_from_rows(
                        combined_raw,
                        note_ref,
                        title_hint,
                        year,
                        entity_column,
                    )
                    if not index:
                        header_rows, body_rows = _split_header_body(combined_raw)
                        index = _index_table(
                            header_rows, body_rows, year, entity_column
                        )
                    if not index:
                        continue
                    filled = len(index)
                    if expected_labels:
                        mapped = map_labels_to_values(index, expected_labels)
                        filled = sum(1 for v in mapped.values() if v is not None)
                    if filled > best_filled:
                        best_filled = filled
                        best = _raw_to_financial_table(
                            combined_raw,
                            page_num=src_page,
                            note_ref=note_ref,
                            title=f"{note_ref} {title_hint}".strip(),
                        )
                        best["values_index"] = index
                        best["page"] = src_page
        except Exception:
            continue

    return best


def upsert_note_table(
    db,
    *,
    company_slug: str,
    company_name: str,
    year: int,
    note_ref: str,
    parent_label: str,
    table: dict[str, Any],
    source_pdf: str,
    extraction_method: str = "pdfplumber_local",
    extraction_model: str | None = None,
    source_pages: list[int] | None = None,
) -> str:
    """Upsert extracted note into financial_tables. Returns statement_key."""
    sk = _note_statement_key(note_ref)
    now = datetime.now(timezone.utc)
    doc = {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": year,
        "report_type": "annual",
        "report_key": f"Annual {year}",
        "quarter": None,
        "statement_key": sk,
        "statement_label": f"Note {note_ref}",
        "statement_title": table.get("statement_title") or f"Note {note_ref} {parent_label}",
        "table_index": 0,
        "caption": f"Note {note_ref}",
        "header_rows": table.get("header_rows") or [],
        "rows": table.get("rows") or [],
        "row_count": len(table.get("rows") or []),
        "source_pdf": source_pdf,
        "note_ref": note_ref,
        "parent_label": parent_label,
        "source_page": table.get("page"),
        "source_pages": source_pages or ([table.get("page")] if table.get("page") else []),
        "extraction_method": extraction_method,
        "extraction_model": extraction_model,
        "extraction_status": "ok",
        "uploaded_at": now,
        "extracted_at": now,
    }
    db.financial_tables.replace_one(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "statement_key": sk,
            "table_index": 0,
        },
        doc,
        upsert=True,
    )
    return sk


def capture_notes_for_plan(
    db,
    company_slug: str,
    year: int,
    plan: list[dict[str, Any]],
    *,
    pdf_path: Path | None = None,
    entity_column: str = "group",
    force: bool = False,
) -> list[dict[str, Any]]:
    """Capture all note tables referenced in the plan."""
    pdf_path = pdf_path or resolve_annual_pdf(db, company_slug, year)
    if not pdf_path or not pdf_path.exists():
        return [{"ok": False, "reason": "pdf_not_found"}]

    sample = db.financial_tables.find_one(
        {"company_slug": company_slug},
        {"company_name": 1},
    )
    company_name = (
        str(sample.get("company_name"))
        if sample and sample.get("company_name")
        else company_slug.replace("_", " ")
    )

    results: list[dict[str, Any]] = []
    done_refs: set[str] = set()

    for item in plan:
        if not item.get("has_note_table"):
            continue
        note_ref = str(item.get("note_ref") or "").strip()
        if not note_ref or note_ref in done_refs:
            continue
        done_refs.add(note_ref)

        sk = item.get("note_statement_key") or _note_statement_key(note_ref)
        if not force:
            existing = db.financial_tables.find_one(
                {
                    "company_slug": company_slug,
                    "year": year,
                    "statement_key": sk,
                    "row_count": {"$gt": 0},
                }
            )
            if existing:
                results.append(
                    {
                        "ok": True,
                        "note_ref": note_ref,
                        "statement_key": sk,
                        "skipped": True,
                        "row_count": existing.get("row_count"),
                    }
                )
                continue

        child_labels = [c["label"] for c in item.get("children") or []]
        table = extract_note_table_from_pdf(
            pdf_path,
            note_ref,
            item.get("parent_label") or item.get("fs_label") or "",
            year,
            entity_column=entity_column,
            expected_labels=[item.get("parent_label", "")] + child_labels,
        )
        if not table or not table.get("rows"):
            results.append(
                {
                    "ok": False,
                    "note_ref": note_ref,
                    "parent_label": item.get("parent_label"),
                    "reason": "extraction_failed",
                }
            )
            continue

        upsert_note_table(
            db,
            company_slug=company_slug,
            company_name=company_name,
            year=year,
            note_ref=note_ref,
            parent_label=item.get("parent_label") or "",
            table=table,
            source_pdf=str(pdf_path),
        )
        results.append(
            {
                "ok": True,
                "note_ref": note_ref,
                "statement_key": sk,
                "page": table.get("page"),
                "row_count": len(table.get("rows") or []),
            }
        )

    return results


def capture_notes_for_company(
    db,
    company_slug: str,
    year: int,
    *,
    entity_column: str = "group",
    force: bool = False,
) -> dict[str, Any]:
    """Full pipeline: build plan from FS + capture note PDF tables."""
    plan = build_note_capture_plan(db, company_slug, year)
    with_notes = [p for p in plan if p.get("has_note_table")]
    capture_results = capture_notes_for_plan(
        db,
        company_slug,
        year,
        with_notes,
        entity_column=entity_column,
        force=force,
    )
    return {
        "ok": True,
        "company_slug": company_slug,
        "year": year,
        "groups_total": len(plan),
        "groups_with_note_ref": len(with_notes),
        "capture_results": capture_results,
    }


def _note_segment_png_path(
    company_name: str,
    year: int,
    statement_key: str,
    segment: int,
) -> Path:
    return (
        DEMO_CAPTURES_OUT
        / company_name
        / "Annual"
        / str(year)
        / statement_key
        / f"segment_{int(segment):03d}.png"
    )


def _note_capture_png_path(
    company_name: str,
    year: int,
    statement_key: str,
    page: int,
) -> Path:
    return (
        DEMO_CAPTURES_OUT
        / company_name
        / "Annual"
        / str(year)
        / statement_key
        / f"page_{int(page):04d}.png"
    )


def _note_capture_files_exist(
    company_name: str,
    year: int,
    statement_key: str,
    capture_files: list[str] | None,
    page: int | None = None,
) -> bool:
    if capture_files:
        return all(
            (
                DEMO_CAPTURES_OUT
                / company_name
                / "Annual"
                / str(year)
                / statement_key
                / name
            ).is_file()
            for name in capture_files
        )
    if page is None:
        return False
    return _note_capture_png_path(company_name, year, statement_key, page).is_file()


def _note_capture_png_exists(
    company_name: str,
    year: int,
    statement_key: str,
    page: int | None,
    capture_files: list[str] | None = None,
) -> bool:
    return _note_capture_files_exist(
        company_name, year, statement_key, capture_files, page
    )


def _resolve_company_display_name(db, company_slug: str) -> str:
    sample = db.financial_tables.find_one(
        {"company_slug": company_slug},
        {"company_name": 1},
    )
    if sample and sample.get("company_name"):
        return str(sample["company_name"])
    return company_slug.replace("_", " ")


def _save_note_table_crops(
    pdf_path: Path,
    crops: list[dict[str, Any]],
    *,
    company_name: str,
    year: int,
    statement_key: str,
    dpi: int = NOTE_CAPTURE_DPI,
    doc=None,
    force: bool = False,
) -> list[Path]:
    """Rasterise note-table crop regions (GROUP/BANK table only, no policy text)."""
    if fitz is None or not crops:
        return []

    dest_dir = DEMO_CAPTURES_OUT / company_name / "Annual" / str(year) / statement_key
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    own_doc = doc is None

    try:
        if own_doc:
            doc = fitz.open(str(pdf_path))
        assert doc is not None
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for crop in crops:
            page_num = int(crop["page"])
            segment = int(crop.get("segment") or len(saved) + 1)
            out_path = dest_dir / f"segment_{segment:03d}.png"
            if out_path.exists() and not force:
                saved.append(out_path)
                continue
            if page_num < 1 or page_num > len(doc):
                continue
            page = doc[page_num - 1]
            clip = fitz.Rect(
                float(crop["x0"]),
                float(crop["y0"]),
                float(crop["x1"]),
                float(crop["y1"]),
            )
            pix = page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
            pix.save(str(out_path))
            saved.append(out_path)
    except Exception:
        return saved
    finally:
        if own_doc and doc is not None:
            doc.close()

    return saved


def _save_all_note_table_crops(
    pdf_path: Path,
    crop_map: dict[str, list[dict[str, Any]]],
    *,
    company_name: str,
    year: int,
    dpi: int = NOTE_CAPTURE_DPI,
    force: bool = False,
) -> dict[str, list[Path]]:
    if fitz is None or not crop_map:
        return {}

    saved: dict[str, list[Path]] = {}
    try:
        with fitz.open(str(pdf_path)) as doc:
            for statement_key, crops in crop_map.items():
                if not crops:
                    continue
                saved[statement_key] = _save_note_table_crops(
                    pdf_path,
                    crops,
                    company_name=company_name,
                    year=year,
                    statement_key=statement_key,
                    dpi=dpi,
                    doc=doc,
                    force=force,
                )
    except Exception:
        return saved
    return saved


def _save_note_page_images(
    pdf_path: Path,
    pages: list[int],
    *,
    company_name: str,
    year: int,
    statement_key: str,
    dpi: int = NOTE_CAPTURE_DPI,
    doc=None,
) -> list[Path]:
    """Rasterise note PDF pages into Demo_Data_captures for the DB UI."""
    if fitz is None or not pages:
        return []

    dest_dir = DEMO_CAPTURES_OUT / company_name / "Annual" / str(year) / statement_key
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    own_doc = doc is None

    try:
        if own_doc:
            doc = fitz.open(str(pdf_path))
        assert doc is not None
        for page_num in pages:
            if page_num < 1 or page_num > len(doc):
                continue
            out_path = dest_dir / f"page_{page_num:04d}.png"
            if out_path.exists():
                saved.append(out_path)
                continue
            page = doc[page_num - 1]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            pix.save(str(out_path))
            saved.append(out_path)
    except Exception:
        return saved
    finally:
        if own_doc and doc is not None:
            doc.close()

    return saved


def _save_all_note_page_images(
    pdf_path: Path,
    captures: dict[str, list[int]],
    *,
    company_name: str,
    year: int,
    dpi: int = NOTE_CAPTURE_DPI,
) -> dict[str, list[Path]]:
    """Render every note statement in one PDF session."""
    if fitz is None or not captures:
        return {}

    saved: dict[str, list[Path]] = {}
    try:
        with fitz.open(str(pdf_path)) as doc:
            for statement_key, pages in captures.items():
                if not pages:
                    continue
                saved[statement_key] = _save_note_page_images(
                    pdf_path,
                    pages,
                    company_name=company_name,
                    year=year,
                    statement_key=statement_key,
                    dpi=dpi,
                    doc=doc,
                )
    except Exception:
        return saved
    return saved


def upsert_note_capture_meta(
    db,
    *,
    company_slug: str,
    company_name: str,
    year: int,
    note_ref: str,
    parent_label: str,
    source_pdf: str,
    source_page: int,
    source_pages: list[int] | None = None,
    capture_files: list[str] | None = None,
    crop_regions: list[dict[str, Any]] | None = None,
    verify: dict[str, Any] | None = None,
) -> str:
    """Store note page location metadata without extracting table rows."""
    sk = _note_statement_key(note_ref)
    now = datetime.now(timezone.utc)
    pages = source_pages or [source_page]
    files = capture_files or [f"segment_{i:03d}.png" for i in range(1, len(pages) + 1)]
    doc = {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": year,
        "report_type": "annual",
        "report_key": f"Annual {year}",
        "quarter": None,
        "statement_key": sk,
        "statement_label": f"Note {note_ref}",
        "statement_title": f"Note {note_ref} {parent_label}".strip(),
        "table_index": 0,
        "caption": f"Note {note_ref}",
        "header_rows": [],
        "rows": [],
        "row_count": 0,
        "source_pdf": source_pdf,
        "note_ref": note_ref,
        "parent_label": parent_label,
        "source_page": source_page,
        "source_pages": pages,
        "capture_files": files,
        "crop_regions": crop_regions or [],
        "capture_verify": verify or {},
        "value_verified": (verify or {}).get("value_verified"),
        "extraction_method": "table_crop_capture",
        "extraction_model": None,
        "extraction_status": "capture_only",
        "uploaded_at": now,
        "extracted_at": now,
    }
    db.financial_tables.replace_one(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "statement_key": sk,
            "table_index": 0,
        },
        doc,
        upsert=True,
    )
    return sk


def _capture_title_hints(item: dict[str, Any], parent_label: str) -> list[str]:
    """Ordered title hints for locating a note table in the annual PDF."""
    seeds = [
        parent_label,
        str(item.get("fs_label") or ""),
        "",
    ]
    hints: list[str] = []
    for seed in seeds:
        if seed not in hints:
            hints.append(seed)
        if not seed:
            continue
        tokens = _hint_tokens(seed)
        if len(tokens) > 2:
            short = " ".join(tokens[:2])
            if short not in hints:
                hints.append(short)
    return hints


def capture_note_images_for_plan(
    db,
    company_slug: str,
    year: int,
    plan: list[dict[str, Any]],
    *,
    pdf_path: Path | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Locate note pages in one PDF pass, render images in one session."""
    pdf_path = pdf_path or resolve_annual_pdf(db, company_slug, year)
    if not pdf_path or not pdf_path.exists():
        return [{"ok": False, "reason": "pdf_not_found"}]

    incoming = [p for p in (plan or []) if p.get("has_note_table")]
    if incoming:
        # Honour a caller-filtered plan (e.g. income-statement notes only).
        plan = incoming
    else:
        # Refresh plan with PDF Note/Page No. columns (authoritative printed pages).
        plan = build_note_capture_plan(db, company_slug, year, pdf_path=pdf_path)
        plan = [p for p in plan if p.get("has_note_table")]

    company_name = _resolve_company_display_name(db, company_slug)
    results: list[dict[str, Any]] = []
    unique_items: dict[str, dict[str, Any]] = {}
    ref_to_sk: dict[str, str] = {}
    ref_to_parent: dict[str, str] = {}

    for item in plan:
        if not item.get("has_note_table"):
            continue
        note_ref = str(item.get("note_ref") or "").strip()
        if not note_ref or note_ref in unique_items:
            continue
        unique_items[note_ref] = item
        ref_to_sk[note_ref] = item.get("note_statement_key") or _note_statement_key(
            note_ref
        )
        ref_to_parent[note_ref] = item.get("parent_label") or item.get("fs_label") or ""

    existing_by_sk: dict[str, dict[str, Any]] = {}
    if not force and ref_to_sk:
        for doc in db.financial_tables.find(
            {
                "company_slug": company_slug,
                "year": year,
                "statement_key": {"$in": list(ref_to_sk.values())},
                "source_page": {"$ne": None},
            },
            {"statement_key": 1, "source_page": 1, "capture_files": 1, "crop_regions": 1},
        ):
            existing_by_sk[str(doc.get("statement_key"))] = doc

    pending_refs: list[str] = []
    skip_validate: list[tuple[str, str, str, Any, Any, list]] = []
    for note_ref, sk in ref_to_sk.items():
        parent_label = ref_to_parent.get(note_ref, "")
        if not force and sk in existing_by_sk:
            existing = existing_by_sk[sk]
            page = existing.get("source_page")
            capture_files = existing.get("capture_files")
            crop_regions = existing.get("crop_regions") or []
            if _note_capture_png_exists(
                company_name, year, sk, page, capture_files
            ):
                skip_validate.append(
                    (note_ref, sk, parent_label, page, capture_files, crop_regions)
                )
                continue
        pending_refs.append(note_ref)

    # One PDF open for all skip-quality checks (was: open per skipped note).
    if skip_validate:
        try:
            with pdfplumber.open(str(pdf_path)) as skip_pdf:
                for (
                    note_ref,
                    sk,
                    parent_label,
                    page,
                    _capture_files,
                    crop_regions,
                ) in skip_validate:
                    quality = validate_note_table_crops(
                        pdf_path,
                        note_ref,
                        parent_label,
                        crop_regions,
                        pdf=skip_pdf,
                    )
                    if quality.get("ok"):
                        results.append(
                            {
                                "ok": True,
                                "note_ref": note_ref,
                                "statement_key": sk,
                                "skipped": True,
                                "page": page,
                                "capture_only": True,
                            }
                        )
                    else:
                        pending_refs.append(note_ref)
        except Exception:
            pending_refs.extend(item[0] for item in skip_validate)

    if pending_refs:
        # One printed-page map + one note-page fitz index for the whole plan.
        map_printed_page_to_pdf_page(pdf_path, 1)
        index_items: list[tuple[str, str]] = []
        for note_ref in pending_refs:
            item = unique_items.get(note_ref) or {}
            parent_label = ref_to_parent.get(note_ref, "")
            index_items.append((note_ref, parent_label))
            fs_label = str(item.get("fs_label") or "")
            if fs_label and fs_label != parent_label:
                index_items.append((note_ref, fs_label))
        build_all_note_pages_index(
            pdf_path, index_items, max_pages_per_ref=8
        )

        # FS totals per note (group/bank) — anchors the correct table by value.
        expected_by_ref: dict[str, tuple[float | None, float | None]] = {}
        fs_value_memo: dict[tuple[str, str], float | None] = {}

        def _expected_fs(label: str, entity: str) -> float | None:
            key = (label, entity)
            if key not in fs_value_memo:
                fs_value_memo[key] = parent_value_from_statements(
                    db, company_slug, year, label, entity_column=entity
                )
            return fs_value_memo[key]

        for note_ref in pending_refs:
            item = unique_items.get(note_ref) or {}
            fs_label = (
                str(item.get("fs_label") or "")
                or ref_to_parent.get(note_ref, "")
            )
            exp_g = exp_b = None
            if fs_label:
                exp_g = _expected_fs(fs_label, "group")
                exp_b = _expected_fs(fs_label, "bank")
            expected_by_ref[note_ref] = (exp_g, exp_b)

        crop_map: dict[str, list[dict[str, Any]]] = {}
        meta_by_ref: dict[str, dict[str, Any]] = {}
        with pdfplumber.open(str(pdf_path)) as pdf_doc:
            for note_ref in pending_refs:
                parent_label = ref_to_parent.get(note_ref, "")
                item = unique_items.get(note_ref) or {}
                title_hints = _capture_title_hints(item, parent_label)
                page_no = item.get("page_no")
                printed = int(page_no) if page_no else None
                exp_g, exp_b = expected_by_ref.get(note_ref, (None, None))

                best_crops: list[dict[str, Any]] = []
                best_quality: dict[str, Any] = {"ok": False, "issues": ["not_tried"]}
                best_label = parent_label
                best_rank = -1
                did_full_scan = False

                # Deduplicate hints — same label was often tried 3–5×.
                hint_order: list[str] = []
                seen_hints: set[str] = set()
                for hint in [*title_hints, "", parent_label]:
                    key = (hint or "").strip().lower()
                    if key in seen_hints:
                        continue
                    seen_hints.add(key)
                    hint_order.append(hint)

                def _candidate_rank(
                    crops: list[dict[str, Any]], quality: dict[str, Any]
                ) -> int:
                    verify = (crops[0].get("verify") or {}) if crops else {}
                    rank = 0
                    if verify.get("group_ok"):
                        rank += 4
                    if verify.get("bank_ok"):
                        rank += 4
                    if quality.get("ok"):
                        rank += 2
                    return rank

                for hint in hint_order:
                    # When FS Page No. is known, skip expensive full-PDF scans first.
                    pages = find_note_pages_by_ref(
                        pdf_path,
                        note_ref,
                        hint,
                        max_pages=6 if printed else 8,
                        printed_page=printed,
                        allow_full_scan=printed is None and not did_full_scan,
                        pdf=pdf_doc,
                    )
                    if printed is None:
                        did_full_scan = True
                    crops = locate_note_table_crops(
                        pdf_path,
                        note_ref,
                        hint,
                        year=year,
                        printed_page=printed,
                        pdf=pdf_doc,
                        pages=pages,
                        expected_group=exp_g,
                        expected_bank=exp_b,
                    )
                    # Printed-page miss: at most one full-scan retry for this note.
                    if not crops and printed is not None and not did_full_scan:
                        did_full_scan = True
                        pages = find_note_pages_by_ref(
                            pdf_path,
                            note_ref,
                            hint,
                            max_pages=8,
                            printed_page=printed,
                            allow_full_scan=True,
                            pdf=pdf_doc,
                        )
                        crops = locate_note_table_crops(
                            pdf_path,
                            note_ref,
                            hint,
                            year=year,
                            printed_page=printed,
                            pdf=pdf_doc,
                            pages=pages,
                            expected_group=exp_g,
                            expected_bank=exp_b,
                        )
                    if not crops:
                        continue
                    quality = validate_note_table_crops(
                        pdf_path, note_ref, hint, crops, pdf=pdf_doc
                    )
                    rank = _candidate_rank(crops, quality)
                    if rank > best_rank:
                        best_rank = rank
                        best_crops = crops
                        best_quality = quality
                        best_label = hint or parent_label
                    verify = crops[0].get("verify") or {}
                    # Perfect: totals match and the crop is structurally complete.
                    if quality.get("ok") and (
                        verify.get("group_ok") or verify.get("bank_ok")
                    ):
                        break
                    # Strong match: both entity totals OK — stop trying more hints.
                    if verify.get("group_ok") and verify.get("bank_ok"):
                        break
                    # No FS anchor available: first structurally OK crop wins.
                    if quality.get("ok") and not (
                        exp_g is not None or exp_b is not None
                    ):
                        break
                    # Printed hub + structural OK is good enough — avoid more hints.
                    if printed is not None and quality.get("ok") and rank >= 2:
                        break

                crops = best_crops
                quality = best_quality
                parent_label = best_label
                if crops:
                    sk = ref_to_sk[note_ref]
                    crop_map[sk] = crops
                    meta_by_ref[note_ref] = {
                        "crops": crops,
                        "parent_label": parent_label,
                        "quality": quality,
                        "verify": crops[0].get("verify") or {},
                    }

        saved_by_sk = _save_all_note_table_crops(
            pdf_path,
            crop_map,
            company_name=company_name,
            year=year,
            force=force,
        )

        for note_ref in pending_refs:
            sk = ref_to_sk[note_ref]
            parent_label = ref_to_parent.get(note_ref, "")
            meta = meta_by_ref.get(note_ref)
            crops = meta["crops"] if meta else []
            if not crops:
                results.append(
                    {
                        "ok": False,
                        "note_ref": note_ref,
                        "parent_label": parent_label,
                        "reason": "table_region_not_found",
                    }
                )
                continue

            quality = (meta or {}).get("quality") or {}
            if not quality.get("ok"):
                results.append(
                    {
                        "ok": False,
                        "note_ref": note_ref,
                        "parent_label": parent_label,
                        "reason": "incomplete_table_capture",
                        "issues": quality.get("issues") or [],
                        "segments": len(crops),
                    }
                )
                continue

            pages = sorted({int(c["page"]) for c in crops})
            capture_files = [
                f"segment_{int(c.get('segment') or idx + 1):03d}.png"
                for idx, c in enumerate(crops)
            ]
            primary_page = pages[0]
            verify = (meta or {}).get("verify") or {}
            upsert_note_capture_meta(
                db,
                company_slug=company_slug,
                company_name=company_name,
                year=year,
                note_ref=note_ref,
                parent_label=parent_label,
                source_pdf=str(pdf_path),
                source_page=primary_page,
                source_pages=pages,
                capture_files=capture_files,
                crop_regions=crops,
                verify=verify,
            )
            results.append(
                {
                    "ok": True,
                    "note_ref": note_ref,
                    "statement_key": sk,
                    "page": primary_page,
                    "pages": pages,
                    "segments": len(crops),
                    "images_saved": len(saved_by_sk.get(sk) or []),
                    "capture_only": True,
                    "value_verified": verify.get("value_verified"),
                    "expected_group": verify.get("expected_group"),
                    "captured_group": verify.get("captured_group"),
                    "expected_bank": verify.get("expected_bank"),
                    "captured_bank": verify.get("captured_bank"),
                }
            )

    return results


def capture_note_images_for_company(
    db,
    company_slug: str,
    year: int,
    *,
    plan: list[dict[str, Any]] | None = None,
    pdf_path: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Save note page images (no table row extraction)."""
    plan = plan or build_note_capture_plan(
        db, company_slug, year, pdf_path=pdf_path
    )
    with_notes = [p for p in plan if p.get("has_note_table")]
    capture_results = capture_note_images_for_plan(
        db,
        company_slug,
        year,
        with_notes,
        pdf_path=pdf_path,
        force=force,
    )
    return {
        "ok": True,
        "company_slug": company_slug,
        "year": year,
        "groups_total": len(plan),
        "groups_with_note_ref": len(with_notes),
        "capture_results": capture_results,
        "capture_only": True,
    }


def lookup_from_note_table(
    db,
    company_slug: str,
    year: int,
    note_ref: str,
    labels: list[str],
    *,
    entity_column: str = "group",
) -> dict[str, float | None]:
    """Read values for labels from a captured note_* financial_tables doc."""
    from generate_comb_model import find_value_col

    sk = _note_statement_key(note_ref)
    doc = db.financial_tables.find_one(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "statement_key": sk,
        }
    )
    if not doc:
        return {lbl: None for lbl in labels}

    header_rows = doc.get("header_rows") or []
    body_rows: list[list[str]] = []
    for row in doc.get("rows") or []:
        cells = row.get("cells") if isinstance(row, dict) else row
        if cells:
            body_rows.append([str(c) for c in cells])

    index: dict[str, float] = {}
    if doc.get("extraction_method") == "openai_vision" and body_rows:
        index = _index_table(header_rows, body_rows, year, entity_column)
    if not index:
        note_ref = doc.get("note_ref") or ""
        parent = doc.get("parent_label") or ""
        raw_rows = header_rows + body_rows
        index = extract_note_breakdown_from_rows(
            raw_rows,
            note_ref,
            parent,
            year,
            entity_column,
        )
    if not index and body_rows:
        index = _index_table(header_rows, body_rows, year, entity_column)
    if not index:
        # fallback: use find_value_col on stored doc
        col = find_value_col(doc, year)
        if col is not None:
            index = {}
            for row in doc.get("rows") or []:
                cells = row.get("cells") or []
                if not cells or col >= len(cells):
                    continue
                nl = norm_label(str(cells[0]))
                val = parse_number(cells[col])
                if nl and val is not None:
                    index[nl] = val

    return map_labels_to_values(index, labels)
