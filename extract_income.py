"""
sofp_extractor.py
=================
Robust Statement of Financial Position (SoFP / Balance Sheet) extractor.

Works with any annual-report layout by:
  1. Finding the SoFP page via TOC hints + heading/keyword scoring
  2. Rebuilding the table from raw word x/y coordinates (NOT pdfplumber's
     borderless-table detector, which frequently merges or splits cells)
  3. Detecting columns by clustering the right-edge (x1) of numeric words
     (requires ≥3 hits per cluster to avoid footnote noise)
  4. Separating the Note column so it is preserved in line_items
  5. Stitching multi-page SoFPs (equity section on next page) automatically
  6. Trimming footnotes / signatures from the bottom

Handles:
  - 2-column layouts  (Note | 2025 | 2024)
  - 4-column layouts  (Note | Bank 2025 | Bank 2024 | Group 2025 | Group 2024)
  - 5-column layouts  (Note | 2025 | 2024 | 2023 | Company 2025 | Company 2024)
  - Reports without a Note column
  - Multi-line labels (long item names wrapping to 2-3 physical lines)
  - Dash / nil placeholders for zero values
  - Per-share rows at the end

JSON output structure
---------------------
{
  "company": "ACME",
  "source_pdf": "/path/to/report.pdf",
  "extracted_at": "2025-01-01T12:00:00",
  "statement_pdf_page_1based": 172,
  "has_note_col": true,
  "column_headers": ["2025", "2024"],
  "line_items": [
    {
      "label":            "Cash and cash equivalents",
      "is_section_header": false,
      "note":             "16",
      "amounts_raw":      ["492,275,401", "420,293,003"],
      "amounts":          [492275401, 420293003]
    },
    ...
  ],
  "raw_grid": [[...], ...]   <- raw extracted grid for debugging
}

Usage (CLI):
    python sofp_extractor.py report.pdf --company ACME --out acme_sofp.json

Usage (Python):
    from sofp_extractor import extract_sofp
    doc = extract_sofp("report.pdf", "ACME", "acme_sofp.json")

Batch usage:
    from sofp_extractor import run_batch
    from pathlib import Path
    run_batch([("ACME", "acme.pdf"), ("XYZ", "xyz.pdf")], json_dir=Path("json_logs"))
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pdfplumber


# ─────────────────────────────────────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_year(s: str) -> bool:
    return bool(re.match(r"^(?:19|20)\d{2}$", (s or "").strip()))


def _is_amount(s: str) -> bool:
    """True for monetary values including dash/nil placeholders."""
    t = (s or "").strip()
    if not t:
        return False
    if t in ("-", "–", "—", "nil", "Nil"):
        return True
    if re.match(r"^\([\d,\s.]+\)$", t):
        return bool(re.search(r"\d", t))
    clean = re.sub(r"[\s,]", "", t)
    return bool(re.match(r"^[\d.]+$", clean) and re.search(r"\d", clean))


def _is_big_amount(s: str) -> bool:
    """Amount with ≥4 digits — not a 1-2 digit note ref."""
    if not _is_amount(s):
        return False
    return len(re.sub(r"\D", "", (s or ""))) >= 4


def _parse_num(s: str):
    """Convert amount string → int/float or None."""
    t = (s or "").strip()
    if not t or t in ("-", "–", "—", "nil", "Nil"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    clean = re.sub(r"[^\d.]", "", t)
    if not clean:
        return None
    try:
        n = float(clean) if "." in clean else int(clean)
        return -n if neg else n
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Word / line grouping
# ─────────────────────────────────────────────────────────────────────────────

def _group_lines(words: list[dict], y_tol: float = 4.0) -> list[list[dict]]:
    """Cluster word dicts into visual lines by top-y proximity."""
    items = [w for w in words if (w.get("text") or "").strip()]
    if not items:
        return []
    items.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = [[items[0]]]
    for w in items[1:]:
        if abs(w["top"] - lines[-1][0]["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def _line_text(ln: list[dict]) -> str:
    return " ".join(w["text"].strip() for w in ln if w.get("text", "").strip())


# ─────────────────────────────────────────────────────────────────────────────
# Column detection  (cluster x1 right-edges of numeric words)
# ─────────────────────────────────────────────────────────────────────────────

def _cluster(vals: list[float], tol: float) -> list[tuple[float, int]]:
    """
    Cluster values and return list of (centre, count) sorted by centre.
    """
    if not vals:
        return []
    sv = sorted(vals)
    groups: list[list[float]] = [[sv[0]]]
    for v in sv[1:]:
        if abs(v - groups[-1][-1]) <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [(sum(g) / len(g), len(g)) for g in groups]


def _detect_cols(lines: list[list[dict]], body_start: int,
                 page_width: float) -> list[float]:
    """
    Cluster x1 (right-edge) of monetary words in the SoFP body.
    Requires ≥3 hits per cluster to filter footnote/prose noise.
    Returns sorted column right-edge centres (max 5).
    """
    x1s: list[float] = []
    for ln in lines[body_start: body_start + 110]:
        for w in ln:
            t = w["text"].strip()
            if (
                (_is_big_amount(t) or t in ("-", "–", "—"))
                and w["x1"] > page_width * 0.38
            ):
                x1s.append(w["x1"])

    if not x1s:
        return []

    # Only keep clusters with ≥3 occurrences (real columns repeat many times)
    all_clusters = _cluster(x1s, tol=14.0)
    centers = [c for c, cnt in all_clusters if c > page_width * 0.38 and cnt >= 3]
    return sorted(centers)[-5:]   # keep rightmost 5 at most


def _detect_note_x1(lines: list[list[dict]], body_start: int,
                    first_amt_x1: float) -> float | None:
    """
    Detect the note-reference column: 1-2 digit numbers sitting
    between the label zone and the amount columns.
    Requires ≥3 hits to be confident.
    """
    x1s: list[float] = []
    for ln in lines[body_start: body_start + 90]:
        for w in ln:
            t = w["text"].strip()
            if (
                re.match(r"^\d{1,2}(?:\.\d{1,2})?$", t)
                and not _is_year(t)
                and 80 < w["x1"] < first_amt_x1 - 8
            ):
                x1s.append(w["x1"])

    if not x1s:
        return None
    clusters = _cluster(x1s, tol=20.0)
    # Need at least 3 note refs to call it a real note column
    valid = [(c, cnt) for c, cnt in clusters if cnt >= 3]
    if not valid:
        return None
    return max(c for c, _ in valid)


# ─────────────────────────────────────────────────────────────────────────────
# Word → column assignment
# ─────────────────────────────────────────────────────────────────────────────

def _assign_col(x1: float, col_x1s: list[float],
                note_x1: float | None) -> int:
    """
    Map a word's x1 to a logical column index:
      0          → description label
      1          → note ref  (only when note_x1 is set)
      1 or 2 … N → amount columns (left to right)
    """
    # Amount columns (nearest centre wins)
    for k, cx in enumerate(col_x1s):
        if abs(x1 - cx) <= 18:
            return (2 if note_x1 is not None else 1) + k

    # Note column
    if note_x1 is not None and abs(x1 - note_x1) <= 22:
        return 1

    return 0   # label zone


# ─────────────────────────────────────────────────────────────────────────────
# Header-line detection
# ─────────────────────────────────────────────────────────────────────────────

_HDR_TOKEN = re.compile(
    r"^(?:group|bank|company|notes?|rs|rs\.|rs\.'?000|rs\.mn|usd|lkr|000|"
    r"restated|mn|rm|as|at|of|the|31st?|30th?|dec(?:ember)?|mar(?:ch)?|"
    r"jun(?:e)?|sep(?:tember)?|jan(?:uary)?|feb(?:ruary)?|apr(?:il)?|"
    r"may|jul(?:y)?|aug(?:ust)?|oct(?:ober)?|nov(?:ember)?|"
    r"(?:19|20)\d{2}|\d{4}th?)$",
    re.I,
)


def _is_header_line(ln: list[dict]) -> bool:
    texts = [w["text"].strip() for w in ln if w.get("text", "").strip()]
    return bool(texts) and all(_HDR_TOKEN.match(t) for t in texts)


def _is_header_row(row: list[str]) -> bool:
    texts = [c.strip() for c in row if c.strip()]
    return bool(texts) and all(_HDR_TOKEN.match(t) for t in texts)


# ─────────────────────────────────────────────────────────────────────────────
# Stop / closing detection
# ─────────────────────────────────────────────────────────────────────────────

_STOP = (
    "the accounting policies and notes",
    "figures in brackets indicate",
    "integral part of these financial",
    "integral part of the financial",
    "statement of changes in equity",
    "statement of cash flows",
    "cash flows from operating",
    "cash flows from invest",
    "approved and signed for",
    "signed for and on behalf",
    "board of directors is responsible",
    "directors are responsible for the",
    "chief executive officer",
    "head of finance",
    "chief financial officer",
    "independent auditor",
    "in compliance with the requirements",
    "companies act no",
    "i certify that",
    "it is certified that",
    "corporate managers",
    "managing director",
)

_CLOSING_RE = re.compile(
    r"total\s+(?:equity\s*(?:&|and)\s*liabilit|liabilit\S*\s*(?:&|and)\s*equity|"
    r"net\s+assets)\b",
    re.I,
)

_SECTION_RE = re.compile(
    r"^(?:assets|liabilities|equity(?:\s+and\s+liabilities)?|"
    r"non.?current\s+assets|current\s+assets|"
    r"non.?current\s+liabilities|current\s+liabilities|"
    r"equity\s+attributable|shareholders.?\s*equity|"
    r"capital\s+and\s+reserves|equity\s+and\s+liabilities)$",
    re.I,
)


def _is_stop(text: str) -> bool:
    tl = text.lower()
    return any(m in tl for m in _STOP)


# ─────────────────────────────────────────────────────────────────────────────
# Single-page row extraction
# ─────────────────────────────────────────────────────────────────────────────

def _page_rows(page, col_x1s: list[float], note_x1: float | None,
               skip_to_assets: bool = True) -> tuple[list[list[str]], bool]:
    """
    Extract structured rows from one page.
    Returns (rows, found_closing_total).

    Row layout: [label, note_or_empty (if note_x1 set), amt1, amt2, ...]
    """
    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
    except Exception:
        return [], False

    lines = _group_lines(words, y_tol=4.0)
    if not lines:
        return [], False

    # Locate start line
    start_i = 0
    if skip_to_assets:
        for i, ln in enumerate(lines):
            txt = _line_text(ln)
            if re.search(r"^\s*ASSETS\s*$", txt, re.I):
                start_i = i
                break
            if any(_is_big_amount(w["text"]) for w in ln):
                start_i = i
                break

    n_cols = (2 if note_x1 is not None else 1) + len(col_x1s)
    rows: list[list[str]] = []
    found_closing = False

    # Multi-line label accumulation
    pending: list[str] | None = None   # row currently being built
    pending_extra: list[str] = []       # extra label text from continuation lines

    def flush():
        nonlocal pending, pending_extra
        if pending is not None:
            if pending_extra:
                pending[0] = (pending[0] + " " + " ".join(pending_extra)).strip()
                pending_extra = []
            rows.append(pending)
            pending = None

    for ln in lines[start_i:]:
        txt = _line_text(ln)
        if not txt.strip():
            continue

        if _is_stop(txt):
            flush()
            break

        # Skip repeated header lines in the body (continuation pages re-print headers)
        if rows and _is_header_line(ln):
            flush()
            continue

        # Build row
        row: list[str] = [""] * n_cols
        has_amt = False
        label_parts: list[str] = []

        for w in ln:
            t = w["text"].strip()
            if not t:
                continue
            col = _assign_col(w["x1"], col_x1s, note_x1)
            if col == 0:
                label_parts.append(t)
            else:
                row[col] = ((row[col] + " " + t).strip() if row[col] else t)
                if _is_big_amount(t) or t in ("-", "–", "—"):
                    has_amt = True

        row[0] = " ".join(label_parts)

        if has_amt:
            flush()
            pending = row
        else:
            txt_stripped = row[0].strip()
            # Section headers flush the pending row first
            is_sec = bool(txt_stripped and _SECTION_RE.match(txt_stripped))
            if pending is not None and not is_sec:
                # Continuation of a multi-line label
                pending_extra.append(txt_stripped)
            else:
                flush()
                if txt_stripped:
                    rows.append(row)

        if _CLOSING_RE.search(txt):
            found_closing = True

    flush()
    return rows, found_closing


# ─────────────────────────────────────────────────────────────────────────────
# Multi-page stitching + trim
# ─────────────────────────────────────────────────────────────────────────────

def _is_continuation(text: str) -> bool:
    tl = text.lower()
    return (
        ("equity" in tl or "shareholders" in tl or "capital and reserves" in tl)
        and "cash flows from" not in tl
        and "income statement" not in tl
        and "statement of comprehensive" not in tl
        and "statement of changes in equity" not in tl
    )


def _trim_closing(rows: list[list[str]]) -> list[list[str]]:
    """Trim rows after the balance-sheet closing total row."""
    last_closing: int | None = None
    for i, row in enumerate(rows):
        txt = " ".join(row)
        if _CLOSING_RE.search(txt) and any(_is_big_amount(c) for c in row):
            last_closing = i

    if last_closing is None:
        return rows

    cutoff = last_closing + 1
    # Keep one extra row for "Net assets per share" type footer
    if cutoff < len(rows):
        nxt = " ".join(rows[cutoff]).lower()
        if "net asset" in nxt and ("per" in nxt or "share" in nxt):
            cutoff += 1

    return rows[:cutoff]


# ─────────────────────────────────────────────────────────────────────────────
# Full grid extraction from a PDF + page index
# ─────────────────────────────────────────────────────────────────────────────

def _extract_grid(pdf, page_idx: int) -> tuple[list[list[str]] | None, bool]:
    """
    Extract raw string grid from the SoFP starting at page_idx.
    Returns (grid, has_note_col).
    """
    n = len(pdf.pages)
    page = pdf.pages[page_idx]

    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
    except Exception:
        return None, False

    lines = _group_lines(words, y_tol=4.0)

    # Locate ASSETS heading
    body_start = 0
    for i, ln in enumerate(lines):
        if re.search(r"^\s*ASSETS\s*$", _line_text(ln), re.I):
            body_start = i
            break

    # Detect column layout
    col_x1s = _detect_cols(lines, body_start, float(page.width))
    if not col_x1s:
        return None, False

    note_x1 = _detect_note_x1(lines, body_start, col_x1s[0])
    has_note = note_x1 is not None

    # Extract header rows (year/entity labels above ASSETS)
    n_cols = (2 if has_note else 1) + len(col_x1s)
    header_rows: list[list[str]] = []
    for ln in lines[max(0, body_start - 8): body_start]:
        if not _is_header_line(ln):
            continue
        row = [""] * n_cols
        label_parts: list[str] = []
        for w in ln:
            t = w["text"].strip()
            if not t:
                continue
            col = _assign_col(w["x1"], col_x1s, note_x1)
            if col == 0:
                label_parts.append(t)
            else:
                row[col] = ((row[col] + " " + t).strip() if row[col] else t)
        row[0] = " ".join(label_parts)
        if any(c.strip() for c in row):
            header_rows.append(row)

    # Extract body rows from this page
    body_rows, found_closing = _page_rows(page, col_x1s, note_x1, skip_to_assets=True)

    if not body_rows:
        return None, False

    # Try continuation page (equity section)
    if not found_closing and page_idx + 1 < n:
        try:
            np_text = pdf.pages[page_idx + 1].extract_text() or ""
        except Exception:
            np_text = ""
        if _is_continuation(np_text):
            cont, _ = _page_rows(pdf.pages[page_idx + 1], col_x1s, note_x1,
                                  skip_to_assets=False)
            cont = [r for r in cont if not _is_header_row(r)]
            body_rows.extend(cont)

    body_rows = _trim_closing(body_rows)

    all_rows = header_rows + body_rows
    if len(all_rows) < 5:
        return None, False

    # Normalise column count
    max_cols = max(len(r) for r in all_rows)
    grid = [r + [""] * (max_cols - len(r)) for r in all_rows]
    return grid, has_note


# ─────────────────────────────────────────────────────────────────────────────
# Page finding
# ─────────────────────────────────────────────────────────────────────────────

_TOC_RE = re.compile(
    r"(?:consolidated\s+)?(?:statement\s+of\s+financial\s+position|balance\s+sheet)"
    r"[^\d\n]{0,120}(\d{1,4})\s*$",
    re.I | re.M,
)


def _footer_page(text: str) -> int | None:
    for line in reversed((text or "").splitlines()[-14:]):
        m = re.match(r"^(\d{1,4})\b(?:\s*[|·•\-]\s*.*)?$", line.strip())
        if m:
            n = int(m.group(1))
            if 1 <= n <= 5000:
                return n
    return None


def _page_off(pdf) -> int | None:
    n = len(pdf.pages)
    offs: list[int] = []
    step = max(1, n // 40)
    for i in range(0, min(n, 400), step):
        try:
            txt = pdf.pages[i].extract_text() or ""
        except Exception:
            continue
        p = _footer_page(txt)
        if p:
            offs.append((i + 1) - p)
    if len(offs) < 3:
        return None
    offs.sort()
    return offs[len(offs) // 2]


def _toc_hints(pdf) -> list[int]:
    hints: list[int] = []
    for i in range(min(len(pdf.pages), 450)):
        try:
            txt = pdf.pages[i].extract_text() or ""
        except Exception:
            continue
        for line in txt.split("\n"):
            m = _TOC_RE.search(line.strip())
            if m:
                p = int(m.group(1))
                if 1 <= p <= len(pdf.pages) + 100:
                    hints.append(p)
    return sorted(set(hints))


def _score(text: str) -> int:
    tl = text.lower()
    sc = 0
    if re.search(r"\bstatement\s+of\s+financial\s+position\b", tl):
        sc += 40
    elif re.search(r"\bbalance\s+sheet\b", tl):
        sc += 20
    if "total assets" in tl:
        sc += 20
    if re.search(r"\btotal\s+equity\s+and\s+liabilit", tl):
        sc += 15
    if re.search(r"\btotal\s+liabilit", tl):
        sc += 10
    if "assets" in tl and "liabilit" in tl:
        sc += 8
    if "as at" in tl or "as of" in tl:
        sc += 5
    if "cash flows from" in tl:
        sc -= 30
    if "statement of changes in equity" in tl:
        sc -= 20
    if re.search(r"\bnotes?\s+to\s+the\b", tl) and sc < 45:
        sc -= 25
    if re.search(r"\bsignificant\s+accounting|material\s+accounting\b", tl):
        sc -= 20
    return sc


def _find_sofp_page(pdf) -> int | None:
    n = len(pdf.pages)
    hints = _toc_hints(pdf)
    off = _page_off(pdf)

    seen: set[int] = set()
    ordered: list[int] = []

    def push(lo: int, hi: int) -> None:
        for i in range(max(0, lo), min(n, hi)):
            if i not in seen:
                seen.add(i)
                ordered.append(i)

    for h in hints:
        idx = (h + (off or 0)) - 1
        push(idx - 6, idx + 12)

    # Broad scan capped at 500 pages (avoids hanging on 600+ page reports)
    push(0, min(n, 500))

    best_i, best_sc = None, -1
    for i in ordered:
        try:
            txt = pdf.pages[i].extract_text() or ""
        except Exception:
            continue
        sc = _score(txt)
        if sc > best_sc:
            best_sc, best_i = sc, i
        # Early exit on very strong hit near a TOC hint
        if best_sc >= 75 and hints:
            break

    return best_i if best_sc >= 28 else None


# ─────────────────────────────────────────────────────────────────────────────
# Label cleaning
# ─────────────────────────────────────────────────────────────────────────────

_BREAKS = [
    (r"\bAss\s+ets\b", "Assets"),
    (r"\bLiabilit\s+ies\b", "Liabilities"),
    (r"\bLia\s+bilities\b", "Liabilities"),
    (r"\bEquit\s+y\b", "Equity"),
    (r"\bReceivable\s+s\b", "Receivables"),
    (r"\bPayable\s+s\b", "Payables"),
    (r"\bBorrowing\s+s\b", "Borrowings"),
    (r"\bFinanci\s+al\b", "Financial"),
    (r"\bIntangibl\s+e\b", "Intangible"),
    (r"\bInstrument\s+s\b", "Instruments"),
]


def _clean_label(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    for pat, rep in _BREAKS:
        s = re.sub(pat, rep, s, flags=re.I)
    # Strip page-number / report-name bleed e.g. "... 334 | Annual Report 2025"
    s = re.sub(r"\s*\d{1,4}\s*[|·]\s*Annual\s+Report.{0,30}$", "", s, flags=re.I).strip()
    return s.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Column header inference
# ─────────────────────────────────────────────────────────────────────────────

def _infer_col_headers(raw_grid: list[list[str]], n_amt: int,
                       has_note: bool) -> list[str]:
    """Read year / entity labels from the rows above 'ASSETS'."""
    assets_i = 0
    for i, row in enumerate(raw_grid):
        if re.match(r"^\s*ASSETS\s*$", (row[0] or ""), re.I):
            assets_i = i
            break

    pre = raw_grid[:assets_i]
    if not pre:
        return [f"Col {k+1}" for k in range(n_amt)]

    n_total = len(raw_grid[assets_i]) if assets_i < len(raw_grid) else n_amt + (2 if has_note else 1)
    amt_offset = n_total - n_amt  # index of first amount column in a row

    col_parts: list[list[str]] = [[] for _ in range(n_amt)]
    for row in pre:
        for k in range(n_amt):
            idx = amt_offset + k
            if idx < len(row) and row[idx].strip():
                col_parts[k].append(row[idx].strip())

    headers: list[str] = []
    for parts in col_parts:
        seen: list[str] = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        headers.append(" ".join(seen) if seen else f"Col {len(headers)+1}")

    return headers


# ─────────────────────────────────────────────────────────────────────────────
# Row → line_item normalisation
# ─────────────────────────────────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r"^(?:notes?|rs|rs\.|rs\.'?000|group|company|bank|as\s+at|as\s+of|"
    r"restated|\d{1,4}|statement\s+of\s+financial\s+position)$",
    re.I,
)


def _normalise(row: list[str], n_amt: int, has_note: bool) -> dict | None:
    """
    Convert a raw row into a structured line_item dict, or None if noise.

    Row layout when has_note=True:  [label, note, amt1, amt2, ...]
    Row layout when has_note=False: [label, amt1, amt2, ...]
    """
    if not any(c.strip() for c in row):
        return None

    label = _clean_label(row[0])

    # Extract note ref from column 1 (if note column exists)
    note: str | None = None
    if has_note and len(row) > 1:
        candidate = (row[1] or "").strip()
        if re.match(r"^\d{1,2}(?:\.\d{1,2})?$", candidate) and not _is_year(candidate):
            note = candidate

    # Extract amounts: last n_amt cells
    # Walk from the right, skipping empty cells
    amt_cells: list[str] = []
    for cell in reversed(row):
        c = (cell or "").strip()
        if not c:
            # Blank cells in amount columns are valid (e.g. Company col has no value)
            if amt_cells or len(amt_cells) < n_amt:
                amt_cells.insert(0, "")
                if len(amt_cells) >= n_amt:
                    break
        elif _is_amount(c):
            amt_cells.insert(0, c)
            if len(amt_cells) >= n_amt:
                break
        else:
            break

    # Trim leading empty cells if we overshot
    raw_amts = amt_cells[-n_amt:] if len(amt_cells) > n_amt else amt_cells
    # Pad to n_amt if we got fewer
    while len(raw_amts) < n_amt:
        raw_amts.insert(0, "")

    # Skip rows with no label and no real amounts
    if not label and not any(_is_big_amount(a) or a in ("-", "–", "—") for a in raw_amts):
        return None

    # Skip known noise / pure header rows
    if label and _NOISE_RE.match(label):
        return None

    is_section = bool(label and _SECTION_RE.match(label))

    parsed = [_parse_num(a) for a in raw_amts]

    return {
        "label":            label,
        "is_section_header": is_section,
        "note":             note,
        "amounts_raw":      raw_amts,
        "amounts":          parsed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_sofp(
    pdf_path: str | Path,
    company_key: str,
    json_out: str | Path | None = None,
) -> dict | None:
    """
    Extract the Statement of Financial Position from an annual-report PDF.

    Parameters
    ----------
    pdf_path    : path to the PDF file
    company_key : short identifier written into the JSON (e.g. "ACME")
    json_out    : optional path to save the JSON result

    Returns
    -------
    dict  — see module docstring for full structure.
    None  — if extraction failed.
    """
    pdf_path = Path(pdf_path)
    print(f"\n[{company_key}] {pdf_path.name}")

    with pdfplumber.open(pdf_path) as pdf:
        page_idx = _find_sofp_page(pdf)
        if page_idx is None:
            print("  ERROR: SoFP page not found")
            return None
        print(f"  SoFP page: {page_idx + 1}")

        raw_grid, has_note = _extract_grid(pdf, page_idx)

    if not raw_grid:
        print("  ERROR: Table extraction failed")
        return None

    # Determine number of amount columns
    max_cols = max(len(r) for r in raw_grid)
    n_prefix = 2 if has_note else 1   # label + optional note
    n_amt = max_cols - n_prefix

    col_headers = _infer_col_headers(raw_grid, n_amt, has_note)

    line_items: list[dict] = []
    for row in raw_grid:
        item = _normalise(row, n_amt, has_note)
        if item:
            line_items.append(item)

    if not line_items:
        print("  ERROR: No line items parsed")
        return None

    note_flag = "with Note col" if has_note else "no Note col"
    print(f"  {len(line_items)} items | {n_amt} amt cols ({note_flag}): {col_headers}")

    doc = {
        "company":                    company_key,
        "source_pdf":                 str(pdf_path.resolve()),
        "extracted_at":               datetime.now().isoformat(timespec="seconds"),
        "statement_pdf_page_1based":  page_idx + 1,
        "has_note_col":               has_note,
        "column_headers":             col_headers,
        "line_items":                 line_items,
        "raw_grid":                   raw_grid,
    }

    if json_out is not None:
        jp = Path(json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        print(f"  Saved → {jp}")

    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Batch helper
# ─────────────────────────────────────────────────────────────────────────────

def run_batch(companies: list[tuple[str, Path | str]],
              json_dir: Path | str) -> None:
    """
    companies : list of (company_key, pdf_path)
    json_dir  : directory to write <key>_SoFP.json files
    """
    json_dir = Path(json_dir)
    ok = 0
    for key, pdf_path in companies:
        out = json_dir / f"{key}_SoFP.json"
        if extract_sofp(pdf_path, key, out):
            ok += 1
    print(f"\nBatch done. {ok}/{len(companies)} succeeded.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Extract Statement of Financial Position from an annual-report PDF."
    )
    ap.add_argument("pdf", type=Path, help="Path to the PDF")
    ap.add_argument("--company", "-c", default="company", help="Company key/name")
    ap.add_argument("--out", "-o", type=Path, default=None, help="Output JSON path")
    args = ap.parse_args()

    pdf = args.pdf.resolve()
    out = args.out or pdf.parent / f"{pdf.stem}_SoFP.json"
    extract_sofp(pdf, args.company, out)
