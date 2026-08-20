"""
Q_data_extraction.py
====================
Extract financial statement tables from CSE quarterly-report PDFs and
write structured JSON output.

Primary extraction uses OpenAI GPT-4o Vision (same pipeline as
Data_retrive.py / step3_send_to_openai.py): each detected statement page
is rasterised to PNG, sent to the API with a verbatim transcription
prompt, and saved in the step3 JSON shape.

A local rule-based fallback (pdfplumber / camelot) is still run in
parallel and stored under ``local_statements`` for comparison.

Technologies
------------
  OpenAI GPT-4o  – verbatim table transcription from page images (primary)
  pdfplumber     – page detection + local fallback extraction
  PyMuPDF        – page rasterisation (PNG captures for API + viewer)
  camelot-py     – stream-mode table detection (local fallback, optional)
  pytesseract    – OCR fallback for garbled text layers

What is extracted
-----------------
Every financial table that appears BEFORE the first
"Notes to the Financial Statements" page.  The script covers:

  • Income Statement / Statement of Profit or Loss
  • Statement of Other Comprehensive Income
  • Statement of Financial Position (Balance Sheet)
  • Statement of Changes in Equity  (Group and/or Company)
  • Cash Flow Statement
  • Any other table found before the Notes section

Output layout (per PDF)
-----------------------
  <output_dir>/<company_slug>/
      <company_slug>_results.json     ← structured extraction
      captures/
          <stmt_key>/
              page_NNN.png            ← rasterised page image(s)

JSON schema  (_results.json)
-----------------------------
{
  "source_pdf"        : "filename.pdf",
  "company"           : "ACL PLASTICS PLC",
  "period"            : "31st March 2016",
  "generated_at"      : "2025-01-01T12:00:00",
  "model"             : "gpt-5",
  "api_status"        : "ok",
  "statements"        : {          // OpenAI verbatim output (step3 shape)
    "<stmt_key>" : {
      "status": "ok",
      "title" : "Consolidated Income Statement",
      "data"  : {
        "statement_title": "...",
        "tables": [{"header_rows": [...], "rows": [{"cells": [...], "style": "data"}]}]
      }
    }
  },
  "local_statements"  : { ... }    // rule-based fallback (legacy shape)
}

Statement keys follow a consistent naming convention:
  consolidated_income_statement
  company_income_statement
  consolidated_comprehensive_income
  company_comprehensive_income
  financial_position
  changes_in_equity
  cash_flow_statement
  (+ any extra tables found, numbered: extra_table_1, extra_table_2, …)

Usage
-----
    # Batch: first Quarterly PDF per company (API key from backend/.env)
    python Q_data_extraction.py --reports ../reports --out ../extracted

    # Explicit API key + cheaper model
    python Q_data_extraction.py --reports ../reports --apikey sk-... --model gpt-5

    # Dry-run: rasterise images only, no API calls
    python Q_data_extraction.py --reports ../reports --dry-run

    # Single PDF
    python Q_data_extraction.py --pdf report.pdf --out ./extracted

    # Disable OCR fallback (faster, may miss garbled pages)
    python Q_data_extraction.py --no-ocr

    # Verbose / debug logging
    python Q_data_extraction.py --verbose
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import camelot
    _CAMELOT = True
except ImportError:
    camelot = None  # type: ignore
    _CAMELOT = False

import fitz          # PyMuPDF
import pandas as pd
import pdfplumber

import extraction_validation as val

# ── OpenAI verbatim extraction (reused from step3_send_to_openai.py) ──────
try:
    import step3_send_to_openai as _step3
    _STEP3_AVAILABLE = True
except Exception as _step3_err:
    _step3 = None  # type: ignore
    _STEP3_AVAILABLE = False
    _STEP3_IMPORT_ERROR = str(_step3_err)


# Map quarterly-report statement keys (used by classify_page) to the
# step3 prompt keys, so OpenAI gets the right verbatim instructions.
QSTMT_TO_STEP3 = {
    "consolidated_income_statement"   : "income_statement",
    "company_income_statement"        : "income_statement",
    "income_statement"                : "income_statement",
    "profit_loss_comprehensive"       : "income_statement",
    "consolidated_comprehensive_income": "oci",
    "company_comprehensive_income"    : "oci",
    "comprehensive_income"            : "oci",
    "financial_position"              : "sofp",
    "changes_in_equity"               : "equity",
    "cash_flow_statement"             : "cash_flows",
}


# ─────────────────────────────────────────────────────────────────────────────
# QUARTERLY-SPECIFIC EXTRA HINTS
# ─────────────────────────────────────────────────────────────────────────────
# Sri-Lankan CSE quarterly statements ALWAYS print "Change %" / "Change"
# sub-columns next to each pair of reporting-period columns:
#
#     |              Quarter Ended           |        Twelve Months Ended      |
#     | Note | 31.03.2016 | 31.03.2015 | Ch% | 31.03.2016 | 31.03.2015 | Ch% |
#
# The default step3 prompts are written for ANNUAL reports (no Change %)
# and were causing GPT-4o to silently drop those columns.  These overrides
# make the column-count rule extremely explicit so every percentage cell
# (and the % sign itself) is preserved verbatim.
# ─────────────────────────────────────────────────────────────────────────────

_QUARTERLY_EXTRA = """

QUARTERLY-REPORT-SPECIFIC RULES (read carefully):

Q1. This is a SRI-LANKAN CSE QUARTERLY report. It is NOT an annual report.
    The table almost ALWAYS prints two GROUPS of columns side-by-side:
        Group A: "Quarter Ended"          (or "3 Months Ended", "Period Ended")
        Group B: "Twelve Months Ended"    (or "9 Months Ended", "YTD")
    Each group typically has THREE sub-columns:
        - current period (e.g. 31.03.2016)
        - comparative   (e.g. 31.03.2015)
        - "Change", "Change %", "%", "Variance" or similar
    You MUST capture EVERY ONE of these sub-columns. Do NOT drop the
    Change / Change % / % / Variance column.  It is just as important as
    the period columns.

Q2. Percentage values: keep the printed "%" sign in the cell EXACTLY as
    printed (e.g. "22.9%", "(2.9%)", "115.6%", "(36.5%)"). NEVER strip
    the % sign. NEVER convert it to a decimal. If a percentage is
    negative, keep the brackets ("(2.9%)") or minus sign exactly as the
    PDF shows.

