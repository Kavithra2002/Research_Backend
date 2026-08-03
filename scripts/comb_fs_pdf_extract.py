"""
Description-first FS extraction from annual report PDFs.

Locates statement pages (income statement, SoFP, cash flows) via pdfplumber,
builds a label→value index for GROUP + year, falls back to OpenAI vision
when the local grid parse is too sparse. Used for COMB FS sheet population.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from comb_note_extractor import (
    _entity_year_col,
    _index_table,
    _split_header_body,
    _words_table_rows,
    map_labels_to_values,
    norm_label,
)
from comb_note_openai import DEFAULT_MODEL, MAX_COMPLETION_TOKENS, resolve_api_key
from comb_reconcile import parse_number
from generate_comb_model import LABEL_ALIASES, norm_label as g_norm_label
from extraction_aliases_store import merge_alias_map

STATEMENT_CONFIG: dict[str, dict[str, Any]] = {
    "income_statement": {
        "title_terms": [
            "statement of profit or loss",
            "income statement",
            "statement of comprehensive income",
        ],
        "page_boost": ["total operating income", "profit for the year"],
    },
    "sofp": {
        "title_terms": [
            "statement of financial position",
            "balance sheet",
        ],
        "page_boost": [
            "total assets",
            "total equity",
            "memorandum information",
            "number of employees",
        ],
    },
    "cash_flows": {
        "title_terms": [
            "statement of cash flows",
            "cash flow statement",
        ],
        "page_boost": [
            "cash flows from operating",
            "net increase",
            "cash and cash equivalents as at",
        ],
    },
    "oci": {
        "title_terms": ["statement of other comprehensive income", "other comprehensive income"],
        "page_boost": [],
    },
}

CASH_FLOW_ALIASES: dict[str, list[str]] = {
    "Gross cash and cash equivalents as at December 31,": [
        "gross cash and cash equivalents as at december 31",
        "gross cash and cash equivalents",
    ],
    "Less: Impairment charges on cash and cash equivalents": [
        "less impairment charges on cash and cash equivalents",
        "impairment charges on cash and cash equivalents",
    ],
    "Cash and cash equivalents as per Statement of Financial Position": [
        "cash and cash equivalents as per statement of financial position",
        "cash and cash equivalents as per the statement of financial position",
    ],
    "Cash and cash equivalents as at January 01,": [
        "cash and cash equivalents as at january 01",
        "cash and cash equivalents as at 1 january",
        "cash and cash equivalents as at january 1",
    ],
    "Dividend paid to shareholders": ["dividend paid to shareholders", "dividends paid to shareholders"],
    "Dividend paid to non-controlling interest": [
        "dividend paid to non-controlling interest",
        "dividends paid to non-controlling interest",
    ],
    "Net increase/(decrease) in cash and cash equivalents": [
        "net increase/(decrease) in cash and cash equivalents",
        "net increase in cash and cash equivalents",
        "net decrease in cash and cash equivalents",
    ],
    "Cash flows from operating activities Profit before income tax": [
        "profit before income tax",
        "cash flows from operating activities",
    ],
    "Payment of lease liabilities/advance payment of right-of-use assets": [
        "payment of lease liabilities",
        "advance payment of right of use assets",
    ],
}


def build_fs_section_map(manifest: dict) -> dict[str, str]:
    """Map each FS data label -> statement section key."""
    section = "income_statement"
    out: dict[str, str] = {}
    for row in manifest.get("fs", {}).get("rows") or []:
        kind = row.get("kind")
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        if kind == "section":
            lu = label.upper()
            if "CASH FLOW" in lu:
                section = "cash_flows"
            elif "BALANCE" in lu or "FINANCIAL POSITION" in lu:
                section = "sofp"
            elif "OCI" in lu:
                section = "oci"
            elif "INCOME" in lu:
                section = "income_statement"
            continue
        if kind == "data":
            out[label] = section
    return out


def _search_terms_for_label(label: str) -> list[str]:
    merged = merge_alias_map(LABEL_ALIASES, "fs")
    terms = list(merged.get(label, []))
    terms.extend(CASH_FLOW_ALIASES.get(label, []))
    terms.append(label)
    cleaned = []
    for t in terms:
        t = re.sub(r"\s+", " ", (t or "").strip())
        if t and t not in cleaned:
            cleaned.append(t)
    return cleaned


# Shared fitz text cache — used by statement page finding for all sections.
_FITZ_TEXT_CACHE: dict[str, list[str]] = {}


def _fitz_texts_cached(pdf_path: Path) -> list[str]:
    try:
        st = pdf_path.stat()
        key = f"{pdf_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = str(pdf_path)
    cached = _FITZ_TEXT_CACHE.get(key)
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
    _FITZ_TEXT_CACHE[key] = texts
    if len(_FITZ_TEXT_CACHE) > 4:
        for old in list(_FITZ_TEXT_CACHE.keys())[:-2]:
            _FITZ_TEXT_CACHE.pop(old, None)
    return texts


def find_statement_pages(
    pdf_path: Path,
    statement_key: str,
    *,
    max_pages: int = 6,
) -> list[int]:
    if fitz is None:
        return []
    cfg = STATEMENT_CONFIG.get(statement_key, {})
    title_terms = [t.lower() for t in cfg.get("title_terms") or []]
    boost_terms = [t.lower() for t in cfg.get("page_boost") or []]
    hits: list[tuple[int, int]] = []

    texts = _fitz_texts_cached(pdf_path)
    for i, raw in enumerate(texts):
        text = raw.lower()
        score = 0
        for t in title_terms:
            if t in text:
                score += 20
        for t in boost_terms:
            if t in text:
                score += 6
        if statement_key == "cash_flows":
            if "gross cash and cash equivalents as at december" in text:
                score += 40
            if "cash flows from operating activities" in text:
                score += 15
            if "group" in text and "bank" in text:
                score += 8
        elif statement_key == "income_statement":
            if "gross income" in text and "page no" in text:
                score += 45
            if "interest income" in text and "group" in text:
                score += 25
            if "total operating income" in text:
                score += 15
            if "statement of profit or loss" in text:
                score += 20
            if "total assets" in text:
                score -= 50
            if "group" in text and "bank" in text:
                score += 5
        elif statement_key == "sofp":
            if "statement of financial position" in text:
                score += 50
            if "memorandum information" in text:
                score += 35
            if "number of employees" in text:
                score += 25
            if "total liabilities and equity" in text:
                score += 10
            # Multi-year highlight / financial-highlight spreads (not the SoFP).
            year_hits = len(re.findall(r"\b20\d{2}\b", text))
            if year_hits >= 6 and "statement of financial position" not in text:
                score -= 40
            if "financial highlights" in text or "five year" in text or "5 year" in text:
                score -= 30
        if text.count("statement of") > 3 and score < 30:
            score -= 15
        if score:
            hits.append((score, i + 1))

    hits.sort(reverse=True)
    pages = [p for _, p in hits[:max_pages]]
    # Include following pages for multi-page statements (P&L often runs
    # onto a 3rd page with EPS / OCI continuation lines).
    expand_extra = 2 if statement_key in {"income_statement", "oci"} else 1
    expanded: list[int] = []
    for p in pages:
        if p not in expanded:
            expanded.append(p)
        for delta in range(1, expand_extra + 1):
            nxt = p + delta
            if nxt not in expanded and len(expanded) < max_pages:
                expanded.append(nxt)
    return expanded[:max_pages]


def find_memorandum_pages(pdf_path: Path) -> list[int]:
    """Pages containing the SoFP memorandum block (employees, service centres)."""
    if fitz is None:
        return []
    pages: list[int] = []
    for i, raw in enumerate(_fitz_texts_cached(pdf_path)):
        text = raw.lower()
        if "memorandum information" in text and (
            "number of employees" in text
            or "customer service centre" in text
        ):
            pages.append(i + 1)
    return pages


def _statement_pages_with_memorandum(
    pdf_path: Path,
    statement_key: str,
    *,
    max_pages: int = 8,
) -> list[int]:
    pages = find_statement_pages(pdf_path, statement_key, max_pages=max_pages)
    if statement_key != "sofp":
        return pages
    for p in find_memorandum_pages(pdf_path):
        if p not in pages:
            pages.append(p)
    return pages[: max(max_pages, len(pages))]


def _plumber_index_from_pages(
    pdf_path: Path,
    page_nums: list[int],
    year: int,
    entity_column: str,
) -> dict[str, float]:
    if pdfplumber is None or not page_nums:
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
    return _index_table(header_rows, body_rows, year, entity_column)


def _index_quality(statement_key: str, index: dict[str, float]) -> int:
    score = len(index)
    anchors = {
        "income_statement": [
            "interest income",
            "total operating income",
            "profit for the year",
            "gross income",
            "net interest income",
        ],
        "sofp": ["total assets", "total equity", "deferred tax"],
        "cash_flows": [
            "gross cash and cash equivalents",
            "cash and cash equivalents as per statement",
            "net cash from",
        ],
        "oci": ["other comprehensive income"],
    }
    required = {
        "income_statement": ["interest income", "gross income"],
        "sofp": ["total assets"],
        "cash_flows": ["gross cash and cash equivalents"],
    }
    for term in anchors.get(statement_key, []):
        if any(term in k for k in index):
            score += 30
    # Prefer complete main IS pages over sparse highlight tables.
    if statement_key == "income_statement":
        if any("total operating income" in k for k in index):
            score += 80
        if any("profit for the year" in k for k in index):
            score += 40
        if len(index) >= 20:
            score += 25
    if statement_key == "sofp":
        ta = abs(float(index.get("total assets") or 0))
        # Primary SoFP is LKR '000 (billions). Multi-year highlight / USD
        # tables are much smaller or pick the wrong year column.
        if ta >= 100_000_000:
            score += 120
        elif ta >= 1_000_000:
            score += 20
        else:
            score -= 60
        if any("total liabilities and equity" in k for k in index):
            score += 40
        if any("cash and cash equivalents" in k for k in index):
            score += 20
    for term in required.get(statement_key, []):
        if not any(term in k for k in index):
            score -= 80
    return score


def _best_plumber_index(
    pdf_path: Path,
    page_nums: list[int],
    statement_key: str,
    year: int,
    entity_column: str,
) -> dict[str, float]:
    if not page_nums or pdfplumber is None:
        return {}

    # Extract each candidate page once in a single pdfplumber session.
    unique_pages = sorted({int(p) for p in page_nums if isinstance(p, int) and p > 0})
    # Also prepare +1/+2 for multi-page statements without reopening.
    expand = set(unique_pages)
    for p in unique_pages[:6]:
        expand.add(p + 1)
        expand.add(p + 2)
    page_cache: dict[int, list[list[str]]] = {}
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num in sorted(expand):
                if page_num < 1 or page_num > len(pdf.pages):
                    page_cache[page_num] = []
                    continue
                page_cache[page_num] = _words_table_rows(pdf.pages[page_num - 1]) or []
    except Exception:
        return {}

    def _index_pages(pages: list[int]) -> dict[str, float]:
        combined: list[list[str]] = []
        for page_num in pages:
            combined.extend(page_cache.get(page_num) or [])
        if not combined:
            return {}
        header_rows, body_rows = _split_header_body(combined)
        return _index_table(header_rows, body_rows, year, entity_column)

    # Rank start pages by single-page quality (when present).
    single: list[tuple[int, int, dict[str, float]]] = []
    for page in unique_pages:
        idx = _index_pages([page])
        if idx:
            single.append((_index_quality(statement_key, idx), page, idx))
    single.sort(reverse=True)

    candidates: list[tuple[int, dict[str, float]]] = [(q, idx) for q, _, idx in single]

    # Always expand the original statement page hits — many IS tables start on a
    # page whose solo parse is empty/header-only (header continues on page+1).
    start_pages = list(dict.fromkeys(
        [p for _, p, _ in single[:4]] + unique_pages[:4]
    ))
    for page in start_pages:
        for span in ([page], [page, page + 1], [page, page + 1, page + 2]):
            idx = _index_pages(span)
            if not idx:
                continue
            score = _index_quality(statement_key, idx)
            candidates.append((score, idx))
            # Early exit for a clearly complete income statement.
            if (
                statement_key == "income_statement"
                and any("total operating income" in k for k in idx)
                and (idx.get("gross income") or 0) > 1_000_000
                and any("profit for the year" in k for k in idx)
            ):
                return idx

    if not candidates:
        return {}
    if statement_key == "sofp":
        # Prefer the primary Statement of Financial Position (LKR '000).
        # Highlight / multi-year pages often score well but pick the wrong year
        # or BANK column — for GROUP keep the largest plausible total assets.
        with_ta = [
            (q, idx)
            for q, idx in candidates
            if abs(float(idx.get("total assets") or 0)) >= 100_000_000
        ]
        if with_ta:
            entity = (entity_column or "group").lower()
            if entity == "bank":
                # Prefer a true BANK column parse (typically below GROUP totals).
                with_ta.sort(
                    key=lambda x: (
                        x[0],
                        -abs(float(x[1].get("total assets") or 0)),
                        len(x[1]),
                    ),
                    reverse=True,
                )
            else:
                # GROUP current-year is the largest SoFP total among candidates;
                # quality score alone can prefer wrong-year highlight tables.
                with_ta.sort(
                    key=lambda x: (
                        abs(float(x[1].get("total assets") or 0)),
                        x[0],
                        len(x[1]),
                    ),
                    reverse=True,
                )
            return with_ta[0][1]
    if statement_key == "income_statement":
        with_total = [
            (q, idx)
            for q, idx in candidates
            if any("total operating income" in k for k in idx)
            and (idx.get("gross income") or 0) > 1_000_000
        ]
        if with_total:
            with_total.sort(
                key=lambda x: (
                    x[0],
                    1 if any("profit for the year" in k for k in x[1]) else 0,
                    x[1].get("gross income", 0),
                ),
                reverse=True,
            )
            return with_total[0][1]
        with_gross = [
            (q, idx)
            for q, idx in candidates
            if (idx.get("gross income") or 0) > 1_000_000
        ]
        if with_gross:
            with_gross.sort(
                key=lambda x: (x[0], len(x[1]), x[1].get("gross income", 0)),
                reverse=True,
            )
            return with_gross[0][1]
        with_interest = [
            (q, idx) for q, idx in candidates if idx.get("interest income") is not None
        ]
        if with_interest:
            with_interest.sort(
                key=lambda x: (x[0], abs(x[1]["interest income"])),
                reverse=True,
            )
            return with_interest[0][1]
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _parse_json_content(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _openai_statement_index(
    api_key: str,
    pdf_path: Path,
    page_nums: list[int],
    *,
    statement_key: str,
    year: int,
    entity_column: str = "group",
    model: str = DEFAULT_MODEL,
) -> dict[str, float]:
    if OpenAI is None or fitz is None or not page_nums:
        return {}

    scale = 160 / 72.0
    images: list[bytes] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_num in page_nums[:3]:
            if page_num < 1 or page_num > len(doc):
                continue
            pix = doc[page_num - 1].get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=False,
            )
            images.append(pix.tobytes("png"))
    if not images:
        return {}

    entity = entity_column.upper()
    prompt = f"""Extract every line item from this bank annual report **{statement_key.replace('_', ' ')}** table.

