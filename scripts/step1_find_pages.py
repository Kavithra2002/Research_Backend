"""
step1_find_pages.py  (v5)
=========================
STEP 1 of 3 - Find & record the exact page numbers for every financial
statement in an annual-report PDF.

What's new in v5
----------------
1. SAFER TOC PARSING (no more wrong pages from two-column TOCs)
   Instead of greedy "NUMBER TITLE" matching that can pull the wrong
   number when two TOC entries are on the same extracted line, we now
   look for KNOWN statement titles inside each TOC line and read the
   page number that immediately follows the title text.  This works
   for both layouts:
       "STATEMENT OF FINANCIAL POSITION 170"
       "FINANCIAL HIGHLIGHTS 8 INCOME STATEMENT 168"      (2-col merged)
       "Statement of Profit or Loss .......... 208"

2. STRICT HEADING DETECTION (no false positives from body sentences)
   A page is treated as a statement page only if its heading text
   appears as a STANDALONE heading line (short, near the top, mostly
   uppercase / title-case) - not as part of a long paragraph such as
   "...prudent balance sheet management..." in a Risk Management page.

3. BLOCKING HEADINGS STOP CONTINUATION
   Continuation now stops on any of the following section headings:
     - Material / Significant Accounting Policy
     - Report of the Board of Directors
     - Risk Management
     - Corporate Governance
     - Chairman / CEO / Chief Executive
     - Independent Auditor
     - Glossary, GRI, Annexure, Notice of Meeting, Form of Proxy
     - Any heading that belongs to a different known statement

4. TIGHTER PAGE CAPS
   Income/OCI/SoFP/Equity/CashFlows are now capped at 3-4 pages each
   (these tables almost never run longer in practice).

JSON schema is unchanged - step 2 keeps working without modification.

Usage
-----
    python step1_find_pages.py report.pdf --company HemasPLC
    python step1_find_pages.py report.pdf --company HemasPLC --out manifest.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber


# ─────────────────────────────────────────────────────────────────────────────
# Statement definitions
#
#   key, human title, title_regex
#
# `title_regex` is also used to identify the title text inside TOC lines.
# It MUST match the *complete* statement title (no shortcut words like just
# "balance sheet" — those go in OWN_HEADING for prominent-heading checks).
# ─────────────────────────────────────────────────────────────────────────────

STMT_DEFS: list[tuple[str, str, re.Pattern]] = [
    (
        "income_statement",
        "Income Statement / Statement of Profit or Loss",
        re.compile(
            r"(?:income\s+statement|statement\s+of\s+profit(?:\s+or\s+loss)?)",
            re.I,
        ),
    ),
    (
        "oci",
        "Statement of Other Comprehensive Income (OCI)",
        re.compile(
            r"statement\s+of\s+(?:other\s+)?comprehensive\s+income",
            re.I,
        ),
    ),
    (
        "sofp",
        "Statement of Financial Position (Balance Sheet)",
        re.compile(
            r"(?:statement\s+of\s+financial\s+position|balance\s+sheet)",
            re.I,
        ),
    ),
    (
        "equity",
        "Statement of Changes in Equity",
        re.compile(r"statement\s+of\s+changes\s+in\s+equity", re.I),
    ),
    (
        "cash_flows",
        "Statement of Cash Flows",
        re.compile(
            r"(?:statement\s+of\s+cash\s+flows?|cash\s+flow\s+statement)",
            re.I,
        ),
    ),
    (
        "shareholder_info",
        "Shareholder / Share Information",
        re.compile(r"share(?:holder|s)?\s+information", re.I),
    ),
    (
        "investor_info",
        "Investor Information",
        re.compile(r"investor\s+(?:information|relations)", re.I),
    ),
    (
        "ten_year_summary",
        "Ten Year Summary / Decade at a Glance",
        re.compile(
            r"(?:ten[\s\-]year|10[\s\-]year)\s+"
            r"(?:summary|achievements?|highlights?|financial)|"
            r"decade\s+at\s+a\s+glance",
            re.I,
        ),
    ),
    (
        "five_year_summary",
        "Five Year Summary / Achievements",
        re.compile(
            r"(?:five[\s\-]year|5[\s\-]year)\s+"
            r"(?:summary|achievements?|highlights?|financial)",
            re.I,
        ),
    ),
    (
        "notes",
        "Notes to the Financial Statements",
        re.compile(
            r"notes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements?",
            re.I,
        ),
    ),
]

_KEY_TITLE:   dict[str, str]    = {k: t for k, t, _ in STMT_DEFS}
_OWN_HEADING: dict[str, re.Pattern] = {k: r for k, _, r in STMT_DEFS}


# Headings that, when found at the TOP of a continuation page, signal the
# end of the current statement and the start of something else entirely.
_BLOCKING_HEADINGS_RE = re.compile(
    r"\bshare\s+performance\b|"
    r"\boperating\s+environment\b|"
    r"\bthe\s+group[\u2019']?s?\s+strategy\b|"
    r"\bgroup[\u2019']?s?\s+strategy\b|"
    r"\bmaterial\s+accounting\s+polic|"
    r"\bsignificant\s+accounting\s+polic|"
    r"\baccounting\s+policies\b|"
    r"\breport\s+of\s+the\s+board\s+of\s+directors\b|"
    r"\bdirector(?:s)?'?s?\s+report\b|"
    r"\brisk\s+management\b|"
    r"\bcorporate\s+governance\b|"
    r"\bchairman[\u2019']?s?\s+message\b|"
    r"\bchief\s+executive\s+officer|"
    r"\bceo[\u2019']?s?\s+review\b|"
    r"\bindependent\s+auditor|"
    r"\bglossary\b|"
    r"\bgri\s+content\s+index\b|"
    r"\bsasb\s+disclosures\b|"
    r"\bnotice\s+of\s+(?:annual\s+)?(?:general\s+)?meeting\b|"
    r"\bform\s+of\s+proxy\b|"
    r"\bannexure\b|"
    r"\bcorporate\s+information\b|"
    r"\bcorporate\s+social\b|"
    r"\bvalue\s+creation\s+model\b|"
    r"\bstakeholder\s+engagement\b|"
    r"\bmateriality\b|"
    r"\bbranch\s+network\b|"
    r"\baudit\s+committee\b|"
    r"\bremuneration\s+committee\b|"
    r"\bnomination\s+(?:and|&)\s+governance\b|"
    r"\brelated\s+party\s+transactions\s+review\b|"
    r"\bdirector(?:s)?[\u2019']?\s+responsibilit|"
    r"\bstatement\s+of\s+value\s+added\b|"
    r"\bsegment(?:al)?\s+(?:information|review)\b|"
    r"\bfinancial\s+calendar\b|"
    r"\bquarterly\s+statistics\b|"
    r"\bus\s+dollar\s+financial\s+statements\b",
    re.I,
)

# Different KNOWN statement heading — also stops a different statement's
# continuation.
_ANY_STMT_HEADING_RE = re.compile(
    r"\bincome\s+statement\b|"
    r"\bstatement\s+of\s+(?:other\s+)?comprehensive\s+income\b|"
    r"\bstatement\s+of\s+financial\s+position\b|"
    r"\bstatement\s+of\s+changes\s+in\s+equity\b|"
    r"\bstatement\s+of\s+cash\s+flows?\b|"
    r"\bbalance\s+sheet\b|"
    r"\bshare(?:holder)?s?\s+information\b|"
    r"\binvestor\s+(?:information|relations)\b|"
    r"\b(?:ten|five|10|5)[\s\-]year\s+(?:summary|achievements?|highlights?)\b|"
    r"\bdecade\s+at\s+a\s+glance\b|"
    r"\bnotes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements?\b",
    re.I,
)

_NOTES_STOP_HEADINGS_RE = re.compile(
    r"\bshareholder\s+information\b|"
    r"\bshares?\s+information\b|"
    r"\binvestor\s+(?:information|relations)\b|"
    r"\b(?:five|ten|5|10)[\s\-]year\s+(?:summary|achievements|highlights|financial)\b|"
    r"\bdecade\s+at\s+a\s+glance\b|"
    r"\bquarterly\s+statistics\b|"
    r"\bglossary\b|"
    r"\bnotice\s+of\s+meeting\b|"
    r"\bform\s+of\s+proxy\b|"
    r"\bgri\s+content\s+index\b|"
    r"\bsasb\s+disclosures\b|"
    r"\binvestor\s+feedback\s+form\b|"
    r"\bgroup\s+companies\s+and\s+directorate\b|"
    r"\breal\s+estate\s+holdings\b|"
    r"\bindependent\s+assurance\s+report\b|"
    r"\bcorporate\s+information\b|"
    r"\bannexure\b|"
    r"\bbranch\s+network\b",
    re.I,
)

_PGREF_RE = re.compile(r"\bpage\s+\d{1,4}\b", re.I)


# ─────────────────────────────────────────────────────────────────────────────
# Running-header / footer stripping
#
# Many annual reports (e.g. Dialog Axiata 2024) repeat a navigation
# breadcrumb on EVERY page, such as:
#     "Overview Leadership ... Governance and Financial Statements ..."
#     "Discussion and Analysis Risk Management Information"
# Those lines contain section words ("Risk Management", "Governance") that
# falsely trip the blocking-heading filters and hide the real statement
# title.  We detect lines that repeat across a large fraction of pages and
# strip them before any heading analysis.
# ─────────────────────────────────────────────────────────────────────────────

# Populated per-PDF at the start of find_all_statement_pages(); reset each
# run so it never leaks between reports processed in the same process.
_RUNNING_HEADERS: set[str] = set()


def _norm_header_line(line: str) -> str:
    s = re.sub(r"\d+", "", line or "")
    s = re.sub(r"[\u2022\u25c6\u25cf\uf096|·•]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def compute_running_headers(pdf, threshold: float = 0.5,
                            min_pages: int = 5, edge_lines: int = 4) -> set[str]:
    """Return the set of normalised lines that recur on >= `threshold`
    fraction of pages (and on at least `min_pages` pages).

    Only the first `edge_lines` and last 3 lines of each page are
    considered — that's where page chrome lives.  Lines that are clean
    statement titles are never treated as chrome, so a repeated real
    heading is preserved.
    """
    from collections import Counter

    n = len(pdf.pages)
    if n < max(4, min_pages):
        return set()

    counter: Counter = Counter()
    for page in pdf.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
        if not lines:
            continue
        edge = lines[:edge_lines] + lines[-3:]
        seen_on_page: set[str] = set()
        for ln in edge:
            if len(ln) < 4:
                continue
            norm = _norm_header_line(ln)
            if norm and norm not in seen_on_page:
                seen_on_page.add(norm)
                counter[norm] += 1

    cutoff = max(min_pages, int(n * threshold))
    headers: set[str] = set()
    for norm, count in counter.items():
        if count < cutoff:
            continue
        # Never strip a line that is itself a clean statement title.
        if _is_clean_statement_title(norm):
            continue
        headers.add(norm)
    return headers


def _is_clean_statement_title(norm_line: str) -> bool:
    for _key, _title, title_re in STMT_DEFS:
        m = title_re.match(norm_line)
        if m and _heading_title_fills_line(norm_line, m):
            return True
    return False


def strip_running_headers(text: str, header_set: set[str]) -> str:
    if not text or not header_set:
        return text or ""
    kept = [ln for ln in text.split("\n")
            if _norm_header_line(ln) not in header_set]
    return "\n".join(kept)


# ─────────────────────────────────────────────────────────────────────────────
# Section navigation breadcrumb stripping
#
# Some reports (e.g. Ceylon Tobacco 2023-2025) print a multi-section
# navigation breadcrumb at the very top of alternating pages, such as:
#     "Overview Executive Crafting Our Our Business Sustainability Risk
#      Management Financial Supplementary"
#     "Review Strategy Imperatives in Focus & Governance Statements Information"
# These lines contain section words ("Risk Management", "Governance") that
# falsely trip the blocking-heading filters and hide the real statement
# title underneath.  Because the breadcrumb only appears on the financial
# section pages (well under the 50% running-header threshold), it is NOT
# caught by compute_running_headers — so we detect and strip it structurally:
# a single line that crams together several distinct section keywords is
# navigation chrome, never a real statement heading.
# ─────────────────────────────────────────────────────────────────────────────

_NAV_BREADCRUMB_KEYWORDS = (
    "overview", "executive", "strategy", "sustainability",
    "risk management", "governance", "financial statement",
    "supplementary", "business review", "value creation",
    "stewardship", "management discussion", "imperatives",
    "in focus", "our business", "leadership", "performance review",
)


def _is_nav_breadcrumb(line: str) -> bool:
    s = (line or "").strip().lower()
    # Real statement headings are short; breadcrumbs are long ribbons that
    # span the page width.
    if len(s) < 45:
        return False
    if _is_clean_statement_title(_norm_header_line(s)):
        return False
    hits = sum(1 for kw in _NAV_BREADCRUMB_KEYWORDS if kw in s)
    return hits >= 3


def strip_nav_breadcrumbs(text: str) -> str:
    if not text:
        return text or ""
    kept = [ln for ln in text.split("\n") if not _is_nav_breadcrumb(ln)]
    return "\n".join(kept)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_text(page) -> str:
    try:
        txt = page.extract_text() or ""
    except Exception:
        return ""
    if _RUNNING_HEADERS:
        txt = strip_running_headers(txt, _RUNNING_HEADERS)
    txt = strip_nav_breadcrumbs(txt)
    return txt


def _top_lines(txt: str, n: int) -> list[str]:
    return [ln for ln in (txt or "").split("\n")[:n]]


# ─────────────────────────────────────────────────────────────────────────────
# Heading-line check
#
# A page is considered a "real" statement page only if the title appears as
# a STANDALONE heading line near the top of the page.  This prevents body
# sentences like "...balance sheet management..." inside a Risk Management
# page from being mis-identified as the SoFP.
# ─────────────────────────────────────────────────────────────────────────────

_LEADING_PG_NUM_RE = re.compile(r"^\s*\d{1,4}\s+")
# TOC / footer noise such as "105 | Statement of ..." or " | 144"
_LEADING_TOC_NOISE_RE = re.compile(r"^\s*(?:\d{1,4}\s*\|\s*|\|\s*\d{1,4}\s*)+")
# Leading qualifiers that precede a real statement title, e.g.
# "Consolidated Statement of Changes in Equity" or "Company Statement of ...".
_LEADING_QUALIFIER_RE = re.compile(
    r"^(?:consolidated|company|group|parent|bank|separate|interim)\s+",
    re.I,
)
_TOC_LINE_RE = re.compile(
    r"\b(?:table\s+of\s+)?contents?\b|"
    r"\boverview\b.*\bfinancial\s+statements\b",
    re.I,
)


def _top_nonempty_lines(text: str, top_n: int) -> list[str]:
    lines: list[str] = []
    seen = 0
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        seen += 1
        if seen > top_n:
            break
        lines.append(line)
    return lines


def _iter_heading_candidates(text: str, top_n: int = 14,
                             max_len: int = 75) -> list[str]:
    """
    Build heading candidates from the top of a page.

    Many annual reports (e.g. Ambeon / CTC 2020) break a single statement
    title across two or three short lines:
        STATEMENT OF
        PROFIT OR LOSS
    """
    lines = _top_nonempty_lines(text, top_n)
    out: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        cand = candidate.strip()
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)

    for i, line in enumerate(lines):
        if len(line) <= max_len:
            _add(line)
        for span in (2, 3):
            chunk = lines[i:i + span]
            if len(chunk) < span:
                break
            if any(len(ln) > 48 for ln in chunk):
                break
            joined = " ".join(chunk)
            if len(joined) <= max_len + 45:
                _add(joined)
    return out


def _normalize_heading_body(line: str) -> str:
    body = _LEADING_PG_NUM_RE.sub("", line, count=1)
    body = _LEADING_TOC_NOISE_RE.sub("", body, count=1)
    body = body.strip()
    # Strip up to two leading qualifiers ("Consolidated", "Company", ...).
    for _ in range(2):
        new = _LEADING_QUALIFIER_RE.sub("", body, count=1)
        if new == body:
            break
        body = new.strip()
    return body


def _heading_title_fills_line(body: str, match: re.Match) -> bool:
    match_len = match.end() - match.start()
    if match_len >= len(body) - 4:
        return True
    return match_len / max(len(body), 1) >= 0.50


def has_prominent_heading(text: str, title_re: re.Pattern,
                          top_n: int = 14, max_len: int = 75) -> bool:
    """
    Return True only if `title_re` matches a *standalone heading line* near
    the top of `text`.

    A heading line satisfies ALL of:
      - is one of the first `top_n` non-empty lines (or a short join of
        2-3 consecutive lines when PDF layout splits the title),
      - is no longer than `max_len` characters (joined headings may be a
        little longer),
      - the title pattern matches RIGHT AT THE START of the line (after at
        most a leading page-number / TOC prefix is stripped),
      - the matched title is at least 50% of the remaining line length, OR
        the line is fully consumed by the title.

    This rejects body sentences such as
        "...prudent balance sheet management..."
    and 2-column TOC lines such as
        "21 Board of Directors 33 Investor Relations".
    """
    if not text:
        return False

    for line in _iter_heading_candidates(text, top_n, max_len):
        if len(line) > max_len + 45:
            continue

        body = _normalize_heading_body(line)
        if not body:
            continue

        m = title_re.match(body)
        if not m:
            continue

        if _heading_title_fills_line(body, m):
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# TOC extraction
#
# We don't try to grok every "number/title" token on a TOC line.  Instead we
# search the line for each KNOWN statement title; the first number that
# follows the title becomes its printed page.  This handles both column
# orientations and rejects garbage matches.
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_toc(txt: str) -> bool:
    if not txt:
        return False
    first = txt[:400].lower()
    if re.search(r"\b(?:table\s+of\s+)?contents?\b", first):
        return True

    short_entries = 0
    for line in txt.split("\n"):
        line = line.strip()
        if not line or len(line) > 220:
            continue
        if re.search(r"\d{1,4}\s*$", line) and re.search(r"[A-Za-z]{4,}", line):
            short_entries += 1
        elif re.match(r"^\d{1,4}\s+[A-Za-z]", line):
            short_entries += 1
    return short_entries >= 5


def _toc_page_for_match(line: str, m: re.Match) -> int | None:
    """Read a printed page number before or after a TOC title match."""
    tail = line[m.end():]
    pm = re.search(r"[\s.|]+(\d{1,4})\b", tail)
    if pm:
        between = tail[:pm.start()]
        if not re.search(r"[A-Z][A-Z]{3,}", between):
            pg = int(pm.group(1))
            if 1 <= pg <= 5000:
                return pg

    prefix = line[:m.start()]
    pm = re.search(r"(?:^|[\s|])(\d{1,4})\s*$", prefix)
    if pm:
        pg = int(pm.group(1))
        if 1 <= pg <= 5000:
            return pg
    return None


def _extract_toc_pairs(txt: str) -> list[tuple[str, int, str]]:
    """
    From a TOC line, return all (key, printed_page, matched_title) entries
    found by anchoring at known statement titles.
    """
    out: list[tuple[str, int, str]] = []
    if not txt:
        return out

    raw_lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
    lines: list[str] = []
    for i, line in enumerate(raw_lines):
        lines.append(line)
        if len(line) < 80 and i + 1 < len(raw_lines):
            nxt = raw_lines[i + 1]
            if len(nxt) < 60 and not re.match(r"^\d{1,4}\b", nxt):
                lines.append(f"{line} {nxt}")

    for line in lines:
        if len(line) < 6 or len(line) > 220:
            continue

        for key, _, title_re in STMT_DEFS:
            for m in title_re.finditer(line):
                pg = _toc_page_for_match(line, m)
                if pg is None:
                    continue
                out.append((key, pg, line[m.start():m.end()]))
    return out


def scan_toc(pdf, scan_limit: int = 60) -> dict[str, list[int]]:
    toc: dict[str, list[int]] = {k: [] for k, _, _ in STMT_DEFS}

    toc_pages: list[int] = []
    for i in range(min(len(pdf.pages), scan_limit)):
        txt = _safe_text(pdf.pages[i])
        if _looks_like_toc(txt):
            toc_pages.append(i)

    if not toc_pages:
        toc_pages = list(range(0, min(len(pdf.pages), 12)))

    for i in toc_pages:
        txt = _safe_text(pdf.pages[i])
        for key, pg, _ in _extract_toc_pairs(txt):
            toc[key].append(pg)

    # Dedupe while preserving order.
    for k in toc:
        seen, out = set(), []
        for p in toc[k]:
            if p not in seen:
                seen.add(p)
                out.append(p)
        toc[k] = out

    return toc


# ─────────────────────────────────────────────────────────────────────────────
# Page-offset discovery
# ─────────────────────────────────────────────────────────────────────────────

def discover_offset(pdf, toc: dict[str, list[int]]) -> int:
    candidates: dict[int, int] = {}
    n = len(pdf.pages)

    for key, printed_list in toc.items():
        heading_re = _OWN_HEADING[key]
        for printed in printed_list:
            base = printed - 1
            for off in range(-3, 18):
                pdf_idx = base + off
                if not (0 <= pdf_idx < n):
                    continue
                txt = _safe_text(pdf.pages[pdf_idx])
                if has_prominent_heading(txt, heading_re):
                    candidates[off] = candidates.get(off, 0) + 1
                    break

    if not candidates:
        return 0
    best_off, _ = max(candidates.items(), key=lambda kv: (kv[1], -abs(kv[0])))
    return best_off


# ─────────────────────────────────────────────────────────────────────────────
# Page-by-page validation
# ─────────────────────────────────────────────────────────────────────────────

_REJECT_BODY_RE = re.compile(
    r"independent\s+auditor.?s?\s+report|"
    r"basis\s+for\s+(?:our\s+)?opinion|"
    r"key\s+audit\s+matter|"
    r"chartered\s+accountants|"
    r"in\s+our\s+opinion",
    re.I,
)


def is_statement_first_page(text: str, key: str) -> bool:
    """
    Strict check: the page must (a) have the statement heading prominently
    near the top, and (b) not be the auditor's report / opinion etc.
    """
    if not text:
        return False
    if _REJECT_BODY_RE.search(text[:600]):
        return False

    # Reject table-of-contents pages that mention statement titles but are
    # not the actual statement.
    head = (text or "")[:500].lower()
    if _TOC_LINE_RE.search(head):
        return False

    # If the top of the page screams a different known section, reject.
    top_block = "\n".join(_top_lines(text, 6))
    if _BLOCKING_HEADINGS_RE.search(top_block):
        return False

    own_re = _OWN_HEADING[key]
    if not has_prominent_heading(text, own_re):
        return False

    # Same statement heading found -- but is there an OBVIOUS other-statement
    # heading higher up on the page?
    other_match = _ANY_STMT_HEADING_RE.search(top_block)
    if other_match and not own_re.search(top_block):
        return False

    return True


def _continuation_ok(page_txt: str, key: str) -> bool:
    if not page_txt.strip():
        return False

    first6 = "\n".join(_top_lines(page_txt, 6))
    own_re = _OWN_HEADING[key]

    # Notes treats _NOTES_STOP_HEADINGS_RE specially; everything else uses
    # the strict blocking set.
    if key == "notes":
        return not bool(_NOTES_STOP_HEADINGS_RE.search(first6))

    if _BLOCKING_HEADINGS_RE.search(first6):
        return False

    if _ANY_STMT_HEADING_RE.search(first6):
        return bool(own_re.search(first6))

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main detection
# ─────────────────────────────────────────────────────────────────────────────

_MAX_CONT: dict[str, int] = {
    "income_statement":  3,
    "oci":               3,
    "sofp":              3,
    "equity":            4,
    "cash_flows":        4,
    "shareholder_info":  8,
    "investor_info":     8,
    "ten_year_summary":  8,
    "five_year_summary": 8,
    "notes":             200,
}


def find_all_statement_pages(pdf) -> tuple[dict[str, dict], int]:
    global _RUNNING_HEADERS

    # Detect repeating page chrome (nav breadcrumbs, footers) so it cannot
    # hide real headings or trip the blocking-heading filters.  Recomputed
    # per-PDF so it never leaks between reports.
    _RUNNING_HEADERS = compute_running_headers(pdf)

    n   = len(pdf.pages)
    toc = scan_toc(pdf)
    off = discover_offset(pdf, toc)

    result: dict[str, dict] = {}

    # ── Pass 1: TOC-anchored detection ──────────────────────────────────────
    for key, title, _ in STMT_DEFS:
        printed_list = sorted(set(toc.get(key, [])))
        if not printed_list:
            continue

        best_idx: int | None = None
        for printed in printed_list:
            idx0 = printed - 1 + off
            for delta in range(-4, 8):
                cand = idx0 + delta
                if not (0 <= cand < n):
                    continue
                txt = _safe_text(pdf.pages[cand])
                if is_statement_first_page(txt, key):
                    best_idx = cand
                    break
            if best_idx is not None:
                break

        if best_idx is not None:
            result[key] = {
                "title":              title,
                "printed_pages":      [best_idx + 1 - off],
                "pdf_indices_0based": [best_idx],
                "pdf_pages_1based":   [best_idx + 1],
                "page_count":         1,
                "confirmed":          True,
            }

    # ── Pass 2: heading-scan fallback for the still-missing statements ──────
    missing = [k for k, *_ in STMT_DEFS if k not in result]
    if missing:
        for i in range(n):
            if not missing:
                break
            txt = _safe_text(pdf.pages[i])
            if not txt:
                continue
            for key in list(missing):
                if not is_statement_first_page(txt, key):
                    continue
                title = _KEY_TITLE[key]
                result[key] = {
                    "title":              title,
                    "printed_pages":      [i + 1 - off],
                    "pdf_indices_0based": [i],
                    "pdf_pages_1based":   [i + 1],
                    "page_count":         1,
                    "confirmed":          False,
                }
                missing.remove(key)
                break

    # ── Pass 3: extend each statement to its real continuation pages ─────────
    for key, info in result.items():
        start_idx = info["pdf_indices_0based"][0]
        max_cont  = _MAX_CONT.get(key, 4)
        indices   = [start_idx]

        for nxt in range(start_idx + 1, min(n, start_idx + 1 + max_cont)):
            nxt_txt = _safe_text(pdf.pages[nxt])
            if not _continuation_ok(nxt_txt, key):
                break
            indices.append(nxt)

        info["pdf_indices_0based"] = indices
        info["pdf_pages_1based"]   = [i + 1 for i in indices]
        info["page_count"]         = len(indices)

    return result, off


# ─────────────────────────────────────────────────────────────────────────────
# CLI runner
# ─────────────────────────────────────────────────────────────────────────────

def run(pdf_path: str | Path, company: str,
        out_path: str | Path | None = None,
        verbose: bool = True) -> dict:

    pdf_path = Path(pdf_path).resolve()

    if verbose:
        print(f"\n[{company}]  {pdf_path.name} ...", end="", flush=True)

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            n_pages          = len(pdf.pages)
            page_map, offset = find_all_statement_pages(pdf)
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}

    all_keys = [k for k, *_ in STMT_DEFS]
    found    = [k for k in all_keys if k in page_map]
    missing  = [k for k in all_keys if k not in page_map]

    if verbose:
        print(f"  {len(found)} found / {len(missing)} not found  "
              f"({n_pages} pages, offset {offset})")
        for k in found:
            info = page_map[k]
            pgs  = info["pdf_pages_1based"]
            rng  = (f"pg {pgs[0]}" if len(pgs) == 1
                    else f"pg {pgs[0]}-{pgs[-1]} ({info['page_count']}pp)")
            conf = "TOC" if info["confirmed"] else "scan"
            print(f"    OK  [{conf}]  {info['title']}  ->  {rng}")
        for k in missing:
            title = _KEY_TITLE[k]
            print(f"    --  {title}")

    manifest = {
        "company":         company,
        "source_pdf":      str(pdf_path),
        "total_pdf_pages": n_pages,
        "page_offset":     offset,
        "generated_at":    datetime.now().isoformat(timespec="seconds"),
        "statements":      page_map,
    }

    if out_path is None:
        out_path = pdf_path.parent / f"{pdf_path.stem}_pages.json"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if verbose:
        print(f"    -> {out_path}")

    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="STEP 1 - Detect financial statement page ranges in an annual-report PDF."
    )
    ap.add_argument("pdf",       type=Path,
                    help="Path to the annual report PDF")
    ap.add_argument("--company", "-c", default="company",
                    help="Company name / key  (default: 'company')")
    ap.add_argument("--out",     "-o", type=Path, default=None,
                    help="Output JSON path  (default: <pdf_stem>_pages.json)")
    ap.add_argument("--quiet",   action="store_true",
                    help="Suppress all terminal output")
    args = ap.parse_args()

    run(args.pdf, args.company, args.out, verbose=not args.quiet)