Q3. EVERY data row MUST contain a value (or "" / "-") in the Change %
    column too.  If a row has 4 numbers + 2 percentages printed across
    the page (e.g. "Revenue 327,557 267,912 22.9% 1,275,279 1,150,233
    10.9%"), the "cells" array MUST contain ALL 6 values in the SAME row
    (plus the label, plus the Note slot if any). DO NOT split the
    percentages onto a separate row. DO NOT drop them.

Q4. Header rows: in the GROUPED header row, place "Quarter Ended" in the
    leftmost of its 3 spanned positions and "" in the next two; same for
    "Twelve Months Ended". Below it, the printed "31.03.2016 / 31.03.2015 /
    Change %" line is its own header row with 3 entries per group.

Q5. EXAMPLE for an Income Statement row that has 7 cells total
    (label + 0 Notes + 3 quarter sub-cols + 3 YTD sub-cols):

        header_rows[0] = ["", "Quarter Ended", "", "",
                              "Twelve Months Ended", "", ""]
        header_rows[1] = ["", "31.03.2016", "31.03.2015", "Change %",
                              "31.03.2016", "31.03.2015", "Change %"]
        header_rows[2] = ["", "Rs.'000", "Rs.'000", "%",
                              "Rs.'000", "Rs.'000", "%"]
        row Revenue:
          {"cells": ["Revenue",
                     "327,557", "267,912", "22.9%",
                     "1,275,279", "1,150,233", "10.9%"],
           "style": "data"}

Q6. ENTITY + PERIOD COLUMNS (critical for Sri-Lankan banks and groups):
    - Tables often show GROUP (or consolidated) beside BANK or COMPANY.
      Capture BOTH side-by-side exactly as printed; do not merge them.
    - When multiple period blocks appear on one line (e.g. "For the six months
      ended" beside "For the quarter ended", or "nine months" beside "quarter"),
      capture EVERY block with correct header_rows — do not drop the quarter block.
    - Typical order: YTD / six-month / nine-month / full-year block first,
      then the "quarter ended" (or "three months ended") block to its right.
    - GROUP columns always appear to the LEFT of BANK / COMPANY columns.
""".strip()


def _quarterly_prompt(prompt_key: str, title: str) -> str:
    """
    Build a verbatim prompt for a quarterly statement.  We start from the
    step3 prompt for the matching statement type (so all the existing
    column-width / Note-slot / spanning-header rules are preserved) and
    APPEND the quarterly-specific instructions on top.
    """
    base = (_step3.PROMPTS.get(prompt_key) if _STEP3_AVAILABLE else None) \
           or (_step3._verbatim_prompt(title) if _STEP3_AVAILABLE else "")
    return base + "\n\n" + _QUARTERLY_EXTRA

warnings.filterwarnings("ignore", category=UserWarning)

# ── Optional OCR back-ends ────────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image as PILImage
    _TESSERACT = True
except ImportError:
    _TESSERACT = False

try:
    from doctr.io import DocumentFile
    from doctr.models import ocr_predictor as _doctr_predictor
    _DOCTR = True
except ImportError:
    _DOCTR = False

try:
    from paddleocr import PaddleOCR as _PaddleOCR
    _PADDLE = True
except ImportError:
    _PADDLE = False

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("q_extraction")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 – KEYWORD PATTERNS
# ═══════════════════════════════════════════════════════════════════════════

# Patterns that indicate the start of the Notes section.
# Extraction stops at the FIRST page that matches any of these.
NOTES_STOP: list[str] = [
    r"notes?\s+to\s+the\s+financial\s+statements?",
    r"notes?\s+to\s+the\s+accounts?",
    r"notes?\s+to\s+interim",
    r"notes?\s+to\s+financial",
    r"^\s*1[\.\)]\s+basis\s+of\s+preparation",
    r"^\s*1[\.\)]\s+corporate\s+information",
    r"^\s*basis\s+of\s+preparation",
]

# Statement classification: (regex, base_key, base_title)
# Order matters: more-specific patterns must come before general ones.
STMT_RULES: list[tuple[str, str, str]] = [
    (r"consolidated\s+statement\s+of\s+(?:other\s+)?comprehensive\s+income",
     "consolidated_comprehensive_income",
     "Consolidated Statement of Comprehensive Income"),

    (r"company\s+statement\s+of\s+(?:other\s+)?comprehensive\s+income",
     "company_comprehensive_income",
     "Company Statement of Comprehensive Income"),

    (r"statement\s+of\s+(?:other\s+)?comprehensive\s+income",
     "comprehensive_income",
     "Statement of Comprehensive Income"),

    (r"statement\s+of\s+profit\s+or\s+loss\s+and\s+other\s+comprehensive\s+income",
     "profit_loss_comprehensive",
     "Statement of Profit or Loss and Other Comprehensive Income"),

    (r"consolidated\s+income\s+statement",
     "consolidated_income_statement",
     "Consolidated Income Statement"),

    (r"company\s+income\s+statement",
     "company_income_statement",
     "Company Income Statement"),

    (r"income\s+statement",
     "income_statement",
     "Income Statement"),

    (r"statement\s+of\s+financial\s+position|financial\s+position|balance\s+sheet",
     "financial_position",
     "Statement of Financial Position"),

    (r"statement\s+of\s+changes\s+in\s+equity|changes\s+in\s+equity",
     "changes_in_equity",
     "Statement of Changes in Equity"),

    (r"cash\s*flow\s+statement|statement\s+of\s+cash\s+flows?|statement\s+of\s+cashflow|cashflow",
     "cash_flow_statement",
     "Cash Flow Statement"),
]

# Pages to skip unconditionally (matched against full page text)
SKIP_PATTERNS: list[str] = [
    r"corporate\s+information",
    r"twenty\s+major\s+share",
    r"supplementary\s+disclosure",
    r"directors['']?\s+share\s+holdings?",
]

# Matched only in the first 5 lines (heading area) to avoid false positives
SKIP_HEADING_PATTERNS: list[str] = [
    r"board\s+of\s+directors",
    r"public\s+holding",
    r"market\s+prices?",
]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 – PAGE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def _imatch(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))


def is_notes_page(text: str) -> bool:
    return any(_imatch(p, text) for p in NOTES_STOP)


def is_skip_page(text: str) -> bool:
    if any(_imatch(p, text) for p in SKIP_PATTERNS):
        return True
    heading = "\n".join(text.splitlines()[:5])
    return any(_imatch(p, heading) for p in SKIP_HEADING_PATTERNS)


def classify_page(text: str) -> tuple[str, str] | None:
    """
    Return (stmt_key, stmt_title) for the first matching rule, or None.
    When a page has BOTH "consolidated" and "company" headings (two sub-tables
    on one page) we return the first match only; the caller handles multi-table
    pages via camelot.
    """
    t = text.lower()
    for pattern, key, title in STMT_RULES:
        if re.search(pattern, t):
            # Refine the title from actual page text when possible
            for line in text.splitlines():
                if re.search(pattern, line, re.IGNORECASE) and len(line.strip()) > 8:
                    title = line.strip()
                    break
            return key, title
    return None


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 – METADATA EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

_PERIOD_RE = re.compile(
    r"(?:for\s+the\s+(?:period|year|quarter)\s+ended?|as\s+at|ended?)\s+"
    r"(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4}|\d{1,2}[/\-\.]\d{2}[/\-\.]\d{4})",
    re.IGNORECASE,
)
_COMPANY_STOP = re.compile(
    r"\b(plc|ltd|limited|pvt|pte|inc|corp)\b", re.IGNORECASE
)


def extract_company_and_period(pdf_path: str) -> tuple[str, str]:
    """
    Heuristic: scan the first two pages for company name and reporting period.
    Returns ("COMPANY NAME", "period string").
    """
    company = ""
    period = ""

    with pdfplumber.open(pdf_path) as pdf:
        for pg_idx in range(min(3, len(pdf.pages))):
            text = pdf.pages[pg_idx].extract_text() or ""
            lines = [l.strip() for l in text.splitlines() if l.strip()]

            # Company: look for a line that contains a legal entity suffix
            if not company:
                for line in lines:
                    if _COMPANY_STOP.search(line) and len(line) < 80:
                        # Strip parenthetical codes like (PQ 87)
                        company = re.sub(r"\s*\([^)]*\)\s*$", "", line).strip()
                        break

            # Period: look for "for the period ended …" or "as at …"
            if not period:
                m = _PERIOD_RE.search(text)
                if m:
                    period = m.group(1).strip()

            if company and period:
                break

    return company or Path(pdf_path).stem, period.replace("\n", " ").strip() or ""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 – OCR HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _page_to_pil(fitz_doc: fitz.Document, page_index: int,
                 dpi: int = 200) -> "PILImage.Image | None":
    if not _TESSERACT:
        return None
    page = fitz_doc[page_index]
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                          colorspace=fitz.csRGB, alpha=False)
    return PILImage.open(io.BytesIO(pix.tobytes("png")))


def ocr_text(fitz_doc: fitz.Document, page_index: int) -> str:
    """
    Return OCR text for a page.
    Priority: doctr → PaddleOCR → tesseract → "".
    """
    # doctr
    if _DOCTR:
        try:
            img = _page_to_pil(fitz_doc, page_index, dpi=200)
            if img:
                model = _doctr_predictor(pretrained=True)
                result = model(DocumentFile.from_images([img]))
                lines = [
                    " ".join(w.value for w in line.words)
                    for page in result.pages
                    for block in page.blocks
                    for line in block.lines
                ]
                return "\n".join(lines)
        except Exception as e:
            log.debug("doctr failed p%d: %s", page_index + 1, e)

    # PaddleOCR
    if _PADDLE:
        try:
            import numpy as np
            img = _page_to_pil(fitz_doc, page_index, dpi=200)
            if img:
                ocr = _PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
                result = ocr.ocr(np.array(img), cls=True)
                lines = [line[1][0] for line in (result[0] or [])]
                return "\n".join(lines)
        except Exception as e:
            log.debug("PaddleOCR failed p%d: %s", page_index + 1, e)

    # tesseract
    if _TESSERACT:
        try:
            img = _page_to_pil(fitz_doc, page_index, dpi=200)
            if img:
                return pytesseract.image_to_string(img, config="--psm 6")
        except Exception as e:
            log.debug("tesseract failed p%d: %s", page_index + 1, e)

    return ""


def best_page_text(plumber_page, fitz_doc: fitz.Document,
                   page_index: int, use_ocr: bool) -> str:
    """Return the richest available text for a page."""
    text = plumber_page.extract_text() or ""
    if len(text.strip()) < 40 and use_ocr:
        fallback = ocr_text(fitz_doc, page_index)
        if len(fallback.strip()) > len(text.strip()):
            log.debug("  p%d: OCR gave better text (%d vs %d chars)",
                      page_index + 1, len(fallback), len(text))
            return fallback
    return text


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 – TABLE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def _clean(v: Any) -> str:
    """Normalise a cell value to a tidy string."""
    if v is None:
        return ""
    s = str(v).strip()
    # Collapse newlines within a cell (merged header cells from camelot)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"\s{3,}", "  ", s)
    return s


def _df_raw(df: pd.DataFrame) -> list[list[str]]:
    return [[_clean(c) for c in row] for row in df.itertuples(index=False)]


def _camelot(pdf_path: str, page_num: int,
             flavor: str = "stream") -> list[pd.DataFrame]:
    if not _CAMELOT:
        return []
    try:
        tables = camelot.read_pdf(
            pdf_path, pages=str(page_num), flavor=flavor,
            edge_tol=50, row_tol=10, strip_text="\n",
        )
        return [t.df for t in tables if not t.df.empty]
    except Exception as e:
        log.debug("camelot/%s p%d: %s", flavor, page_num, e)
        return []


def _pdfplumber_tables(plumber_page) -> list[pd.DataFrame]:
    settings = dict(
        vertical_strategy="text", horizontal_strategy="text",
        snap_tolerance=5, join_tolerance=3,
        edge_tolerance=4, min_words_vertical=2,
        min_words_horizontal=1, intersection_tolerance=3,
    )
    for table_settings in (settings, None):   # custom, then pdfplumber default
        try:
            raw = plumber_page.extract_tables(table_settings=table_settings) or []
            dfs = [pd.DataFrame(t) for t in raw if t]
            if _dfs_usable(dfs):
                return dfs
        except Exception as e:
            log.debug("pdfplumber tables: %s", e)
    return []


_VAL_TOKEN_RE = re.compile(
    r"^(?:"
    r"[\(\-\u2013\u2014]?\d[\d,\.]*\)?%?"   # 329,652  (251,405)  -2.8%  10%
    r"|[\(\)\-\u2013\u2014]"                # bare  (   )   -   en/em-dash
    r")$"
)

# Stricter pattern for column-ANCHOR detection: must contain at least 2
# consecutive digits.  This excludes bare "-", "(", ")" — those sit at
# slightly different x-positions than real numbers and would otherwise
# create phantom column clusters (the "COL1 / COL3 / COL5" we used to see).
_ANCHOR_TOKEN_RE = re.compile(r"\d{2,}")


def _is_val_token(tok: str) -> bool:
    return bool(_VAL_TOKEN_RE.match(tok.strip()))


def _is_anchor_token(tok: str) -> bool:
    return bool(_ANCHOR_TOKEN_RE.search(tok.strip()))


def _extract_words_table(plumber_page) -> list[pd.DataFrame]:
    """
    Coordinate-based table extraction.

      1. Cluster page words into visual lines by y-position.
      2. Find right-aligned column anchors from clusters of value tokens
         (a "value token" is a number, a percentage, or a bare paren / dash).
      3. For each line, split into [label, val_1, val_2, …] by snapping each
         value-zone word to its nearest column anchor.

    Works directly on pdfplumber word coordinates, so labels with spaces
    ("Cost of sales") and missing cells ("Other operating income | - | 885")
    keep their alignment.
    """
    try:
        words = plumber_page.extract_words(
            x_tolerance=1.5, y_tolerance=2,
            keep_blank_chars=False, use_text_flow=False,
        )
    except Exception as e:
        log.debug("extract_words: %s", e)
        return []
    if not words:
        return []

    # 1. Cluster into visual lines
    words.sort(key=lambda w: (round(w["top"], 0), w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) < 3:
            lines[-1].append(w)
        else:
            lines.append([w])

    # 1b. Per line, merge fragmented number tokens that the PDF rendering
    #     split apart, so they don't pollute column anchors:
    #       "( 18,830)"      → "(18,830)"
    #       "1" + ",472,639" → "1,472,639"   (Hemas-style leading digit)
    #       "284,206" + ")"  → "284,206)"    (trailing close paren)
    digit_start  = re.compile(r"^\d")
    digit_end    = re.compile(r"\d\)?%?$")
    short_num    = re.compile(r"^\d{1,3}$")
    comma_num    = re.compile(r"^,\d")
    for li, ln in enumerate(lines):
        ln.sort(key=lambda w: w["x0"])
        merged: list[dict] = []
        i = 0
        while i < len(ln):
            w = ln[i]
            t = w["text"]

            if (t == "(" and i + 1 < len(ln)
                    and digit_start.match(ln[i + 1]["text"])):
                nxt = dict(ln[i + 1])
                nxt["text"] = "(" + nxt["text"]
                nxt["x0"]   = w["x0"]
                merged.append(nxt)
                i += 2
                continue

            if (t == ")" and merged
                    and digit_end.search(merged[-1]["text"])):
                merged[-1] = dict(merged[-1])
                merged[-1]["text"] = merged[-1]["text"] + ")"
                merged[-1]["x1"]   = w["x1"]
                i += 1
                continue

            # 1-3 digit prefix glued onto a comma-number  ("1" + ",472,639")
            if (short_num.match(t) and i + 1 < len(ln)
                    and comma_num.match(ln[i + 1]["text"])
                    and -1 < ln[i + 1]["x0"] - w["x1"] < 6):
                nxt = dict(ln[i + 1])
                nxt["text"] = t + nxt["text"]
                nxt["x0"]   = w["x0"]
                merged.append(nxt)
                i += 2
                continue

            # 1-3 digit prefix glued onto another digit-starting token
            # ("1" + "1.8" → "11.8", "1" + "1,472,639" → "11,472,639").
            # Sub-pixel overlap is tolerated, hence -1.5 lower bound.
            if (short_num.match(t) and i + 1 < len(ln)
                    and re.match(r"^\d", ln[i + 1]["text"])
                    and -1.5 <= ln[i + 1]["x0"] - w["x1"] <= 2):
                nxt = dict(ln[i + 1])
                nxt["text"] = t + nxt["text"]
                nxt["x0"]   = w["x0"]
                merged.append(nxt)
                i += 2
                continue

            merged.append(w)
            i += 1
        lines[li] = merged

    # 2. Column anchors from clusters of strict-numeric x1 (right-edge)
    #    positions.  We only count tokens with ≥ 2 consecutive digits so
    #    bare "-" and "(" don't create phantom anchors.
    anchor_x1: list[float] = []
    for ln in lines:
        for w in ln:
            if _is_anchor_token(w["text"]):
                anchor_x1.append(float(w["x1"]))
    if len(anchor_x1) < 4:
        return []

    anchor_x1.sort()
    clusters: list[list[float]] = []
    current: list[float] = [anchor_x1[0]]
    for x in anchor_x1[1:]:
        if x - current[-1] < 6.0:
            current.append(x)
        else:
            clusters.append(current)
            current = [x]
    clusters.append(current)

    # Require ≥ 3 hits per cluster — keeps real columns, drops page numbers,
    # date stamps, footnote refs etc.
    col_anchors = sorted(
        sum(c) / len(c) for c in clusters if len(c) >= 3
    )
    if len(col_anchors) < 2:
        # Fall back to a more permissive 2-hit requirement
        col_anchors = sorted(
            sum(c) / len(c) for c in clusters if len(c) >= 2
        )
    if len(col_anchors) < 2:
        return []

    n_cols = len(col_anchors)

    # 3. Assemble rows using phrase-based distribution.
    #
    # Phrase grouping rules:
    #   • gap ≤ 8 pt  → same phrase
    #   • two adjacent VALUE tokens → ALWAYS separate phrases (even if gap
    #     is tiny — prevents "(999,534)(1,028,002)" merging)
    #
    # Placement rules:
    #   • DATA rows (line has ≥1 value token): everything left of the first
    #     value token is the row label; each value phrase snaps to its column.
    #   • HEADER rows (no value tokens): every phrase is distributed to the
    #     column(s) whose anchor falls inside the phrase's zone, so we get
    #     "Quarter Ended / 31.03.2016 / Rs'000" instead of flat "31.03.2016".
    PHRASE_GAP = 8.0
    raw_rows: list[list[str]] = []

    for ln in lines:
        if not ln:
            continue
        ln.sort(key=lambda w: w["x0"])

        # Group into phrases (never merge two value tokens)
        phrases: list[list[dict]] = [[ln[0]]]
        for w in ln[1:]:
            gap = w["x0"] - phrases[-1][-1]["x1"]
            if (_is_val_token(phrases[-1][-1]["text"])
                    and _is_val_token(w["text"])):
                phrases.append([w])
            elif gap <= PHRASE_GAP:
                phrases[-1].append(w)
            else:
                phrases.append([w])

        # First value-token position → label / value boundary on this line
        first_val_x0: float | None = None
        for w in ln:
            if _is_val_token(w["text"]):
                first_val_x0 = float(w["x0"])
                break
        is_data_row = first_val_x0 is not None

        row = [""] * (n_cols + 1)

        for pi, phrase in enumerate(phrases):
            text = " ".join(w["text"] for w in phrase).strip()
            if not text:
                continue
            p_x0   = float(phrase[0]["x0"])
            p_x1   = float(phrase[-1]["x1"])
            center = (p_x0 + p_x1) / 2
            next_x0 = (
                float(phrases[pi + 1][0]["x0"])
                if pi + 1 < len(phrases) else float("inf")
            )

            # ── DATA row: label zone (left of first value token) ───────
            if is_data_row and p_x0 < first_val_x0 - 2:
                row[0] = (row[0] + " " + text).strip() if row[0] else text
                continue

            # ── Title / footnote lines that sit left of the table grid ─
            if p_x1 < col_anchors[0] - 5:
                row[0] = (row[0] + " " + text).strip() if row[0] else text
                continue

            # ── Long prose lines (certification text, footnotes) ───────
            if not is_data_row and len(text) > 55:
                row[0] = (row[0] + " " + text).strip() if row[0] else text
                continue

            # ── Single value token → snap to nearest column ────────────
            if len(phrase) == 1 and _is_val_token(phrase[0]["text"]):
                idx = min(
                    range(n_cols),
                    key=lambda i: abs(col_anchors[i] - p_x1),
                )
                if abs(col_anchors[idx] - p_x1) <= 20:
                    row[idx + 1] = (
                        f"{row[idx + 1]} {text}".strip()
                        if row[idx + 1] else text
                    )
                continue

            # ── Span / header phrase → zone-based column assignment ────
            assigned: list[int] = [
                i for i, a in enumerate(col_anchors)
                if (p_x0 - 6.0) <= a < next_x0
            ]
            if not assigned:
                idx = min(
                    range(n_cols),
                    key=lambda i: abs(col_anchors[i] - center),
                )
                if abs(col_anchors[idx] - center) <= 25:
                    assigned = [idx]
            for idx in assigned:
                row[idx + 1] = (
                    f"{row[idx + 1]} {text}".strip()
                    if row[idx + 1] else text
                )

        if any(c.strip() for c in row):
            raw_rows.append(row)

    if not raw_rows:
        return []

    return [pd.DataFrame(raw_rows)]


def _parse_financial_line(line: str) -> list[str] | None:
    """
    Split a single-space financial line into [label, num, num, …].
    e.g. "Revenue 329,652 269,242 22%" → ["Revenue", "329,652", "269,242", "22%"]
    """
    line = line.strip()
    if not line:
        return None
    m = re.search(r"[\(\-]?\d", line)
    if not m:
        return [line]
    label = line[: m.start()].strip()
    rest  = line[m.start() :]
    nums  = re.findall(r"\([\d,\.]+\)|\-?[\d][\d,\.]*%?", rest)
    if label and nums:
        return [label] + nums
    if nums:
        return nums
    if label:
        return [label]
    return None


def _text_table(text: str) -> list[pd.DataFrame]:
    """
    Parse page text into a table DataFrame.
    Tries double-space columns first (layout=True text), then single-space
    financial-line parsing for CSE quarterly PDFs.
    """
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cols = [c.strip() for c in re.split(r"  +", line) if c.strip()]
        if len(cols) <= 1:
            parsed = _parse_financial_line(line)
            if parsed:
                cols = parsed
        if cols:
            rows.append(cols)
    if not rows:
        return []
    w = max(len(r) for r in rows)
    padded = [r + [""] * (w - len(r)) for r in rows]
    return [pd.DataFrame(padded)]


def _dfs_usable(dfs: list[pd.DataFrame]) -> bool:
    """True when at least one DataFrame has a row with label + numeric values."""
    for df in dfs:
        raw = _df_raw(df)
        if raw and _find_first_data_row(raw) < len(raw):
            return True
    return False


def extract_page_tables(pdf_path: str, page_num: int,
                        plumber_page, fitz_doc: fitz.Document,
                        use_ocr: bool) -> list[pd.DataFrame]:
    """
    Multi-strategy table extraction (in priority order):
      1. camelot stream
      2. pdfplumber text-based
      3. camelot lattice
      4. raw text splitting
      5. OCR + text splitting
    """
    # Coordinate-based extraction is the most reliable strategy for CSE
    # quarterly PDFs: it uses each word's x/y position rather than
    # whitespace heuristics, so labels with spaces and missing cells stay
    # in their correct columns.
    dfs = _extract_words_table(plumber_page)
    if dfs and _dfs_usable(dfs):
        return dfs

    dfs = _camelot(pdf_path, page_num, "stream")
    if dfs and _dfs_usable(dfs):
        return dfs

    dfs = _pdfplumber_tables(plumber_page)
    if dfs:
        return dfs

    dfs = _camelot(pdf_path, page_num, "lattice")
    if dfs and _dfs_usable(dfs):
        return dfs

    # layout=True preserves column spacing in most CSE quarterly PDFs
    text = (
        plumber_page.extract_text(layout=True)
        or plumber_page.extract_text()
        or ""
    )
    dfs = _text_table(text)
    if dfs and _dfs_usable(dfs):
        return dfs

    if use_ocr and (_TESSERACT or _DOCTR or _PADDLE):
        otext = ocr_text(fitz_doc, page_num - 1)
        dfs = _text_table(otext)
        if dfs and _dfs_usable(dfs):
            return dfs

    return []


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 – RAW ROWS → STRUCTURED JSON
# ═══════════════════════════════════════════════════════════════════════════

_NUM_RE = re.compile(
    r"^\s*[\(\-]?\s*[\d,\.]+\s*[\)]?\s*%?\s*$"
)
_DATE_RE = re.compile(
    r"^\d{1,2}[\/\.\-]\d{2}[\/\.\-]\d{2,4}$"
)

def _is_numeric(v: str) -> bool:
    v = v.strip()
    if _DATE_RE.match(v):
        return False   # date strings like 31.03.2016 are NOT numeric data
    return bool(_NUM_RE.match(v.replace(" ", "")))


def _find_first_data_row(raw: list[list[str]]) -> int:
    """
    Return the index of the first row that looks like a genuine data row:
      - col[0] is non-empty text (the line-item label)
      - at least one of col[1:] is a numeric value (not a date, not a unit label)
    Rows above this index are treated as header rows.
    """
    for i, row in enumerate(raw):
        if not row[0].strip():
            continue
        nums = [c for c in row[1:] if c.strip() and _is_numeric(c)]
        if nums:
            return i
    return len(raw)   # no data found


_HEADER_HINT = re.compile(
    r"quarter|month|ended|change|rs'|rs\.|group|company|31\.03|%\s*$",
    re.IGNORECASE,
)


def _collect_header_rows(raw: list[list[str]],
                         first_data_idx: int) -> list[list[str]]:
    """
    Return header rows before the first data row, excluding title/footnote
    lines that were accidentally duplicated across every column.
    """
    out: list[list[str]] = []
    for r in raw[:first_data_idx]:
        if not any(c.strip() for c in r):
            continue
        cells = [c.strip() for c in r[1:] if c.strip()]
        if not cells:
            continue
        # Skip rows where every column has the same long duplicated text
        if len(set(cells)) == 1 and len(cells[0]) > 30:
            continue
        # Keep rows that look like real table headers
        if any(_HEADER_HINT.search(c) for c in cells):
            out.append(r)
            continue
        # Keep short header rows (span titles like "Quarter Ended")
        if all(len(c) < 25 for c in cells):
            out.append(r)
    return out


def _build_columns_v2(header_rows: list[list[str]], n_cols: int) -> list[str]:
    """
    Build a column name for each column index by merging content from
    all header rows.  Column 0 is always "Line Item".  Duplicate column
    labels are disambiguated with a "(2)", "(3)" suffix so the values
    dict can store every column without overwriting.
    """
    if not header_rows:
        return ["Line Item"] + [f"Col{i}" for i in range(1, n_cols)]

    cols: list[str] = []
    for col_idx in range(n_cols):
        parts: list[str] = []
        for hr in header_rows:
            if col_idx < len(hr):
                v = hr[col_idx].strip()
                if v and v not in parts:
                    parts.append(v)
        cols.append(" / ".join(parts) if parts else f"Col{col_idx}")

    if cols:
        cols[0] = "Line Item"

    seen: dict[str, int] = {}
    for i, name in enumerate(cols):
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            cols[i] = f"{name} ({seen[name]})"
    return cols


def build_statement_record(title: str, pages: list[int],
                           dfs: list[pd.DataFrame]) -> dict:
    """
    Convert one or more DataFrames (for a multi-table page) into the
    canonical statement record.
    """
    all_raw: list[list[str]] = []
    for df in dfs:
        all_raw.extend(_df_raw(df))

    # Deduplicate exact-duplicate consecutive rows (camelot artifact)
    deduped: list[list[str]] = []
    for row in all_raw:
        if not deduped or row != deduped[-1]:
            deduped.append(row)
    all_raw = deduped

    # Strip rows where ALL cells (including col 0) are empty
    all_raw = [r for r in all_raw if any(c.strip() for c in r)]

    # Strip pure-title rows: col[0] has text but ALL other cols are empty
    while all_raw and all(not c.strip() for c in all_raw[0][1:]):
        all_raw = all_raw[1:]

    if not all_raw:
        return {"title": title, "pages": pages,
                "columns": [], "rows": [], "raw_rows": []}

    n_cols       = max(len(r) for r in all_raw)
    # Pad every row to n_cols
    all_raw      = [r + [""] * (n_cols - len(r)) for r in all_raw]

    first_data   = _find_first_data_row(all_raw)
    header_rows  = _collect_header_rows(all_raw, first_data)
    columns      = _build_columns_v2(header_rows, n_cols)

    # Build data rows
    structured: list[dict] = []
    col_keys = columns[1:]   # labels for value columns
    for row in all_raw[first_data:]:
        label  = _clean(row[0])
        if label.endswith(" -"):
            label = label[:-2].strip()
        values: dict[str, str] = {}
        for idx in range(1, n_cols):
            col_label = col_keys[idx - 1] if (idx - 1) < len(col_keys) else f"Col{idx}"
            values[col_label] = _clean(row[idx])
        if any(v.strip() for v in values.values()):
            structured.append({"label": label, "values": values})

    return {
        "title"       : title,
        "pages"       : pages,
        "columns"     : columns,
        "header_rows" : header_rows,
        "rows"        : structured,
        "raw_rows"    : all_raw,
    }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 – KEY DEDUPLICATION
# ═══════════════════════════════════════════════════════════════════════════

def _unique_key(base_key: str, used: dict[str, int]) -> str:
    """
    Return a unique statement key.  When the same statement type appears
    more than once (e.g. consolidated + company on the same key), append _2.
    """
    if base_key not in used:
        used[base_key] = 1
        return base_key
    used[base_key] += 1
    return f"{base_key}_{used[base_key]}"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 – MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8.5 – OpenAI VERBATIM EXTRACTION (reuses step3_send_to_openai.py)
# ═══════════════════════════════════════════════════════════════════════════

def _load_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser (no python-dotenv dependency)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env[k] = v
    except Exception:
        pass
    return env


def resolve_api_key(cli_key: str | None = None) -> str | None:
    """CLI flag → OPENAI_API_KEY env var → backend/.env"""
    if cli_key:
        return cli_key
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    backend_env = Path(__file__).resolve().parent.parent / ".env"
    return _load_env_file(backend_env).get("OPENAI_API_KEY")


def _extract_with_openai(
    stmt_key:  str,
    title:     str,
    img_paths: list[Path],
    client,
    model:     str,
) -> dict:
    """
    Send the captured page images for ONE statement to OpenAI using the
    verbatim transcription prompt from step3_send_to_openai.py and return
    a step3-shaped dict:
        { "status": "ok",
          "title":  "<original title>",
          "data":   { "statement_title": ..., "tables": [...], ... } }
    """
    if not _STEP3_AVAILABLE:
        return {
            "status": "step3_unavailable",
            "title":  title,
            "error":  _STEP3_IMPORT_ERROR,
        }
    if not img_paths:
        return {"status": "no_images", "title": title}

    prompt_key = QSTMT_TO_STEP3.get(stmt_key, "income_statement")
    prompt     = _quarterly_prompt(prompt_key, title)

    try:
        b64s = [_step3._load_b64(p) for p in img_paths]
        raw  = _step3.call_gpt4o(client, b64s, prompt, model=model)
    except Exception as e:
        log.warning("    OpenAI call failed for %s: %s", stmt_key, e)
        return {"status": "api_error", "title": title, "error": str(e)}

    parsed = _step3.extract_json(raw)
    if parsed is None:
        log.warning("    Could not parse JSON from OpenAI for %s", stmt_key)
        return {
            "status":       "parse_failed",
            "title":        title,
            "raw_response": (raw or "")[:4000],
        }

    return {"status": "ok", "title": title, "data": parsed}


MAX_QUARTERLY_VALIDATION_ROUNDS = 3


def _stmt_rule_for_base(base_key: str) -> tuple[str, str, str] | None:
    for pattern, key, title in STMT_RULES:
        if key == base_key:
            return pattern, key, title
    return None


def _stmt_rules_for_bases(base_keys: list[str]) -> list[tuple[re.Pattern[str], str, str]]:
    out: list[tuple[re.Pattern[str], str, str]] = []
    for bk in base_keys:
        rule = _stmt_rule_for_base(bk)
        if rule:
            pat, key, title = rule
            out.append((re.compile(pat, re.I | re.M), key, title))
    return out


def _rescan_quarterly_for_families(
    pdf_path: Path,
    captures_dir: Path,
    missing_families: list[str],
    existing_keys: set[str],
    use_ocr: bool,
) -> dict[str, dict]:
    """
    Second-pass PDF walk: find pages for quarterly families still missing.
    Returns new statement records keyed by unique stmt_key.
    """
    base_keys = val.quarterly_keys_for_missing_families(missing_families)
    if not base_keys:
        return {}

    want_patterns = _stmt_rules_for_bases(base_keys)

    if not want_patterns:
        return {}

    found: dict[str, dict] = {}
    used_keys: dict[str, int] = dict.fromkeys(existing_keys, 1)
    extra_count = 0

    fitz_doc = fitz.open(str(pdf_path))
    try:
        with pdfplumber.open(str(pdf_path)) as plumber_pdf:
            notes_reached = False
            for page_index, plumber_page in enumerate(plumber_pdf.pages):
                page_num = page_index + 1
                text = best_page_text(plumber_page, fitz_doc, page_index, use_ocr)

                if not notes_reached and is_notes_page(text):
                    notes_reached = True
                    break
                if is_skip_page(text):
                    continue

                matched: tuple[str, str] | None = None
                for pat, base_key, title in want_patterns:
                    if pat.search(text):
                        matched = (base_key, title)
                        break
                if not matched:
                    continue

                base_key, title = matched
                stmt_key = _unique_key(base_key, used_keys)

                stmt_cap_dir = captures_dir / stmt_key
                stmt_cap_dir.mkdir(parents=True, exist_ok=True)
                try:
                    fitz_page = fitz_doc[page_index]
                    pix = fitz_page.get_pixmap(
                        matrix=fitz.Matrix(2.0, 2.0),
                        colorspace=fitz.csRGB,
                        alpha=False,
                    )
                    png_path = stmt_cap_dir / f"page_{page_num:03d}.png"
                    pix.save(str(png_path))
                except Exception:
                    pass

                dfs = extract_page_tables(
                    pdf_path=str(pdf_path),
                    page_num=page_num,
                    plumber_page=plumber_page,
                    fitz_doc=fitz_doc,
                    use_ocr=use_ocr,
                )
                if stmt_key in found:
                    rec = found[stmt_key]
                    for df in dfs:
                        rec["raw_rows"].extend(_df_raw(df))
                    rec["pages"].append(page_num)
                elif dfs:
                    record = build_statement_record(title, [page_num], dfs)
                    if record["rows"] or record["raw_rows"]:
                        found[stmt_key] = record
                        log.info("   [rescan] p%d [%s] recovered", page_num, stmt_key)
    finally:
        fitz_doc.close()

    return found


def _validate_and_fill_quarterly_gaps(
    *,
    pdf_path: Path,
    company_dir: Path,
    captures_dir: Path,
    api_results: dict[str, dict],
    statements: dict[str, dict],
    api_client,
    model: str,
    dry_run: bool,
    use_ocr: bool,
    company_slug: str | None = None,
    max_rounds: int = MAX_QUARTERLY_VALIDATION_ROUNDS,
) -> tuple[dict[str, dict], val.ValidationResult]:
    """Validate quarterly extraction and retry failed / missing families."""
    result_doc = {"statements": api_results}
    log.info("   [validate] required quarterly families: %s",
             ", ".join(val.QUARTERLY_REQUIRED_FAMILIES))

    last_report = val.validate_quarterly_results(result_doc)

    for round_num in range(1, max_rounds + 1):
        if last_report.ok or dry_run or api_client is None:
            break

        gaps = last_report.all_gaps()
        if not gaps:
            break

        log.info(
            "   [validate] round %d/%d — gaps: %s",
            round_num, max_rounds, ", ".join(gaps),
        )

        # Retry failed API extractions first.
        for key in list(last_report.failed_extraction):
            stmt_dir = captures_dir / key
            if not stmt_dir.is_dir():
                continue
            img_paths = sorted(stmt_dir.glob("page_*.png"))
            title = (api_results.get(key) or {}).get("title") or key
            log.info("   [validate] re-extract %s (%d images)", key, len(img_paths))
            api_results[key] = _extract_with_openai(
                stmt_key=key, title=title,
                img_paths=img_paths, client=api_client, model=model,
            )

        # Re-scan PDF for missing statement families.
        if last_report.missing_manifest:
            new_stmts = _rescan_quarterly_for_families(
                pdf_path, captures_dir, last_report.missing_manifest,
                set(api_results.keys()) | set(statements.keys()),
                use_ocr,
            )
            for stmt_key, record in new_stmts.items():
                statements[stmt_key] = record
                title = record.get("title") or stmt_key
                img_paths = sorted((captures_dir / stmt_key).glob("page_*.png"))
                log.info(
                    "   [validate] API extract rescanned %s (%d images)",
                    stmt_key, len(img_paths),
                )
                api_results[stmt_key] = _extract_with_openai(
                    stmt_key=stmt_key, title=title,
                    img_paths=img_paths, client=api_client, model=model,
                )

        last_report = val.validate_quarterly_results({"statements": api_results})

    meta_path = company_dir / "validation_report_quarterly.json"
    meta_path.write_text(
        json.dumps(last_report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return api_results, last_report


def _slug(name: str) -> str:
    """Make a filesystem-safe slug from a company name."""
    s = re.sub(r"[^\w\s-]", "", name.lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s or "company"


def process_pdf(pdf_path: Path, output_dir: Path,
                use_ocr: bool = True,
                api_client = None,
                model: str = "gpt-5",
                dry_run: bool = False,
                # Optional path overrides — used by
                # `process_quarterly_for_company` so the quarterly
                # output can co-exist next to annual results in
                # `backend/testing/<COMPANY_KEY>/`.
                company_dir_override: Path | None = None,
                slug_override:        str | None  = None,
                results_filename:     str | None  = None,
                meta_filename:        str         = "extraction_meta.json",
                captures_dirname:     str         = "captures") -> dict:
    """
    Full extraction pipeline for one PDF file.

    Pipeline:
      1. Detect financial-statement pages (page-by-page classification).
      2. Rasterise every detected page to PNG under
         <company_dir>/<captures_dirname>/<stmt_key>/page_NNN.png .
      3. ALSO run the local rule-based table extractor (kept as a fallback
         for offline / dry-run viewing).
      4. If `api_client` is given (and `dry_run` is False), send each
         statement's page images to OpenAI for VERBATIM transcription
         using the prompts defined in step3_send_to_openai.py, and use
         that as the canonical extraction output.

    Returns the summary dict (also saved to disk).
    """
    log.info("── Processing: %s", pdf_path.name)

    # ── Metadata ────────────────────────────────────────────────────────
    company, period = extract_company_and_period(str(pdf_path))
    slug = slug_override or _slug(company) or pdf_path.stem

    # ── Output directories ───────────────────────────────────────────────
    company_dir = company_dir_override or (output_dir / slug)
    captures_dir = company_dir / captures_dirname
    company_dir.mkdir(parents=True, exist_ok=True)
    captures_dir.mkdir(exist_ok=True)

    fitz_doc  = fitz.open(str(pdf_path))
    statements: dict[str, dict] = {}
    used_keys: dict[str, int]   = {}
    extra_count                 = 0
    notes_reached               = False

    with pdfplumber.open(str(pdf_path)) as plumber_pdf:
        n_pages = len(plumber_pdf.pages)
        log.info("   Company : %s", company)
        log.info("   Period  : %s", period)
        log.info("   Pages   : %d", n_pages)

        for page_index, plumber_page in enumerate(plumber_pdf.pages):
            page_num = page_index + 1

            # ── Text ──────────────────────────────────────────────────
            text = best_page_text(plumber_page, fitz_doc, page_index, use_ocr)

            # ── Stop at Notes ─────────────────────────────────────────
            if not notes_reached and is_notes_page(text):
                notes_reached = True
                log.info("   p%d: Notes section detected — stopping", page_num)
                break

            # ── Skip non-financial pages ──────────────────────────────
            if is_skip_page(text):
                log.info("   p%d: skipped (non-financial page)", page_num)
                continue

            # ── Classify ──────────────────────────────────────────────
            classified = classify_page(text)

            # Continuation-page heuristic: if a page has no heading match but
            # follows a known statement AND contains only numeric/label rows
            # (no new statement header), treat it as a continuation of the
            # most-recently written statement.
            if classified is None and statements and len(text.strip()) >= 20:
                last_key = list(statements.keys())[-1]
                last_record = statements[last_key]
                # Check: does this page NOT match any notes-stop pattern, and
                # does it have ≥2 rows that look numeric?
                import re as _re
                num_lines = sum(
                    1 for l in text.splitlines()
                    if _re.search(r"\d[\d,\.]+", l)
                )
                if num_lines >= 2:
                    # Treat as continuation of last statement
                    cont_dfs = extract_page_tables(
                        pdf_path=str(pdf_path), page_num=page_num,
                        plumber_page=plumber_page, fitz_doc=fitz_doc,
                        use_ocr=use_ocr,
                    )
                    if cont_dfs:
                        for df in cont_dfs:
                            last_record["raw_rows"].extend(_df_raw(df))
                        last_record["pages"].append(page_num)
                        # Also rasterise this page under the last stmt key
                        try:
                            stmt_cap_dir = captures_dir / last_key
                            stmt_cap_dir.mkdir(exist_ok=True)
                            fitz_page = fitz_doc[page_index]
                            pix = fitz_page.get_pixmap(
                                matrix=fitz.Matrix(2.0, 2.0),
                                colorspace=fitz.csRGB, alpha=False,
                            )
                            (stmt_cap_dir / f"page_{page_num:03d}.png").save(str(
                                stmt_cap_dir / f"page_{page_num:03d}.png"))
                            pix.save(str(stmt_cap_dir / f"page_{page_num:03d}.png"))
                        except Exception:
                            pass
                        log.info("   p%d: continuation → merged into [%s]",
                                 page_num, last_key)
                        continue
            if classified is None and len(text.strip()) < 50:
                log.info("   p%d: skipped (unclassified / empty)", page_num)
                continue

            if classified:
                base_key, title = classified
            else:
                extra_count += 1
                base_key = f"extra_table_{extra_count}"
                title    = f"Extra Table {extra_count}"

            stmt_key = _unique_key(base_key, used_keys)

            # ── Rasterise page for testing viewer ─────────────────────
            stmt_cap_dir = captures_dir / stmt_key
            stmt_cap_dir.mkdir(exist_ok=True)
            try:
                fitz_page = fitz_doc[page_index]
                pix = fitz_page.get_pixmap(
                    matrix=fitz.Matrix(2.0, 2.0),   # ~144 DPI
                    colorspace=fitz.csRGB, alpha=False,
                )
                png_path = stmt_cap_dir / f"page_{page_num:03d}.png"
                pix.save(str(png_path))
            except Exception as e:
                log.debug("   rasterise p%d: %s", page_num, e)

            # ── Extract tables ────────────────────────────────────────
            dfs = extract_page_tables(
                pdf_path  = str(pdf_path),
                page_num  = page_num,
                plumber_page = plumber_page,
                fitz_doc  = fitz_doc,
                use_ocr   = use_ocr,
            )

            if not dfs:
                log.info("   p%d [%s]: no tables found", page_num, stmt_key)
                continue

            # ── Build structured record ───────────────────────────────
            # If the key already exists (continuation page), merge rows
            if stmt_key in statements:
                existing = statements[stmt_key]
                for df in dfs:
                    existing["raw_rows"].extend(_df_raw(df))
                existing["pages"].append(page_num)
                log.info("   p%d [%s]: merged continuation (%d dfs)",
                         page_num, stmt_key, len(dfs))
            else:
                record = build_statement_record(title, [page_num], dfs)
                # Skip truly empty records (e.g. cover page picked up by camelot)
                if not record["rows"] and not record["raw_rows"]:
                    log.info("   p%d [%s]: skipped (no extractable data)",
                             page_num, stmt_key)
                    # Roll back the key counter so the slot is not wasted
                    if stmt_key in used_keys:
                        used_keys[stmt_key] = max(0, used_keys[stmt_key] - 1)
                        if used_keys[stmt_key] == 0:
                            del used_keys[stmt_key]
                    continue
                statements[stmt_key] = record
                log.info("   p%d [%s]: %d row(s) × %d col(s)",
                         page_num, stmt_key,
                         len(record["rows"]), len(record["columns"]))

    fitz_doc.close()

    # ── OpenAI verbatim extraction per statement ─────────────────────────
    api_results: dict[str, dict] = {}
    use_api      = (api_client is not None) and (not dry_run)
    api_status   = (
        "ok"        if use_api else
        "dry_run"   if dry_run else
        "no_api_key"
    )

    for stmt_key, record in statements.items():
        # Map the per-statement title (already in record) and find its images
        title    = record.get("title") or stmt_key.replace("_", " ").title()
        stmt_dir = captures_dir / stmt_key
        if not stmt_dir.is_dir():
            api_results[stmt_key] = {
                "status": "no_capture_folder", "title": title}
            continue

        img_paths = sorted(stmt_dir.glob("page_*.png"))

        if dry_run:
            api_results[stmt_key] = {
                "status": "dry_run",
                "title":  title,
                "pages":  [p.name for p in img_paths],
            }
            continue
        if not use_api:
            api_results[stmt_key] = {
                "status": "no_api_key",
                "title":  title,
                "pages":  [p.name for p in img_paths],
            }
            continue

        log.info("   [api] %s — %d image(s) → %s",
                 stmt_key, len(img_paths), model)
        api_results[stmt_key] = _extract_with_openai(
            stmt_key=stmt_key, title=title,
            img_paths=img_paths, client=api_client, model=model,
        )

    # ── Validation + targeted re-extraction ─────────────────────────────
    validation_report: val.ValidationResult | None = None
    if use_api:
        api_results, validation_report = _validate_and_fill_quarterly_gaps(
            pdf_path=pdf_path,
            company_dir=company_dir,
            captures_dir=captures_dir,
            api_results=api_results,
            statements=statements,
            api_client=api_client,
            model=model,
            dry_run=dry_run,
            use_ocr=use_ocr,
            company_slug=slug_override or slug,
        )

    # ── Write per-company results JSON in the step3 verbatim shape ──────
    result = {
        "company"        : company,
        "period"         : period,
        "source_pdf"     : pdf_path.name,
        "generated_at"   : datetime.now().isoformat(timespec="seconds"),
        "model"          : model if use_api else None,
        "api_status"     : api_status,
        # `statements` holds the OpenAI-extracted data (in step3 verbatim
        # shape) keyed by our quarterly statement keys.  When the API is
        # disabled this is empty / status-only and the local rule-based
        # extraction is preserved under "local_statements".
        "statements"     : api_results,
        "local_statements": statements,
    }

    results_path = company_dir / (results_filename or f"{slug}_results.json")
    results_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Drop a step3-style extraction_meta.json so testing.py-style viewers
    # (and re-runs) can detect that this folder is "done".
    meta_path = company_dir / meta_filename
    meta_path.write_text(
        json.dumps({
            "company":      company,
            "source_pdf":   pdf_path.name,
            "model":        model if use_api else None,
            "api_status":   api_status,
            "statements":   sorted(api_results.keys()),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run":      dry_run,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    n_ok_api = sum(1 for v in api_results.values() if v.get("status") == "ok")

    summary = {
        "file"           : pdf_path.name,
        "company"        : company,
        "period"         : period,
        "slug"           : slug,
        "results_json"   : str(results_path),
        "n_statements"   : len(statements),
        "n_api_ok"       : n_ok_api,
        "api_status"     : api_status,
        "statement_keys" : list(statements.keys()),
    }
    if validation_report is not None:
        summary["validation"] = validation_report.to_dict()
        summary["validation_ok"] = validation_report.ok
        if not validation_report.ok:
            summary["gaps"] = validation_report.all_gaps()
    log.info("   ✓ %d statement(s) detected, %d via OpenAI → %s",
             len(statements), n_ok_api, results_path)
    return summary


def process_quarterly_for_company(
    company_key: str,
    pdf_path:    Path,
    testing_dir: Path,
    api_key:     str | None = None,
    model:       str = "gpt-5",
    dry_run:     bool = False,
    use_ocr:     bool = True,
    force:       bool = False,
) -> dict:
    """
    Run the QUARTERLY extraction pipeline for ONE company / ONE PDF, writing
    output INTO an existing per-company folder (typically the same one
    Data_retrive.py / Extract_selected_reports.py already populates with
    Annual artefacts):

        <testing_dir>/<company_key>/
            <company_key>_quarterly_results.json
            extraction_meta_quarterly.json
            captures_quarterly/<stmt_key>/page_NNN.png

    This lets the Extracted Tables UI offer an Annual ⇄ Quarterly toggle
    on the same company entry without splitting it into two folders.
    """
    company_dir = (testing_dir / company_key)
    company_dir.mkdir(parents=True, exist_ok=True)

    results_filename = f"{company_key}_quarterly_results.json"
    results_path     = company_dir / results_filename

    # Honour --force / --skip-existing semantics like Data_retrive does.
    if not force and results_path.exists():
        try:
            data = json.loads(results_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and not data.get("dry_run"):
                log.info("   [skip] %s quarterly results already exist",
                         company_key)
                return {
                    "company": company_key,
                    "status":  "skipped_existing",
                    "results_json": str(results_path),
                }
        except Exception:
            pass

    # Build the OpenAI client (if we have a key).
    client = None
    if api_key and _STEP3_AVAILABLE and not dry_run:
        try:
            client = _step3.OpenAI(api_key=api_key)
        except Exception as e:
            log.warning("Could not init OpenAI client: %s", e)
            client = None

    summary = process_pdf(
        pdf_path             = pdf_path,
        output_dir           = testing_dir,    # parent (unused due to overrides)
        use_ocr              = use_ocr,
        api_client           = client,
        model                = model,
        dry_run              = dry_run,
        company_dir_override = company_dir,
        slug_override        = company_key,
        results_filename     = results_filename,
        meta_filename        = "extraction_meta_quarterly.json",
        captures_dirname     = "captures_quarterly",
    )

    summary["company"]      = company_key
    summary["results_json"] = str(results_path)

    validation = summary.get("validation") or {}
    if validation and not validation.get("ok", True):
        summary["status"] = "missing_statements"
        summary["gaps"] = summary.get("gaps") or validation.get("missing_manifest", [])
    else:
        summary["status"] = "ok"
    return summary


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 – BATCH RUNNER  (one Q report per company)
# ═══════════════════════════════════════════════════════════════════════════

def _first_quarterly_pdf(company_dir: Path) -> Path | None:
    """
    Return the FIRST quarterly PDF (sorted by filename) for a company,
    or None if the company has no Quarterly/*.pdf.
    """
    q_dir = company_dir / "Quarterly"
    if not q_dir.is_dir():
        return None
    pdfs = sorted(q_dir.glob("*.pdf"))
    return pdfs[0] if pdfs else None


def run_reports_batch(reports_dir: Path, output_dir: Path,
                      use_ocr: bool = True,
                      api_key:  str | None = None,
                      model:    str = "gpt-5",
                      dry_run:  bool = False) -> list[dict]:
    """
    Walk every <reports>/<COMPANY>/Quarterly/ folder, take the FIRST
    quarterly PDF per company, and run the extraction pipeline on it.
    Logs progress in a clean per-company format:

        company 1 (ACL_PLASTICS_PLC) Q report running
            -> 643_1464256279305.03.2016.pdf
        done

        company 2 (AGARAPATANA_PLANTATIONS_PLC) Q report running
            -> 3027_1695207844797.pdf
        done
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if not reports_dir.is_dir():
        log.error("Reports folder not found: %s", reports_dir)
        return []

    companies = sorted(p for p in reports_dir.iterdir() if p.is_dir())
    if not companies:
        log.error("No company subfolders in %s", reports_dir)
        return []

    # Build OpenAI client once (or print a clear message if disabled)
    client = None
    api_mode = "disabled"
    if dry_run:
        api_mode = "dry-run (no API calls)"
    elif api_key and _STEP3_AVAILABLE:
        try:
            client = _step3.OpenAI(api_key=api_key)
            api_mode = f"OpenAI ({model})"
        except Exception as e:
            log.error("Failed to init OpenAI client: %s", e)
            client = None
            api_mode = "OpenAI INIT FAILED — falling back to no-API"
    elif api_key and not _STEP3_AVAILABLE:
        api_mode = f"step3 import failed: {_STEP3_IMPORT_ERROR}"
    else:
        api_mode = "no API key — local rule-based only"

    print("=" * 70)
    print(f"  Q_DATA_EXTRACTION — first quarterly report per company")
    print(f"  Reports : {reports_dir.resolve()}")
    print(f"  Output  : {output_dir.resolve()}")
    print(f"  Found   : {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}")
    print(f"  Camelot : {'available' if _CAMELOT else 'NOT installed (pdfplumber fallback)'}")
    print(f"  API     : {api_mode}")
    print("=" * 70 + "\n")

    summaries: list[dict] = []
    n_done = n_skip = n_fail = 0

    for idx, company_dir in enumerate(companies, start=1):
        company_name = company_dir.name
        pdf_path = _first_quarterly_pdf(company_dir)

        print(f"company {idx} ({company_name}) Q report running")

        if pdf_path is None:
            print(f"    -> no Quarterly/*.pdf found")
            print(f"skipped\n")
            n_skip += 1
            continue

        print(f"    -> {pdf_path.name}")
        try:
            summary = process_pdf(
                pdf_path, output_dir,
                use_ocr   = use_ocr,
                api_client = client,
                model     = model,
                dry_run   = dry_run,
            )
            summaries.append(summary)
            api_note = (
                f", {summary.get('n_api_ok', 0)} via API"
                if client and not dry_run else ""
            )
            print(f"done  ({summary['n_statements']} statement(s){api_note} "
                  f"-> {summary['slug']}/"
                  f"{Path(summary['results_json']).name})\n")
            n_done += 1
        except Exception as ex:
            log.exception("    extraction failed for %s", pdf_path.name)
            print(f"failed: {ex}\n")
            n_fail += 1

    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summaries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"  ALL COMPANIES PROCESSED")
    print(f"  done    : {n_done}")
    print(f"  skipped : {n_skip}  (no Quarterly folder / no PDFs)")
    print(f"  failed  : {n_fail}")
    print(f"  summary : {summary_path}")
    print("=" * 70 + "\n")

    return summaries


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 – CLI
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Extract financial tables from CSE quarterly-report PDFs → JSON"
    )
    ap.add_argument(
        "--reports", type=Path, default=None, metavar="DIR",
        help="Reports root: process the FIRST Quarterly PDF for every "
             "<DIR>/<COMPANY>/Quarterly/ subfolder.",
    )
    ap.add_argument(
        "--pdf", type=Path, action="append", dest="pdfs", default=None,
        metavar="FILE",
        help="PDF to process (repeatable; ignored when --reports is given).",
    )
    ap.add_argument(
        "--out", type=Path, default=Path("extracted"),
        help="Output root directory  (default: ./extracted)",
    )
    ap.add_argument(
        "--no-ocr", action="store_true",
        help="Disable OCR fallback",
    )
    ap.add_argument(
        "--verbose", action="store_true",
        help="Enable DEBUG logging",
    )
    ap.add_argument(
        "--apikey", default=None,
        help="OpenAI API key (else OPENAI_API_KEY env var or backend/.env)",
    )
    ap.add_argument(
        "--model", default="gpt-5",
        help="OpenAI model name  (default: gpt-5)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Render images and run local extraction, but DON'T call OpenAI",
    )
    args = ap.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Quiet the per-page INFO chatter when running the batch — keeps the
    # "company N … done" logs readable. Errors/warnings still show.
    if args.reports and not args.verbose:
        logging.getLogger("q_extraction").setLevel(logging.WARNING)

    api_key = resolve_api_key(args.apikey)

    args.out.mkdir(parents=True, exist_ok=True)

    if args.reports:
        run_reports_batch(
            args.reports, args.out,
            use_ocr = not args.no_ocr,
            api_key = api_key,
            model   = args.model,
            dry_run = args.dry_run,
        )
        return

    pdfs: list[Path] = args.pdfs or sorted(Path(".").glob("*.pdf"))
    if not pdfs:
        log.error("No PDF files found. Use --reports DIR or --pdf FILE.")
        sys.exit(1)

    # Build OpenAI client once for ad-hoc --pdf runs as well
    client = None
    if api_key and _STEP3_AVAILABLE and not args.dry_run:
        try:
            client = _step3.OpenAI(api_key=api_key)
        except Exception as e:
            log.error("Failed to init OpenAI client: %s", e)
    elif api_key and not _STEP3_AVAILABLE:
        log.warning("step3 import failed (%s) — running without API",
                    _STEP3_IMPORT_ERROR)

    summaries: list[dict] = []
    for pdf_path in pdfs:
        if not pdf_path.exists():
            log.error("Not found: %s", pdf_path)
            continue
        try:
            s = process_pdf(
                pdf_path, args.out,
                use_ocr   = not args.no_ocr,
                api_client = client,
                model     = args.model,
                dry_run   = args.dry_run,
            )
            summaries.append(s)
        except Exception:
            log.exception("Failed: %s", pdf_path.name)

    summary_path = args.out / "summary.json"
    summary_path.write_text(
        json.dumps(summaries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n" + "=" * 64)
    print("  Q_DATA_EXTRACTION — COMPLETE")
    print("=" * 64)
    for s in summaries:
        print(f"  {s['file']:<45}  {s['n_statements']} statement(s)")
        for k in s["statement_keys"]:
            print(f"    · {k}")
    print(f"\n  Output root  : {args.out.resolve()}")
    print(f"  Summary JSON : {summary_path}")
    print()


if __name__ == "__main__":
    main()