Target column: **{entity}** year **{year}** only (Rs. '000).
Ignore BANK column and prior years.

Return ONLY JSON:
{{
  "lines": [{{"label": "<exact description text>", "value": <number or null>}}]
}}

Rules:
- Parentheses = negative numbers; dash/blank = null
- Include subtotals and section lines that have numeric values
- Use labels exactly as printed in the left description column
"""

    client = OpenAI(api_key=api_key)
    b64_images = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{base64.b64encode(p).decode('ascii')}"},
        }
        for p in images
    ]
    content: list[dict[str, Any]] = b64_images + [{"type": "text", "text": prompt}]

    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
                max_tokens=MAX_COMPLETION_TOKENS,
                response_format={"type": "json_object"},
            )
            payload = _parse_json_content(resp.choices[0].message.content or "{}")
            index: dict[str, float] = {}
            for line in payload.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                label = str(line.get("label") or "").strip()
                val = parse_number(line.get("value"))
                if label and val is not None:
                    nl = norm_label(label)
                    if nl:
                        index[nl] = val
            return index
        except Exception:
            if attempt < 3:
                time.sleep(2.0 * attempt)
    return {}


@dataclass
class FsPdfIndex:
    """Merged label index from PDF statement extraction."""

    by_section: dict[str, dict[str, float]] = field(default_factory=dict)
    by_section_bank: dict[str, dict[str, float]] = field(default_factory=dict)
    section_map: dict[str, str] = field(default_factory=dict)
    pages_by_section: dict[str, list[int]] = field(default_factory=dict)

    def lookup(
        self,
        label: str,
        *,
        section: str | None = None,
        entity_column: str = "group",
    ) -> float | None:
        index_source = (
            self.by_section
            if entity_column.lower() != "bank"
            else self.by_section_bank
        )
        sections = [section] if section else list(index_source.keys())
        terms = _search_terms_for_label(label)
        term_norms = [g_norm_label(t) for t in terms]

        for sk in sections:
            index = index_source.get(sk) or {}
            if not index:
                continue

            for nt in term_norms:
                if nt in index:
                    return index[nt]

            best_val: float | None = None
            best_ratio = 0.0
            best_key_len = -1
            label_nl = g_norm_label(label)
            per_share = "per share" in label_nl or "earnings per" in label_nl
            for nt in term_norms:
                if len(nt) < 10:
                    continue
                for k, v in index.items():
                    if per_share and (
                        "weighted average" in k
                        or "number of ordinary shares" in k
                        or "number of shares" in k
                        or abs(float(v)) >= 1_000
                    ):
                        continue
                    if k == nt:
                        return v
                    if nt in k:
                        ratio = len(nt) / max(len(k), 1)
                    elif k in nt:
                        ratio = len(k) / max(len(nt), 1)
                    else:
                        continue
                    if ratio >= 0.62 and (
                        ratio > best_ratio
                        or (ratio == best_ratio and len(k) > best_key_len)
                    ):
                        best_ratio = ratio
                        best_val = v
                        best_key_len = len(k)
            if best_val is not None:
                return best_val

            mapped = map_labels_to_values(index, terms, min_score=0.55)
            for term in terms:
                v = mapped.get(term)
                if v is None:
                    continue
                if per_share and abs(float(v)) >= 1_000:
                    continue
                return v
        return None
    def label_likely_in_report(self, label: str, *, section: str | None = None) -> bool:
        sk = section or self.section_map.get(label)
        index = self.by_section.get(sk or "") or {}
        terms = [g_norm_label(t) for t in _search_terms_for_label(label)]
        for t in terms:
            if t in index:
                return True
            for k in index:
                if t in k or k in t:
                    return True
        return False


def build_fs_pdf_index(
    pdf_path: Path,
    manifest: dict,
    year: int,
    *,
    entity_column: str = "group",
    use_openai: bool = True,
    api_key: str | None = None,
) -> FsPdfIndex:
    """Build per-statement indexes from PDF (pdfplumber, then OpenAI if sparse)."""
    section_map = build_fs_section_map(manifest)
    result = FsPdfIndex(section_map=section_map)
    api_key = api_key or (resolve_api_key() if use_openai else None)

    for statement_key in ("income_statement", "sofp", "cash_flows", "oci"):
        max_p = 8 if statement_key in {"sofp", "income_statement", "oci"} else 6
        # Do not mix multi-year highlight / memorandum pages into SoFP ranking.
        pages = find_statement_pages(pdf_path, statement_key, max_pages=max_p)
        memo_pages: list[int] = (
            find_memorandum_pages(pdf_path) if statement_key == "sofp" else []
        )
        if not pages and not memo_pages:
            continue
        index = _best_plumber_index(
            pdf_path, pages or memo_pages, statement_key, year, entity_column
        )
        needs_openai = len(index) < 8 or _index_quality(statement_key, index) < 20
        if needs_openai and use_openai and api_key:
            print(
                f"  [pdf-fs] {statement_key}: plumber {len(index)} rows → OpenAI …",
                flush=True,
            )
            oai_index = _openai_statement_index(
                api_key,
                pdf_path,
                pages or memo_pages,
                statement_key=statement_key,
                year=year,
                entity_column=entity_column,
            )
            index = {**index, **oai_index}
        else:
            print(
                f"  [pdf-fs] {statement_key}: {len(index)} rows from pdfplumber "
                f"(pages {(pages or memo_pages)[:3]})",
                flush=True,
            )
        if index:
            from comb_annual_memorandum import merge_memorandum_into_pdf_index

            if memo_pages:
                index = merge_memorandum_into_pdf_index(
                    index, pdf_path, memo_pages, year, entity_column
                )
            result.by_section[statement_key] = index
            result.pages_by_section[statement_key] = list(
                dict.fromkeys([*(pages or []), *memo_pages])
            )
        if statement_key == "sofp":
            bank_index = _best_plumber_index(
                pdf_path, pages or memo_pages, statement_key, year, "bank"
            )
            if bank_index:
                from comb_annual_memorandum import merge_memorandum_into_pdf_index

                if memo_pages:
                    bank_index = merge_memorandum_into_pdf_index(
                        bank_index, pdf_path, memo_pages, year, "bank"
                    )
                result.by_section_bank[statement_key] = bank_index

    return result


def label_absent_in_pdf(
    pdf_path: Path,
    label: str,
    *,
    section: str | None = None,
    pdf_index: FsPdfIndex | None = None,
) -> bool:
    """
    True when the description does not appear in the report (confirmed absent).
  False when label text exists but value could not be parsed.
    """
    if pdf_index and pdf_index.label_likely_in_report(label, section=section):
        return False

    if fitz is None:
        return False

    terms = [t.lower() for t in _search_terms_for_label(label) if len(t) >= 8]
    if not terms:
        terms = [label.lower()[:40]]

    with fitz.open(str(pdf_path)) as doc:
        page_range = range(len(doc))
        if section:
            stmt_pages = find_statement_pages(pdf_path, section, max_pages=8)
            if stmt_pages:
                page_range = [p - 1 for p in stmt_pages]

        for i in page_range:
            text = (doc[i].get_text() or "").lower()
            for t in terms:
                # Require a substantial substring match
                core = re.sub(r"[^a-z0-9]+", " ", t).strip()
                if len(core) >= 10 and core in re.sub(r"[^a-z0-9]+", " ", text):
                    return False
                if t in text:
                    return False
    return True
