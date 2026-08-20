"""Extract COMB note captures for the DB table UI with OpenAI vision.

Transcribes every FS-linked note that has a cropped capture image. Reuses the
verbatim table-transcription prompt from the Extracted Tables pipeline and
stores the result on the note document as ui_extracted_tables.
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from openai import OpenAI

from comb_note_capture import DEMO_CAPTURES_OUT
from comb_note_extractor import (
    _effective_row_label,
    _merge_wrap_cell,
    _should_join_wrapped_row,
    resolve_annual_pdf,
)
from comb_note_openai import resolve_api_key
from comb_note_registry import _note_statement_key, build_note_capture_plan
from comb_workbook_store import get_db
from comb_reconcile import parse_number
from generate_comb_model import COMMERCIAL_BANK_SLUG, norm_label
from step3_send_to_openai import (
    PROMPTS,
    _load_b64,
    call_gpt4o,
    extract_json,
)

DEFAULT_YEARS = (2019, 2020, 2021)
PILOT_TARGETS = (
    ("note_12", "Gross income"),
    ("note_13_1", "Interest income"),
    ("note_13_2", "Less: Interest expense"),
)

_YEAR_ONLY_RE = re.compile(r"^(19|20)\d{2}$")
_UNIT_RE = re.compile(
    r"(?i)^(rs\.?|lkr)(\s*'?0{3})?$|^'?0{3}$|^(rs\.?|lkr)\s*'000$"
)
_REAL_AMOUNT_RE = re.compile(
    r"\(?\-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?\-?\d{5,}(?:\.\d+)?\)?"
)
_NOTE_LIKE_RE = re.compile(
    r"^\d{1,2}(?:\.\d{1,2})?(?:\s*\([a-z]\))?(?:\s*&\s*\d{1,2}(?:\.\d{1,2})?(?:\s*\([a-z]\))?)?$",
    re.I,
)
_INCOMPLETE_LABEL_RE = re.compile(
    r"(?:[-–—]|and|to|of|the|from|through|at|for)$",
    re.I,
)


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\ufffd", "\u2013")


# Vertical PDF margin text ("Financial Statements → Notes to the Financial
# Statements") is often OCR'd backwards and glued onto row labels.
_MARGIN_FORWARD_WORDS = (
    "statements",
    "financial",
    "notes",
    "note",
    "report",
    "annual",
    "pages",
    "page",
    "year",
    "these",
    "form",
    "part",
    "integral",
    "the",
    "to",
)


def _reversed_margin_tokens() -> set[str]:
    tokens = {"eht", "ot"}
    for word in _MARGIN_FORWARD_WORDS:
        rev = word[::-1]
        tokens.add(rev)
        min_n = 3 if len(rev) >= 8 else min(4, len(rev))
        for n in range(min_n, len(rev) + 1):
            tokens.add(rev[:n])
    return tokens


_SIDEBAR_NOISE_TOKENS = _reversed_margin_tokens()
_SIDEBAR_NOISE_ALTS = "|".join(sorted(_SIDEBAR_NOISE_TOKENS, key=len, reverse=True))
_SIDEBAR_NOISE_RE = re.compile(rf"\b(?:{_SIDEBAR_NOISE_ALTS})\b", re.I)
_SIDEBAR_NOISE_TRAILING_RE = re.compile(rf"(?:{_SIDEBAR_NOISE_ALTS})$", re.I)
_PAGE_NUM_TOKEN_RE = re.compile(r"^\d{3,4}$")
_TOTAL_LIKE_RE = re.compile(r"(?i)^((?:sub)?totals?)\b(.*)$")


def _is_label_noise_token(token: str) -> bool:
    """True for reversed-margin scraps, prefixes, and glued page numbers."""
    text = re.sub(r"[^a-z0-9]+", "", str(token or "").casefold())
    if not text:
        return True
    if text in _SIDEBAR_NOISE_TOKENS:
        return True
    if _PAGE_NUM_TOKEN_RE.fullmatch(text):
        return True
    return text[::-1] in _MARGIN_FORWARD_WORDS


def _strip_label_noise(text: str) -> str:
    cleaned = _SIDEBAR_NOISE_RE.sub(" ", text)
    cleaned = _SIDEBAR_NOISE_TRAILING_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = cleaned.split()
    while len(parts) > 1 and _is_label_noise_token(parts[-1]):
        parts.pop()
    return " ".join(parts)


def _collapse_total_like_label(text: str) -> str:
    """Map 'Total stne' / 'Total 177' onto the real Total row."""
    match = _TOTAL_LIKE_RE.match(text)
    if not match:
        return text
    head, rest = match.group(1), match.group(2).strip()
    if not rest:
        return head
    rest_tokens = re.findall(r"[A-Za-z0-9]+", rest)
    if rest_tokens and all(_is_label_noise_token(token) for token in rest_tokens):
        return head
    return text
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december"
)
_MONTH_DAY_RE = re.compile(rf"(?i)\b({_MONTH_NAMES})\s+0*(\d{{1,2}})\b")
_DAY_MONTH_RE = re.compile(rf"(?i)\b0*(\d{{1,2}})\s+({_MONTH_NAMES})\b")
_OPPOSING_LABEL_PAIRS = (
    ("january", "december"),
    ("opening", "closing"),
    ("income", "expense"),
    ("asset", "liability"),
    ("addition", "disposal"),
    ("debit", "credit"),
)


def _title_month(value: str) -> str:
    return value[:1].upper() + value[1:].lower()


def _canonicalize_display_label(value: Any) -> str:
    """Stable printed label: dates, trailing commas, footnote markers."""
    text = re.sub(r"\s+", " ", _clean_text(value)).strip()
    text = _strip_label_noise(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*\(\*+\)\s*$", "", text)
    text = re.sub(r"\s*\(\*+\s*$", "", text)
    text = re.sub(r"\s*\*+\s*$", "", text)
    text = re.sub(r"^\d{1,2}(?:\.\d+)?\s+(?=[A-Za-z(])", "", text)
    text = re.sub(
        r"(?i)^((rs\.?|lkr)(\s*'?0{3})?\s+)+",
        "",
        text,
    )
    text = _MONTH_DAY_RE.sub(
        lambda match: f"{_title_month(match.group(1))} {int(match.group(2))}",
        text,
    )
    text = _DAY_MONTH_RE.sub(
        lambda match: f"{int(match.group(1))} {_title_month(match.group(2))}",
        text,
    )
    text = re.sub(r"[,:;]+$", "", text)
    return _collapse_total_like_label(text.strip())


def _clean_row_label(value: Any) -> str:
    """Collapse whitespace and strip trailing footnote markers like (*)."""
    return _canonicalize_display_label(value)


def _normalize_label_key(value: Any) -> str:
    """Cross-year match key: ignore punctuation, date zero-padding, aliases."""
    text = _canonicalize_display_label(value).casefold()
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    text = _DAY_MONTH_RE.sub(
        lambda match: f"{match.group(2).lower()} {int(match.group(1))}",
        text,
    )
    text = re.sub(r"\bprivate\b", "pvt", text)
    text = re.sub(r"\blimited\b", "ltd", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_opposing_label_tokens(left: str, right: str) -> bool:
    for token_a, token_b in _OPPOSING_LABEL_PAIRS:
        if (token_a in left and token_b in right) or (
            token_b in left and token_a in right
        ):
            return True
    return False


def _label_keys_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if _has_opposing_label_tokens(left, right):
        return False
    tokens_a = set(left.split())
    tokens_b = set(right.split())
    if tokens_a and tokens_b:
        smaller, larger = (
            (tokens_a, tokens_b)
            if len(tokens_a) <= len(tokens_b)
            else (tokens_b, tokens_a)
        )
        extra = larger - smaller
        if smaller <= larger and extra and all(
            _is_label_noise_token(token) for token in extra
        ):
            return True
    if SequenceMatcher(None, left, right).ratio() >= 0.9:
        return True
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return overlap >= 0.85 and abs(len(tokens_a) - len(tokens_b)) <= 3


def _label_noise_score(label: str) -> int:
    return sum(
        1
        for token in _normalize_label_key(label).split()
        if _is_label_noise_token(token)
    )


def _preferred_display_label(labels: list[str]) -> str:
    cleaned = [_canonicalize_display_label(label) for label in labels if str(label).strip()]
    if not cleaned:
        return ""
    counts: dict[str, int] = {}
    for label in cleaned:
        counts[label] = counts.get(label, 0) + 1
    return sorted(
        cleaned,
        key=lambda label: (
            _label_noise_score(label),
            -counts[label],
            -len(label),
            label,
        ),
    )[0]


def _cluster_label_map(labels: list[str]) -> dict[str, str]:
    cluster_labels: list[list[str]] = []
    cluster_keys: list[str] = []
    for raw in labels:
        label = _canonicalize_display_label(raw)
        key = _normalize_label_key(label)
        if not key:
            continue
        placed = False
        for idx, existing_key in enumerate(cluster_keys):
            if _label_keys_match(key, existing_key):
                cluster_labels[idx].append(label)
                placed = True
                break
        if not placed:
            cluster_keys.append(key)
            cluster_labels.append([label])
    key_to_display: dict[str, str] = {}
    for group in cluster_labels:
        display = _preferred_display_label(group)
        for label in group:
            key_to_display[_normalize_label_key(label)] = display
    return key_to_display


def align_ui_tables_across_years(
    db,
    years: list[int],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
) -> dict[str, int]:
    """Rewrite matching line-item labels so the same row shares one display name."""
    docs = list(
        db.financial_tables.find(
            {
                "company_slug": company_slug,
                "year": {"$in": list(years)},
                "report_type": "annual",
                "ui_extracted_tables.0": {"$exists": True},
            }
        )
    )
    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        parent = norm_label(str(doc.get("parent_label") or ""))
        if parent:
            by_parent[parent].append(doc)

    updated_docs = 0
    rewritten_rows = 0
    for group in by_parent.values():
        max_tables = max(len(doc.get("ui_extracted_tables") or []) for doc in group)
        table_maps: list[dict[str, str]] = []
        for table_idx in range(max_tables):
            labels: list[str] = []
            for doc in group:
                tables = doc.get("ui_extracted_tables") or []
                if table_idx >= len(tables):
                    continue
                for row in tables[table_idx].get("rows") or []:
                    labels.append(str((row.get("cells") or [""])[0]))
            table_maps.append(_cluster_label_map(labels))

        for doc in group:
            tables = list(doc.get("ui_extracted_tables") or [])
            changed = False
            new_tables = []
            for table_idx, table in enumerate(tables):
                mapping = table_maps[table_idx] if table_idx < len(table_maps) else {}
                new_rows = []
                for row in table.get("rows") or []:
                    cells = list(row.get("cells") or [])
                    if cells:
                        canonical = mapping.get(
                            _normalize_label_key(cells[0]),
                            _canonicalize_display_label(cells[0]),
                        )
                        if canonical and cells[0] != canonical:
                            cells[0] = canonical
                            changed = True
                            rewritten_rows += 1
                    new_rows.append({**row, "cells": cells})
                new_tables.append({**table, "rows": new_rows})
            if not changed:
                continue
            db.financial_tables.update_one(
                {"_id": doc["_id"]},
                {"$set": {"ui_extracted_tables": new_tables}},
            )
            updated_docs += 1

    return {"docs": updated_docs, "rows": rewritten_rows}


def repair_stored_ui_tables(
    db,
    years: list[int],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
) -> dict[str, int]:
    """Re-apply label repair to stored UI tables without calling OpenAI."""
    docs = list(
        db.financial_tables.find(
            {
                "company_slug": company_slug,
                "year": {"$in": list(years)},
                "report_type": "annual",
                "ui_extracted_tables.0": {"$exists": True},
            }
        )
    )
    updated_docs = 0
    rewritten_rows = 0
    for doc in docs:
        original = list(doc.get("ui_extracted_tables") or [])
        repaired = repair_note_ui_tables(original)
        if not repaired or repaired == original:
            continue
        db.financial_tables.update_one(
            {"_id": doc["_id"]},
            {"$set": {"ui_extracted_tables": repaired}},
        )
        updated_docs += 1
        rewritten_rows += sum(len(table.get("rows") or []) for table in repaired)
    return {"repair_docs": updated_docs, "repair_rows": rewritten_rows}


def repair_and_align_ui_tables(
    db,
    years: list[int],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
) -> dict[str, int]:
    """Clean leftover OCR labels, then merge matching rows across years."""
    repaired = repair_stored_ui_tables(db, years, company_slug=company_slug)
    aligned = align_ui_tables_across_years(db, years, company_slug=company_slug)
    return {**repaired, **aligned}


def _looks_like_year_only(text: str) -> bool:
    first = text.strip().split("\n", 1)[0].strip()
    return bool(_YEAR_ONLY_RE.fullmatch(first)) and "\n" not in text.strip()


def _looks_like_unit_only(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text.strip())
    if not compact:
        return False
    return bool(_UNIT_RE.fullmatch(compact)) or bool(
        re.fullmatch(r"(?i)(rs\.?|lkr)\s*'?0{3}", compact)
    )


def _merge_year_unit_header_rows(header_rows: list[list[str]]) -> list[list[str]]:
    """Combine split year / Rs.'000 header lines into one cell (as in the PDF)."""
    if len(header_rows) < 2:
        return header_rows

    merged: list[list[str]] = [list(header_rows[0])]
    for row in header_rows[1:]:
        prev = merged[-1]
        curr = list(row)
        width = max(len(prev), len(curr))
        while len(prev) < width:
            prev.append("")
        while len(curr) < width:
            curr.append("")

        pairs = 0
        for idx in range(width):
            top = prev[idx].strip()
            bottom = curr[idx].strip()
            if _looks_like_year_only(top) and _looks_like_unit_only(bottom):
                prev[idx] = f"{top}\n{bottom}"
                curr[idx] = ""
                pairs += 1

        if pairs > 0 and all(not cell.strip() for cell in curr):
            continue
        merged.append(curr)
    return merged


