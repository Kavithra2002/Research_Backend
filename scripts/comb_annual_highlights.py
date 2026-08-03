"""
Financial Highlights / Goals tables in annual reports.

Values such as Dividend per share (DPS) often appear only in early-report
highlight pages (Rs. Bn / per-share summary), not on the main P&L.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

from extraction_aliases_store import patterns_for_label
from generate_comb_model import LABEL_ALIASES, norm_label, parse_number

# Labels that are commonly published in Financial Highlights / Goals tables.
HIGHLIGHT_FS_LABELS = frozenset(
    {
        "diviend per share",
        "dividend per share",
        "earnings per share",
        "basic earnings per ordinary share rs",
        "diluted earnings per ordinary share rs",
        "net assets value per ordinary share rs",
        "net assets value per ordinary share",
        "net assets value per share rs",
        "net assets value per share",
    }
)

_HIGHLIGHT_PAGE_TERMS = (
    "financial highlights",
    "financial goals and achievements",
    "information per ordinary share",
    "dividend per share (dps)",
)


def is_highlight_fs_label(label: str) -> bool:
    nl = norm_label(label)
    if nl in HIGHLIGHT_FS_LABELS:
        return True
    return "per share" in nl or "diviend" in nl


def find_financial_highlight_pages(pdf_path: Path, *, max_pages: int = 12) -> list[int]:
    """Pages near the front of the report that carry Financial Highlights / Goals."""
    if fitz is None:
        return []
    hits: list[tuple[int, int]] = []
    with fitz.open(str(pdf_path)) as doc:
        limit = min(len(doc), 40)
        for i in range(limit):
            text = (doc[i].get_text() or "").lower()
            score = 0
            for term in _HIGHLIGHT_PAGE_TERMS:
                if term in text:
                    score += 20
            if "dividend per share" in text or "dividends - shares" in text:
                score += 15
            if "gross income" in text and "total assets" in text and "rs. bn" in text:
                score += 10
            if score:
                hits.append((score, i + 1))
    hits.sort(reverse=True)
    return [p for _, p in hits[:max_pages]]


def _needles_for_label(label: str) -> list[str]:
    nl = norm_label(label)
    needles = patterns_for_label(label, LABEL_ALIASES, "fs", default_to_label=True)
    if "diviend" in nl or "dividend per share" in nl:
        needles = [
            "Dividend per share (DPS)",
            "Dividend per share (DPS) (Rs.)",
            "Dividend per share",
            "Dividends - Shares",
            "Dividends – Shares",
            *needles,
        ]
    seen: set[str] = set()
    out: list[str] = []
    for n in needles:
        key = (n or "").strip().lower()
        if len(key) < 6 or key in seen:
            continue
        seen.add(key)
        out.append(n.strip())
    return out


def _row_amounts_right_of_label(page: Any, label_rect: Any) -> list[float]:
    words = page.get_text("words") or []
    y0 = float(label_rect.y0) - 3.0
    y1 = float(label_rect.y0) + 20.0
    nums: list[float] = []
    skip_goal_amount = False
    for w in sorted(words, key=lambda t: (t[1], t[0])):
        y_mid = (w[1] + w[3]) / 2.0
        if y_mid < y0 or y_mid > y1:
            continue
        if w[0] < float(label_rect.x1) - 4:
            continue
        token = (w[4] or "").strip()
        low = token.lower().rstrip(".")
        # Goals column text: "Over Rs. 5.00" — skip that threshold amount.
        if low in {"over", "above", "minimum"}:
            skip_goal_amount = True
            continue
        if low in {"rs", "rs.", "lkr"}:
            continue
        if not re.search(r"\d", token):
            continue
        v = parse_number(token)
        if v is None:
            continue
        # Skip calendar years in goals headers.
        if abs(v - round(v)) < 1e-9 and 2000 <= abs(v) <= 2035:
            continue
        if skip_goal_amount:
            skip_goal_amount = False
            continue
        if 0 < abs(v) < 500:
            nums.append(float(v))
    return nums


def lookup_highlight_fs_value(
    pdf_path: Path,
    label: str,
    year: int,
    *,
    entity_column: str = "group",
    page_hint: list[int] | None = None,
) -> float | None:
    """
    Read a per-share / highlight metric from Financial Highlights pages.

    Prefer Goals / Information-per-share rows (e.g. DPS 2022 = 4.50).
    """
    del year  # year filtering uses page context / left-to-right achievement cols
    if fitz is None or not is_highlight_fs_label(label):
        return None

    pages = page_hint or find_financial_highlight_pages(pdf_path)
    if not pages:
        return None

    is_dps = "diviend" in norm_label(label) or "dividend per share" in norm_label(label)
    needles = _needles_for_label(label)

    with fitz.open(str(pdf_path)) as doc:
        for pnum in pages:
            if pnum < 1 or pnum > len(doc):
                continue
            page = doc[pnum - 1]
            for needle in needles:
                variants = [needle, re.sub(r"[\/\-_]+", " ", needle)]
                for soft in variants:
                    soft = re.sub(r"\s+", " ", soft).strip()
                    try:
                        hits = page.search_for(soft)
                    except Exception:
                        hits = []
                    for rect in hits:
                        amounts = _row_amounts_right_of_label(page, rect)
                        if not amounts:
                            continue
                        if is_dps:
                            preferred = [
                                a
                                for a in amounts
                                if abs(a) <= 50
                                and (
                                    abs(a * 2 - round(a * 2)) < 1e-6
                                    or abs(a - round(a)) < 1e-6
                                )
                            ]
                            if preferred:
                                return preferred[0]
                        if entity_column.lower() == "bank" and len(amounts) >= 2:
                            return amounts[-1]
                        return amounts[0]
    return None
