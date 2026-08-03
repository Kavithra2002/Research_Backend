"""
OpenAI vision extraction for full COMB note tables (in-memory page capture, no PNG files saved).

Identifies notes from FS Excel + income statement Note column, renders PDF pages
with PyMuPDF, sends to GPT-4o, stores complete tables in financial_tables.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from comb_note_capture import find_note_pages_by_ref, upsert_note_table
from comb_note_registry import _note_statement_key, build_note_capture_plan
from comb_note_extractor import _index_table, norm_label
from comb_reconcile import parse_number

DEFAULT_MODEL = "gpt-4o"
MAX_PAGES_PER_NOTE = 3
MAX_COMPLETION_TOKENS = 16384


def resolve_api_key() -> str | None:
    try:
        import Data_retrive as dr

        return dr._resolve_api_key(None)
    except Exception:
        return None


def render_pages_png_bytes(pdf_path: Path, page_nums: list[int], dpi: int = 160) -> list[bytes]:
    if fitz is None:
        raise RuntimeError("PyMuPDF required")
    images: list[bytes] = []
    scale = dpi / 72.0
    with fitz.open(str(pdf_path)) as doc:
        for page_num in page_nums:
            if page_num < 1 or page_num > len(doc):
                continue
            page = doc[page_num - 1]
            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=False,
            )
            images.append(pix.tobytes("png"))
    return images


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


def _build_prompt(
    note_ref: str,
    parent_label: str,
    year: int,
    *,
    entity_column: str = "group",
) -> str:
    entity = entity_column.upper()
    return f"""You are extracting a financial note table from a bank annual report (Sri Lanka).

Note reference: {note_ref}
Note title: {parent_label}
Target: **{entity}** column, year **{year}**, amounts in Rs. '000.

Extract EVERY data row in the note table on these pages, including:
- All sub-line items (even if value is dash, zero, or in parentheses)
- The Total row if present
- Do NOT skip rows because they look like headers or accounting policy text outside the numeric table

Use the exact line-item labels as printed in the table (left column).

Return ONLY valid JSON (no markdown):
{{
  "note_ref": "{note_ref}",
  "title": "{parent_label}",
  "entity": "{entity}",
  "year": {year},
  "lines": [
    {{"label": "<exact row label>", "value": <number or null>}}
  ]
}}