def _looks_like_row_description(text: str) -> bool:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return False
    if _NOTE_LIKE_RE.match(raw) or re.fullmatch(r"\d{2,3}(?:\s*&\s*\d{2,3})?", raw):
        return False
    if _REAL_AMOUNT_RE.fullmatch(raw):
        return False
    letters = sum(ch.isalpha() for ch in raw)
    return letters >= 8


def _promote_misplaced_description(cells: list[str]) -> list[str]:
    """Move a wrapped label that landed in a Note/amount column back to col 0."""
    out = [str(c) for c in cells]
    if not out:
        return out
    if _clean_row_label(out[0]):
        return out
    for idx in range(1, len(out)):
        if _looks_like_row_description(out[idx]):
            out[0] = _clean_row_label(out[idx])
            out[idx] = ""
            break
    return out


def _clean_amount_cell(text: str) -> str:
    """Keep printed amounts and pure note/page refs; unwrap mashed cells."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return ""
    matches = list(_REAL_AMOUNT_RE.finditer(raw))
    if matches:
        has_junk = bool(
            re.search(
                r"\d{1,2}\.\d{1,2}\s*\([a-z]\)|&|\bpage\b|\b\d{1,2}\.\d{1,2}\b",
                raw,
                re.I,
            )
        )
        if (
            has_junk
            or len(matches) > 1
            or re.search(r"[A-Za-z]", raw)
            or not _REAL_AMOUNT_RE.fullmatch(raw)
        ):
            return matches[-1].group(0)
        return raw
    if _NOTE_LIKE_RE.match(raw) or re.fullmatch(r"\d{2,3}(?:\s*&\s*\d{2,3})?", raw):
        return raw
    return raw


def _row_has_real_amounts(cells: list[str]) -> bool:
    for cell in cells[1:]:
        if _REAL_AMOUNT_RE.search(str(cell or "")):
            return True
    return False


def note_table_missing_amounts(table: dict[str, Any]) -> str | None:
    """Return a short reason when body amounts are clearly short of the printed total."""
    rows = [row for row in (table.get("rows") or []) if isinstance(row, dict)]
    if len(rows) < 3:
        return None
    total_cells = None
    body: list[list[str]] = []
    for row in rows:
        cells = [str(c) for c in (row.get("cells") or [])]
        label = _clean_row_label(cells[0] if cells else "")
        style = str(row.get("style") or "").lower()
        if style == "total" or re.fullmatch(r"(?i)totals?", label):
            total_cells = cells
        else:
            body.append(cells)
    if not total_cells:
        return None
    width = max((len(cells) for cells in body + [total_cells]), default=0)
    for col in range(1, width):
        total = parse_number(
            _clean_amount_cell(total_cells[col] if col < len(total_cells) else "")
        )
        if total is None or abs(total) < 1_000:
            continue
        child_sum = 0.0
        counted = 0
        for cells in body:
            value = parse_number(
                _clean_amount_cell(cells[col] if col < len(cells) else "")
            )
            if value is None:
                continue
            child_sum += value
            counted += 1
        if counted >= 2 and child_sum + max(1.0, abs(total) * 0.002) < abs(total):
            return f"col {col} sum={child_sum:,.0f} total={total:,.0f}"
        break
    return None


def _is_incomplete_label(label: str) -> bool:
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    if not text:
        return False
    if text.endswith(("-", "–", "—")):
        return True
    return bool(_INCOMPLETE_LABEL_RE.search(text))


_WRAP_FRAGMENTS = {
    "instruments",
    "customers",
    "other customers",
    "other comprehensive income",
    "comprehensive income",
    "right-of-use assets",
    "plant and equipment",
    "private limited",
    "company plc",
    "company limited",
    "limited",
    "plc",
    "brokers private limited",
    "development company plc",
}
def _is_short_wrap_fragment(label: str) -> bool:
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    if not text:
        return False
    if text.lower() in _WRAP_FRAGMENTS:
        return True
    if text[:1].islower():
        return True
    if re.fullmatch(
        r"(?i)(company(\s+plc)?|private limited|brokers(\s+private)?(\s+limited)?|plc|limited)",
        text,
    ):
        return True
    return False


def _is_continuation_label(label: str, prev_label: str) -> bool:
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    prev = re.sub(r"\s+", " ", str(prev_label or "")).strip()
    if not text or not prev:
        return False
    if _is_incomplete_label(prev):
        return True
    if _is_short_wrap_fragment(text):
        return True
    return False


def _join_wrapped_note_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join wrapped labels, note/page continuations, and orphan amount lines."""
    out: list[dict[str, Any]] = []
    for row in rows:
        cells = [str(c) for c in (row.get("cells") or [])]
        if not cells:
            continue
        cells[0] = _clean_row_label(cells[0])
        if not out:
            out.append({**row, "cells": cells})
            continue
        prev = out[-1]
        prev_cells = list(prev.get("cells") or [])
        if _should_join_wrapped_row(prev_cells, cells):
            extra = _effective_row_label(cells[0])
            if extra:
                prev_label = _clean_row_label(prev_cells[0] if prev_cells else "")
                prev_cells[0] = re.sub(
                    r"\s+", " ", (prev_label + " " + extra)
                ).strip()
            width = max(len(prev_cells), len(cells))
            while len(prev_cells) < width:
                prev_cells.append("")
            while len(cells) < width:
                cells.append("")
            for idx in range(1, width):
                prev_cells[idx] = _merge_wrap_cell(prev_cells[idx], cells[idx])
            prev["cells"] = prev_cells
            if str(row.get("style") or "").lower() == "total":
                prev["style"] = "total"
            continue
        out.append({**row, "cells": cells})
    return out


