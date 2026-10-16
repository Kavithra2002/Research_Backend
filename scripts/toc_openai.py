"""
toc_openai.py
=============
Use OpenAI vision on Table-of-Contents page image(s) to read the **exact
printed page numbers** for the main financial statements.

Flow
----
1. Cheap local find of TOC page(s) (front Contents + FS mini-TOC).
2. Render those pages to PNG.
3. Ask OpenAI: return JSON page numbers for income / OCI / SoFP / cash flows.
4. Caller jumps straight to those pages for capture + extract.

Accuracy over brittle text parsing of two-column TOCs.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

CORE_TOC_KEYS = (
    "income_statement",
    "oci",
    "sofp",
    "cash_flows",
)

_TOC_PROMPT = """
You are reading the Table of Contents (TOC) page(s) from a Sri Lankan listed
company annual report PDF.

Task: find the PRINTED page number for each financial statement below.
Use the Financial Statements TOC when present (often titled
"Financial statements – Table of contents" or "B – FINANCIAL STATEMENTS").

Return ONLY valid JSON (no markdown), exactly this shape:
{
  "income_statement": <int or null>,
  "oci": <int or null>,
  "sofp": <int or null>,
  "cash_flows": <int or null>,
  "notes": "short note if unsure"
}

Mapping (permanent aliases — same for every company):
- income_statement = "Income Statement" OR "Statement of Profit or Loss"
  OR "Statement of Profit and Loss" (the P&L table — NOT notes, NOT OCI)
- oci = "Statement of Comprehensive Income"
  OR "Statement of Other Comprehensive Income"
  OR "Statement of Profit or Loss and Other Comprehensive Income"
  OR "Other Comprehensive Income"
- sofp = "Statement of Financial Position" OR "Balance Sheet"
- cash_flows = "Statement of Cash Flows" OR "Cash Flow Statement"

Rules:
- Page numbers are the integers printed in the TOC next to each title
  (e.g. Cash Flows → 318).
- Do NOT invent pages. Use null if not listed.
- Ignore "Page No." columns inside financial tables — you are reading the TOC only.
- Prefer the financial-statements TOC over the front-of-book contents when both appear.
""".strip()


def _locate_toc_indices(pdf) -> list[int]:
    """Cheap local locate of TOC page indices (not number parsing)."""
    import step1_find_pages as step1

    n = len(pdf.pages)
    found: list[int] = []
    seen: set[int] = set()

    def _add(i: int) -> None:
        if 0 <= i < n and i not in seen:
            seen.add(i)
            found.append(i)

    # Front-matter contents (first ~40 pages).
    for i in range(min(n, 40)):
        txt = step1._safe_text(pdf.pages[i])
        if step1._looks_like_toc(txt):
            _add(i)

    # Mid-report FS TOC — seed from any front TOC numbers, else probe band.
    front_toc = step1.scan_toc(pdf, scan_limit=40)
    for i in step1._find_financial_statements_toc_pages(pdf, front_toc):
        _add(i)

    # Always keep at most 3 TOC pages (cost control).
    return found[:3]


def _render_page_b64(pdf_path: Path, page_idx: int, dpi: int = 120) -> str | None:
    try:
        import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(str(pdf_path))
        try:
            if page_idx < 0 or page_idx >= len(doc):
                return None
            page = doc[page_idx]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            return base64.b64encode(pix.tobytes("png")).decode()
        finally:
            doc.close()
    except Exception:
        return None


def _parse_toc_json(raw: str) -> dict[str, int]:
    import step3_send_to_openai as step3

    parsed = step3.extract_json(raw)
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for key in CORE_TOC_KEYS:
        val = parsed.get(key)
        if val is None or val == "":
            continue
        try:
            pg = int(val)
        except (TypeError, ValueError):
            m = re.search(r"\d{1,4}", str(val))
            if not m:
                continue
            pg = int(m.group(0))
        if 1 <= pg <= 5000 and not (1900 <= pg <= 2100):
            out[key] = pg
    return out


def openai_read_toc_page_numbers(
    pdf_path: str | Path,
    pdf,
    *,
    api_key: str,
    model: str = "gpt-5",
    dpi: int = 120,
    log: Any = None,
) -> dict[str, list[int]]:
    """
    Return ``{statement_key: [printed_page, ...]}`` from OpenAI vision on TOC pages.
    Empty dict on failure.
    """
    _log = log or (lambda msg, level="info": print(f"  [toc-openai] {msg}"))
    pdf_path = Path(pdf_path)

    indices = _locate_toc_indices(pdf)
    if not indices:
        _log("no TOC candidate pages found", "warn")
        return {}

    images: list[str] = []
    used_idx: list[int] = []
    for i in indices:
        b64 = _render_page_b64(pdf_path, i, dpi=dpi)
        if b64:
            images.append(b64)
            used_idx.append(i)
    if not images:
        _log("could not render TOC pages", "warn")
        return {}

    _log(
        f"reading TOC via OpenAI ({model}) from pdf page(s) "
        f"{', '.join(str(i + 1) for i in used_idx)}"
    )

    try:
        from openai import OpenAI
        import step3_send_to_openai as step3

        client = OpenAI(api_key=api_key)
        raw = step3.call_gpt4o(
            client,
            images,
            _TOC_PROMPT,
            model=model,
            retries=2,
            max_tokens=2048,
        )
    except Exception as exc:
        _log(f"OpenAI TOC read failed: {exc}", "warn")
        return {}

    parsed = _parse_toc_json(raw or "")
    if not parsed:
        _log("OpenAI TOC returned no usable page numbers", "warn")
        return {}

    as_lists = {k: [v] for k, v in parsed.items()}
    _log(
        "TOC page numbers: "
        + ", ".join(f"{k}={v[0]}" for k, v in as_lists.items())
    )
    return as_lists