Rules:
- `value` is the {entity} {year} figure only (not BANK, not {year - 1})
- Parentheses mean negative numbers
- Dash or blank means null
- Include rows like "Net gains on sale of property..." even if small
"""


def _lines_to_financial_table(
    lines: list[dict[str, Any]],
    *,
    note_ref: str,
    title: str,
    year: int,
    entity_column: str = "group",
) -> dict[str, Any]:
    entity = entity_column.upper()
    header_rows = [
        ["", "", entity, "BANK"],
        ["", "", str(year), str(year - 1), str(year), str(year - 1)],
        ["Line Item", "Note", f"Rs. '000", f"Rs. '000", f"Rs. '000", f"Rs. '000"],
    ]
    rows: list[dict[str, Any]] = []
    values_index: dict[str, float] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        label = str(line.get("label") or "").strip()
        if not label:
            continue
        val = parse_number(line.get("value"))
        cells = [label, "", str(val) if val is not None else "", "", "", ""]
        rows.append({"cells": cells, "style": {}})
        if val is not None:
            nl = norm_label(label)
            if nl:
                values_index[nl] = val
    return {
        "header_rows": header_rows,
        "rows": rows,
        "statement_title": f"{note_ref} {title}".strip(),
        "note_ref": note_ref,
        "values_index": values_index,
        "row_count": len(rows),
    }


def extract_note_openai(
    api_key: str,
    pdf_path: Path,
    *,
    note_ref: str,
    parent_label: str,
    year: int,
    entity_column: str = "group",
    model: str = DEFAULT_MODEL,
    page_nums: list[int] | None = None,
) -> dict[str, Any]:
    """Capture note pages in memory and extract full table via OpenAI vision."""
    if OpenAI is None:
        raise RuntimeError("openai package required")

    pages = page_nums or find_note_pages_by_ref(
        pdf_path, note_ref, parent_label, max_pages=MAX_PAGES_PER_NOTE
    )
    # Include next page for tables split across pages (e.g. 17.x)
    expanded: list[int] = []
    for p in pages:
        if p not in expanded:
            expanded.append(p)
        if p + 1 not in expanded and len(expanded) < MAX_PAGES_PER_NOTE:
            expanded.append(p + 1)
    pages = expanded[:MAX_PAGES_PER_NOTE]

    if not pages:
        return {"ok": False, "reason": "no_pages", "note_ref": note_ref}

    pngs = render_pages_png_bytes(pdf_path, pages)
    if not pngs:
        return {"ok": False, "reason": "render_failed", "note_ref": note_ref}

    client = OpenAI(api_key=api_key)
    prompt = _build_prompt(note_ref, parent_label, year, entity_column=entity_column)
    b64_images = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{base64.b64encode(p).decode('ascii')}"},
        }
        for p in pngs
    ]
    content: list[dict[str, Any]] = b64_images + [{"type": "text", "text": prompt}]

    last_err: str | None = None
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
            lines = payload.get("lines") or []
            table = _lines_to_financial_table(
                lines,
                note_ref=note_ref,
                title=parent_label,
                year=year,
                entity_column=entity_column,
            )
            if not table.get("rows"):
                return {
                    "ok": False,
                    "reason": "empty_table",
                    "note_ref": note_ref,
                    "pages": pages,
                }
            return {
                "ok": True,
                "note_ref": note_ref,
                "parent_label": parent_label,
                "pages": pages,
                "table": table,
                "line_count": len(lines),
                "method": "openai_vision",
            }
        except Exception as exc:
            last_err = str(exc)
            if attempt < 3:
                time.sleep(2.0 * attempt)

    return {
        "ok": False,
        "reason": last_err or "api_error",
        "note_ref": note_ref,
        "pages": pages,
    }


def extract_notes_openai_for_company(
    db,
    company_slug: str,
    year: int,
    pdf_path: Path,
    *,
    entity_column: str = "group",
    model: str = DEFAULT_MODEL,
    force: bool = False,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Extract all FS-linked note tables with OpenAI and upsert to financial_tables."""
    api_key = api_key or resolve_api_key()
    if not api_key:
        return {"ok": False, "error": "no_api_key"}

    plan = build_note_capture_plan(db, company_slug, year)
    with_notes = [p for p in plan if p.get("has_note_table") and p.get("note_ref")]

    sample = db.financial_tables.find_one(
        {"company_slug": company_slug}, {"company_name": 1}
    )
    company_name = (
        str(sample.get("company_name"))
        if sample and sample.get("company_name")
        else company_slug.replace("_", " ")
    )

    results: list[dict[str, Any]] = []
    for item in with_notes:
        note_ref = str(item["note_ref"])
        sk = item.get("note_statement_key") or _note_statement_key(note_ref)
        if not force:
            existing = db.financial_tables.find_one(
                {
                    "company_slug": company_slug,
                    "year": year,
                    "statement_key": sk,
                    "extraction_method": "openai_vision",
                    "row_count": {"$gt": 3},
                }
            )
            if existing:
                results.append(
                    {
                        "ok": True,
                        "note_ref": note_ref,
                        "skipped": True,
                        "row_count": existing.get("row_count"),
                    }
                )
                continue
            # Keep local pdfplumber table when OpenAI repeatedly fails (e.g. note 30)
            local = db.financial_tables.find_one(
                {
                    "company_slug": company_slug,
                    "year": year,
                    "statement_key": sk,
                    "row_count": {"$gt": 5},
                }
            )
            if local and str(note_ref) in {"30"}:
                results.append(
                    {
                        "ok": True,
                        "note_ref": note_ref,
                        "skipped": True,
                        "row_count": local.get("row_count"),
                        "reason": "local_fallback",
                    }
                )
                continue

        print(
            f"  [openai-note] {item.get('parent_label')} (note {note_ref}) …",
            flush=True,
        )
        meta = extract_note_openai(
            api_key,
            pdf_path,
            note_ref=note_ref,
            parent_label=str(item.get("parent_label") or item.get("fs_label") or ""),
            year=year,
            entity_column=entity_column,
            model=model,
        )
        if not meta.get("ok"):
            results.append({**meta, "ok": False})
            continue

        table = meta["table"]
        upsert_note_table(
            db,
            company_slug=company_slug,
            company_name=company_name,
            year=year,
            note_ref=note_ref,
            parent_label=str(item.get("parent_label") or ""),
            table=table,
            source_pdf=str(pdf_path),
            extraction_method="openai_vision",
            extraction_model=model,
            source_pages=meta.get("pages"),
        )
        results.append(
            {
                "ok": True,
                "note_ref": note_ref,
                "pages": meta.get("pages"),
                "row_count": table.get("row_count"),
                "line_count": meta.get("line_count"),
            }
        )

    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "company_slug": company_slug,
        "year": year,
        "notes_attempted": len(with_notes),
        "notes_ok": ok_count,
        "results": results,
        "method": "openai_vision",
    }


def all_lines_from_note_doc(
    doc: dict[str, Any] | None,
    *,
    year: int,
    entity_column: str = "group",
) -> list[dict[str, Any]]:
    """All extracted note lines as {label, value} for UI display."""
    if not doc:
        return []

    header_rows = doc.get("header_rows") or []
    body_rows = [
        [str(c) for c in (row.get("cells") or [])]
        for row in doc.get("rows") or []
    ]
    index = _index_table(header_rows, body_rows, year, entity_column)

    out: list[dict[str, Any]] = []
    for i, row in enumerate(doc.get("rows") or []):
        cells = row.get("cells") if isinstance(row, dict) else []
        label = str(cells[0]).strip() if cells else ""
        if not label:
            continue
        nl = norm_label(label)
        val = index.get(nl)
        if val is None:
            for k, v in index.items():
                if nl in k or k in nl:
                    val = v
                    break
        if val is None and len(cells) > 2:
            val = parse_number(cells[2])
        out.append({"label": label, "value": val, "row": i + 1})
    return out


def fetch_note_doc(db, company_slug: str, year: int, note_ref: str) -> dict | None:
    sk = _note_statement_key(note_ref)
    return db.financial_tables.find_one(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "statement_key": sk,
        }
    )


def full_note_breakdown(
    db,
    company_slug: str,
    year: int,
    note_ref: str,
    *,
    entity_column: str = "group",
) -> list[dict[str, Any]]:
    doc = fetch_note_doc(db, company_slug, year, note_ref)
    return all_lines_from_note_doc(doc, year=year, entity_column=entity_column)