def _apply_hanging_prefixes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repeat a dash-ended category prefix onto the following amount rows."""
    out: list[dict[str, Any]] = []
    prefix = ""
    for row in rows:
        cells = list(row.get("cells") or [])
        label = _clean_row_label(cells[0] if cells else "")
        style = str(row.get("style") or "").lower()
        is_total = style == "total" or bool(re.fullmatch(r"(?i)totals?", label))
        if is_total:
            prefix = ""
            if cells:
                cells[0] = label
            out.append({**row, "cells": cells})
            continue
        has_amt = _row_has_real_amounts(cells)
        if label and not has_amt and re.search(r"[–—-]\s*$", label):
            after_dash = re.split(r"[–—-]", label)[-1].strip()
            if not after_dash:
                prefix = label.rstrip()
                continue
        if prefix and label and label[:1].islower():
            prefix = ""
        if prefix and label and not label[:1].islower():
            prefix_core = re.sub(r"[\s–—-]+$", "", prefix).casefold()
            if not label.casefold().startswith(prefix_core[:24]):
                label = f"{prefix} {label.lstrip(' –—-')}".strip()
        if cells:
            cells[0] = label
        out.append({**row, "cells": cells})
    return out


def _drop_junk_note_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        cells = list(row.get("cells") or [])
        label = _clean_row_label(cells[0] if cells else "")
        blob = " ".join(str(c) for c in cells).strip().lower()
        if not label and not _row_has_real_amounts(cells):
            continue
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,2})?", label) and not _row_has_real_amounts(
            cells
        ):
            continue
        if re.search(r"for the year ended|as at december|page no\.?", blob) and not (
            label and _row_has_real_amounts(cells)
        ):
            continue
        if re.search(r"commercial bank of ceylon|annual report 20\d{2}", blob) and not (
            label and _row_has_real_amounts(cells)
        ):
            continue
        if re.fullmatch(r"(?i)(group|bank|company)", label) and not _row_has_real_amounts(
            cells
        ):
            continue
        if re.search(
            r"accounting policy|the group measures|slfrs\s*\d|lkas\s*\d|ifrs\s*\d|"
            r"expected credit loss|this note provides",
            blob,
        ) and not _row_has_real_amounts(cells):
            continue
        if len(label) > 140 and not _row_has_real_amounts(cells):
            continue
        out.append({**row, "cells": cells})
    return out


def _is_junk_header_row(row: list[str]) -> bool:
    filled = [str(c).strip() for c in row if str(c).strip()]
    if not filled:
        return True
    blob = " ".join(filled).lower()
    if re.search(r"\b20\d{2}\b", blob) or re.search(
        r"\bgroup\b|\bbank\b|\bcompany\b", blob
    ):
        return False
    if re.search(r"note.*page no|rs\.?\s*'?0{3}.*rs\.?\s*'?0{3}", blob):
        return True
    if re.fullmatch(
        r"(rs\.?|lkr)(\s*'?0{3})?(\s+(rs\.?|lkr)(\s*'?0{3})?)+", blob
    ):
        return True
    return False


def _peel_trailing_amount(cells: list[str]) -> list[str]:
    """Move a year amount that pdfplumber glued onto the description back to col 1."""
    if not cells:
        return cells
    label = cells[0]
    match = re.search(
        r"^(.*\S)\s+(\(?\-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?)\s*$",
        label,
    )
    if not match:
        return cells
    rest = match.group(1).strip()
    amount = match.group(2)
    if len(rest) < 8:
        return cells
    out = list(cells)
    out[0] = rest
    if len(out) == 1:
        out.append(amount)
    elif not str(out[1]).strip() or not _REAL_AMOUNT_RE.search(str(out[1])):
        out[1] = amount
    return out


def _fix_movement_opening_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """If opening and closing were given the same date, restore January vs December."""
    balance_idxs = []
    for idx, row in enumerate(rows):
        label = _canonicalize_display_label((row.get("cells") or [""])[0])
        if re.match(r"(?i)^balance as at\b", label) and not re.match(
            r"(?i)^adjusted\b", label
        ):
            balance_idxs.append(idx)
    if len(balance_idxs) < 2:
        return rows
    first_i, last_i = balance_idxs[0], balance_idxs[-1]
    first_cells = list(rows[first_i].get("cells") or [])
    last_cells = list(rows[last_i].get("cells") or [])
    if not first_cells or not last_cells:
        return rows
    first_key = _normalize_label_key(first_cells[0])
    last_key = _normalize_label_key(last_cells[0])
    if first_key != last_key:
        return rows
    if "december" in first_key:
        first_cells[0] = "Balance as at January 1"
        rows[first_i] = {**rows[first_i], "cells": first_cells}
    elif "january" in first_key:
        last_cells[0] = "Balance as at December 31"
        rows[last_i] = {**rows[last_i], "cells": last_cells}
    return rows


def repair_note_ui_table(table: dict[str, Any]) -> dict[str, Any]:
    """Fix wrapped labels, hanging prefixes, and mashed note/page amount cells."""
    header_rows = [
        [_clean_text(cell) for cell in row]
        for row in (table.get("header_rows") or [])
        if isinstance(row, list)
    ]
    header_rows = [
        row
        for row in _merge_year_unit_header_rows(header_rows)
        if any(str(c).strip() for c in row)
        and not _is_junk_header_row(row)
        and not (
            len([c for c in row if str(c).strip()]) == 1
            and re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?", str(row[1] if len(row) > 1 else row[0]).strip())
        )
    ]
    rows = []
    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cells = _promote_misplaced_description(
            [_clean_text(c) for c in (row.get("cells") or [])]
        )
        if cells:
            cells[0] = _clean_row_label(cells[0])
        rows.append({**row, "cells": cells})
    rows = _apply_hanging_prefixes(rows)
    rows = _join_wrapped_note_rows(rows)
    rows = _drop_junk_note_rows(rows)
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        cells = [_clean_text(c) for c in (row.get("cells") or [])]
        if cells:
            cells[0] = _clean_row_label(cells[0])
            cells = _peel_trailing_amount(cells)
            cells[1:] = [_clean_amount_cell(c) for c in cells[1:]]
        label = cells[0] if cells else ""
        style = str(row.get("style") or "").strip().lower()
        if label and re.fullmatch(r"(?i)totals?", label):
            style = "total"
        cleaned.append({**row, "cells": cells, "style": style or row.get("style")})
    cleaned = _fix_movement_opening_labels(cleaned)
    non_totals = [
        r
        for r in cleaned
        if str(r.get("style") or "").lower() != "total"
        and not re.fullmatch(r"(?i)totals?", _clean_row_label((r.get("cells") or [""])[0]))
    ]
    totals = [
        r
        for r in cleaned
        if str(r.get("style") or "").lower() == "total"
        or re.fullmatch(r"(?i)totals?", _clean_row_label((r.get("cells") or [""])[0]))
    ]
    for total_row in totals:
        total_row["style"] = "total"
    return {
        "caption": _clean_text(table.get("caption")) or None,
        "header_rows": header_rows,
        "rows": non_totals + totals,
    }


def repair_note_ui_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repaired = [
        repair_note_ui_table(table)
        for table in tables
        if isinstance(table, dict)
    ]
    return [table for table in repaired if table.get("rows")]


def _tables_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    tables = payload.get("tables")
    if not isinstance(tables, list):
        return []
    valid: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            continue
        header_rows = [
            [_clean_text(cell) for cell in row]
            for row in (table.get("header_rows") or [])
            if isinstance(row, list)
        ]
        cleaned_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cells = [_clean_text(cell) for cell in (row.get("cells") or [])]
            if cells:
                cells[0] = _clean_row_label(cells[0])
            cleaned_rows.append({**row, "cells": cells})
        repaired = repair_note_ui_table(
            {
                "caption": table.get("caption"),
                "header_rows": header_rows,
                "rows": cleaned_rows,
            }
        )
        if repaired.get("rows"):
            valid.append(repaired)
    return valid


def _merge_segment_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stitch multi-image segments into one table (headers from first segment)."""
    if len(tables) <= 1:
        return repair_note_ui_tables(tables)
    base = {
        "caption": tables[0].get("caption"),
        "header_rows": list(tables[0].get("header_rows") or []),
        "rows": [],
    }
    seen_labels: set[str] = set()
    pending_totals: list[dict[str, Any]] = []
    for table in tables:
        if not base["header_rows"] and table.get("header_rows"):
            base["header_rows"] = list(table.get("header_rows") or [])
        for row in table.get("rows") or []:
            cells = list(row.get("cells") or [])
            if cells:
                cells[0] = _clean_row_label(cells[0])
            label = cells[0] if cells else ""
            if not label and not _row_has_real_amounts(cells):
                continue
            style = str(row.get("style") or "").lower()
            is_total = style == "total" or bool(re.fullmatch(r"(?i)totals?", label))
            norm = _normalize_label_key(label) if label else ""
            if norm and norm in seen_labels:
                continue
            if norm:
                seen_labels.add(norm)
            cleaned_row = {**row, "cells": cells}
            if is_total:
                pending_totals.append({**cleaned_row, "style": "total"})
            else:
                base["rows"].append(cleaned_row)
    base["rows"].extend(pending_totals)
    repaired = repair_note_ui_tables([base] if base["rows"] else tables)
    return repaired if repaired else tables


def _capture_paths(doc: dict[str, Any]) -> list[Path]:
    company_name = str(doc.get("company_name") or "").strip()
    statement_key = str(doc.get("statement_key") or "").strip()
    year = int(doc.get("year"))
    root = DEMO_CAPTURES_OUT / company_name / "Annual" / str(year) / statement_key
    names = [str(name) for name in (doc.get("capture_files") or [])]
    paths = [root / name for name in names]
    existing = [path for path in paths if path.is_file()]
    if existing:
        return existing
    # Fallback: sequential segments on disk (exclude oversized stray pages).
    return sorted(
        path
        for path in root.glob("segment_*.png")
        if path.is_file() and path.stat().st_size < 200_000
    )


def _discover_targets(db, year: int) -> list[tuple[str, str]]:
    """All FS-linked notes for the year, in report order."""
    pdf_path = resolve_annual_pdf(db, COMMERCIAL_BANK_SLUG, year)
    plan = build_note_capture_plan(
        db, COMMERCIAL_BANK_SLUG, year, pdf_path=pdf_path
    )
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in plan:
        if not item.get("has_note_table"):
            continue
        statement_key = str(item.get("note_statement_key") or "").strip()
        description = str(
            item.get("fs_label") or item.get("parent_label") or ""
        ).strip()
        if not statement_key or not description or statement_key in seen:
            continue
        seen.add(statement_key)
        targets.append((statement_key, description))
    return targets


def run(
    *,
    years: list[int],
    model: str,
    force: bool,
    keys: list[str] | None = None,
    skip_failed: bool = True,
    pilot: bool = False,
) -> dict[str, Any]:
    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured")

    db, mongo_client = get_db()
    openai_client = OpenAI(api_key=api_key)
    results: list[dict[str, Any]] = []
    key_filter = {str(key).strip() for key in (keys or []) if str(key).strip()}

    try:
        for year in years:
            targets = list(PILOT_TARGETS) if pilot else _discover_targets(db, year)
            if key_filter:
                targets = [
                    item for item in targets if item[0] in key_filter
                ]
            print(
                f"[ui-note] {year} · {len(targets)} note(s) queued",
                flush=True,
            )
            for statement_key, description in targets:
                query = {
                    "company_slug": COMMERCIAL_BANK_SLUG,
                    "year": year,
                    "report_type": "annual",
                    "statement_key": statement_key,
                }
                doc = db.financial_tables.find_one(query)
                if not doc:
                    results.append(
                        {
                            "ok": False,
                            "year": year,
                            "description": description,
                            "reason": "note_document_not_found",
                        }
                    )
                    continue
                verify = doc.get("capture_verify") or {}
                if skip_failed and verify.get("value_verified") is False:
                    results.append(
                        {
                            "ok": True,
                            "year": year,
                            "description": description,
                            "reason": "capture_not_verified",
                            "skipped": True,
                        }
                    )
                    continue
                if doc.get("ui_extracted_tables") and not force:
                    cleaned = _tables_from_payload(
                        {"tables": doc.get("ui_extracted_tables")}
                    )
                    if cleaned != doc.get("ui_extracted_tables"):
                        db.financial_tables.update_one(
                            query, {"$set": {"ui_extracted_tables": cleaned}}
                        )
                    results.append(
                        {
                            "ok": True,
                            "year": year,
                            "description": description,
                            "skipped": True,
                        }
                    )
                    continue

                images = _capture_paths(doc)
                if not images:
                    results.append(
                        {
                            "ok": False,
                            "year": year,
                            "description": description,
                            "reason": "capture_image_not_found",
                        }
                    )
                    continue

                print(
                    f"[ui-note] {year} · {description} · {len(images)} capture(s)",
                    flush=True,
                )
                prompt = (
                    f"Target note: {doc.get('note_ref')} — {description}.\n"
                    "Find the numbered heading that matches this topic "
                    f'(example: "{doc.get("note_ref")}. {description}") and transcribe '
                    "ONLY the numerical table under that heading.\n"
                    "Ignore Accounting policy boxes, narrative paragraphs, SLFRS/LKAS text, "
                    "and any other note on the page.\n"
                    "The supplied image(s) are cropped to the target note table. "
                    "If multiple images are provided, they are sequential segments of THE SAME "
                    "table (continuation pages). Merge them into ONE table in reading order.\n"
                    "Transcribe EVERY data row exactly as printed — do not skip middle rows.\n"
                    "Put the Total / Balance as at December 31 row last and mark it with "
                    'style "total".\n'
                    "Do not invent rows. Do not copy prose into the description column.\n"
                    "A description cell must be a short line-item label, never a paragraph.\n"
                    "Ignore vertical or rotated margin text such as "
                    "'Financial Statements' / 'Notes to the Financial Statements'. "
                    "Never append those letters, reversed fragments (e.g. 'stne', "
                    "'stnemetats'), or page numbers to a row label. "
                    "A total line must be labeled exactly 'Total'.\n"
                    "LABEL RULE: strip trailing commas. Write calendar dates as "
                    '"January 1" / "December 31" (no leading zeros). Merge wrapped '
                    "description lines into one row. Use the same short wording a "
                    "later year would use for the same line item.\n\n"
                    "HEADER RULE: Year and unit belong in ONE cell when printed that way "
                    '(example: "2020\\nRs. \'000"). Never put "Rs. \'000" on its own '
                    "header_rows line under the year.\n"
                    "If a note has both a tax-rate / % table and an amount table "
                    "(Rs. '000 / LKR), transcribe both and keep % in the rate-column headers.\n"
                    "Never copy a Change % or tax-rate figure into an amount column.\n\n"
                    f"{PROMPTS['notes']}"
                )
                raw = call_gpt4o(
                    openai_client,
                    [_load_b64(path) for path in images],
                    prompt,
                    model=model,
                )
                payload = extract_json(raw)
                tables = _merge_segment_tables(_tables_from_payload(payload))
                if not tables:
                    results.append(
                        {
                            "ok": False,
                            "year": year,
                            "description": description,
                            "reason": "no_valid_tables_in_response",
                        }
                    )
                    continue

                now = datetime.now(timezone.utc)
                db.financial_tables.update_one(
                    query,
                    {
                        "$set": {
                            "ui_extracted_tables": tables,
                            "ui_extraction_method": "openai_verbatim_vision",
                            "ui_extraction_model": model,
                            "ui_extracted_at": now,
                        }
                    },
                )
                results.append(
                    {
                        "ok": True,
                        "year": year,
                        "description": description,
                        "table_count": len(tables),
                        "row_count": sum(len(t.get("rows") or []) for t in tables),
                        "sample_header": (tables[0].get("header_rows") or [None])[-1]
                        if tables
                        else None,
                    }
                )
        aligned = repair_and_align_ui_tables(db, years)
        print(
            f"[ui-note] repaired {aligned.get('repair_docs', 0)} doc(s); "
            f"aligned {aligned['rows']} row(s) on {aligned['docs']} doc(s)",
            flush=True,
        )
    finally:
        mongo_client.close()

    return {
        "ok": all(item.get("ok") for item in results),
        "attempted": len(results),
        "completed": sum(1 for item in results if item.get("ok")),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", default="2019,2020,2021")
    parser.add_argument("--model", default="gpt-5")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--keys",
        default="",
        help="Comma-separated statement keys (e.g. note_14,note_15). Default: all FS notes.",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="Limit to the original three-note UI pilot.",
    )
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Also transcribe captures that failed total verification.",
    )
    parser.add_argument(
        "--align-only",
        action="store_true",
        help="Repair leftover OCR labels and rewrite matching rows across years; do not call OpenAI.",
    )
    args = parser.parse_args()
    years = [int(value.strip()) for value in args.years.split(",") if value.strip()]
    if args.align_only:
        db, client = get_db()
        try:
            aligned = repair_and_align_ui_tables(db, years)
            print(aligned, flush=True)
        finally:
            client.close()
        return 0
    keys = [value.strip() for value in args.keys.split(",") if value.strip()]
    result = run(
        years=years,
        model=args.model,
        force=args.force,
        keys=keys or None,
        skip_failed=not args.include_failed,
        pilot=args.pilot,
    )
    for item in result["results"]:
        print(item, flush=True)
    print(
        f"Completed {result['completed']}/{result['attempted']} UI note tables",
        flush=True,
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
