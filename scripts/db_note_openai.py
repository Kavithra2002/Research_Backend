"""
db_note_openai.py
=================
Capture note pages from the annual PDF and extract note tables with OpenAI
vision into the printed_statements note shape (Group/Company × years).

Used by Run DB Annual after main FS are loaded from Extracted Tables.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

try:
    import fitz
except ImportError:
    fitz = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

LogFn = Callable[[str, str], None]


def _log(log: LogFn | None, msg: str, level: str = "info") -> None:
    if log:
        log(msg, level)
    else:
        print(msg)


def _resolve_api_key(api_key: str | None) -> str | None:
    if api_key and api_key.strip():
        return api_key.strip()
    try:
        import extraction_accuracy as acc

        return acc.resolve_api_key(None)
    except Exception:
        return None


def _openai_token_kwargs(model: str, max_tokens: int = 12000) -> dict[str, Any]:
    if str(model or "").lower().startswith("gpt-5"):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens, "temperature": 0}


def _parse_json(content: str) -> dict[str, Any]:
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


def _label_for_ref(statements: list[dict[str, Any]], ref: str) -> str:
    for stmt in statements:
        for row in stmt.get("rows") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("note_ref") or "").strip() == ref:
                cells = row.get("cells") or []
                if cells:
                    return str(cells[0] or "").strip() or f"Note {ref}"
            cells = row.get("cells") or []
            if len(cells) > 1 and str(cells[1]).strip() == ref:
                return str(cells[0] or "").strip() or f"Note {ref}"
    return f"Note {ref}"


def _find_pages(
    pdf_path: Path,
    ref: str,
    label: str,
    *,
    year: int,
) -> list[int]:
    """Locate note pages using COMB finder, then printed-statements headings."""
    try:
        from comb_note_capture import find_note_pages_by_ref

        pages = find_note_pages_by_ref(pdf_path, ref, label, max_pages=4)
        if pages:
            return list(pages)[:4]
    except Exception:
        pass
    try:
        from printed_statements import (
            _index_note_headings,
            _note_page_span,
            _page_texts,
        )

        texts = _page_texts(pdf_path)
        headings = _index_note_headings(texts, 1)
        pages, _resolved = _note_page_span(headings, ref, len(texts))
        if pages:
            return list(pages)[:4]
    except Exception:
        pass
    _ = year
    return []


def _render_pngs(pdf_path: Path, pages: list[int], dpi: int = 150) -> list[bytes]:
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required to capture note pages")
    out: list[bytes] = []
    scale = dpi / 72.0
    with fitz.open(str(pdf_path)) as doc:
        for page_num in pages:
            if page_num < 1 or page_num > len(doc):
                continue
            page = doc[page_num - 1]
            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=False,
            )
            out.append(pix.tobytes("png"))
    return out


def _save_captures(
    pngs: list[bytes],
    pages: list[int],
    *,
    company_slug: str,
    year: int,
    note_ref: str,
) -> Path | None:
    try:
        root = (
            Path(__file__).resolve().parent.parent
            / "Demo_Data_captures"
            / company_slug
            / str(year)
            / "notes"
            / f"note_{note_ref.replace('.', '_')}"
        )
        root.mkdir(parents=True, exist_ok=True)
        for png, page in zip(pngs, pages):
            (root / f"page_{page:03d}.png").write_bytes(png)
        return root
    except Exception:
        return None


def _prompt(note_ref: str, title: str, year: int) -> str:
    prior = year - 1
    return f"""Extract the financial statement NOTE table from these page image(s).

Note number: {note_ref}
Likely title / line item: {title}
Report year: {year} (comparative year usually {prior})

Return ONLY valid JSON (no markdown) shaped exactly like:
{{
  "note_ref": "{note_ref}",
  "title": "<short note title as printed>",
  "unit": "LKR",
  "header_rows": [
    ["", "Group", "", "Company", ""],
    ["", "{year}", "{prior}", "{year}", "{prior}"]
  ],
  "rows": [
    {{"cells": ["<row label>", "<group {year}>", "<group {prior}>", "<company {year}>", "<company {prior}>"], "style": "data"}}
  ]
}}

Rules:
- Preserve every data row in the numeric note table (not accounting-policy paragraphs).
- Keep numbers exactly as printed (commas, parentheses for negatives, dashes).
- If the PDF only has Group (or Bank) columns, leave Company cells empty.
- If Bank is used instead of Company, put Bank amounts in the Company columns and keep headers as Group/Company.
- First cell of each row is the label; do not put amounts in the label cell.
- style may be "data", "section", "subtotal", or "total".
"""


def _normalize_note(payload: dict[str, Any], ref: str, pages: list[int]) -> dict[str, Any]:
    title = str(payload.get("title") or f"Note {ref}").strip()
    header_rows = payload.get("header_rows") or []
    rows_in = payload.get("rows") or []
    rows: list[dict[str, Any]] = []
    for row in rows_in:
        if not isinstance(row, dict):
            continue
        cells = [("" if c is None else str(c)) for c in (row.get("cells") or [])]
        if not any(c.strip() for c in cells):
            continue
        rows.append(
            {
                "cells": cells,
                "note_ref": None,
                "style": str(row.get("style") or "data"),
            }
        )
    return {
        "note_ref": str(payload.get("note_ref") or ref),
        "title": title,
        "pages": pages,
        "header_rows": header_rows,
        "rows": rows,
        "note_column": None,
        "unit": str(payload.get("unit") or "LKR"),
        "ok": len(rows) > 0,
        "extraction_model": "openai-vision",
    }


def extract_one_note_openai(
    pdf_path: Path,
    *,
    note_ref: str,
    title: str,
    year: int,
    api_key: str,
    model: str = "gpt-5",
    company_slug: str = "company",
    dpi: int = 150,
    log: LogFn | None = None,
) -> dict[str, Any]:
    pages = _find_pages(pdf_path, note_ref, title, year=year)
    if not pages:
        return {
            "note_ref": note_ref,
            "title": title,
            "pages": [],
            "header_rows": [],
            "rows": [],
            "ok": False,
            "error": "no_pages",
        }

    pngs = _render_pngs(pdf_path, pages, dpi=dpi)
    if not pngs:
        return {
            "note_ref": note_ref,
            "title": title,
            "pages": pages,
            "header_rows": [],
            "rows": [],
            "ok": False,
            "error": "render_failed",
        }

    cap_dir = _save_captures(
        pngs,
        pages,
        company_slug=company_slug,
        year=year,
        note_ref=note_ref,
    )
    if cap_dir:
        _log(log, f"    Note {note_ref}: captured {len(pngs)} page(s) → {cap_dir}")

    if OpenAI is None:
        return {
            "note_ref": note_ref,
            "title": title,
            "pages": pages,
            "header_rows": [],
            "rows": [],
            "ok": False,
            "error": "openai_package_missing",
        }

    client = OpenAI(api_key=api_key)
    content: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
            },
        }
        for png in pngs
    ]
    content.append({"type": "text", "text": _prompt(note_ref, title, year)})

    last_err = "api_error"
    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_object"},
                **_openai_token_kwargs(model),
            )
            raw = _parse_json(resp.choices[0].message.content or "{}")
            note = _normalize_note(raw, note_ref, pages)
            if note.get("ok"):
                _log(
                    log,
                    f"    Note {note_ref}: OpenAI extracted "
                    f"{len(note.get('rows') or [])} rows (pages {pages})",
                )
                return note
            last_err = "empty_table"
        except Exception as exc:
            last_err = str(exc)
            if attempt < 3:
                time.sleep(1.5 * attempt)

    return {
        "note_ref": note_ref,
        "title": title,
        "pages": pages,
        "header_rows": [],
        "rows": [],
        "ok": False,
        "error": last_err,
    }


def extract_notes_openai_for_statements(
    pdf_path: Path,
    statements: list[dict[str, Any]],
    *,
    year: int,
    company_slug: str,
    api_key: str | None = None,
    model: str = "gpt-5",
    dpi: int = 150,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Extract every note referenced on the ET main FS tables via OpenAI vision."""
    from printed_statements import collect_note_refs

    key = _resolve_api_key(api_key)
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY required for Run DB Annual note capture/extract"
        )
    if fitz is None:
        raise RuntimeError("PyMuPDF required to capture note pages")

    refs = collect_note_refs(statements)
    notes: dict[str, Any] = {}
    if not refs:
        _log(log, "  No note refs on Extracted Tables FS — nothing to capture")
        return notes

    _log(
        log,
        f"  Capturing + OpenAI-extracting {len(refs)} note table(s) "
        f"from {Path(pdf_path).name} (model={model})",
    )
    for ref in refs:
        title = _label_for_ref(statements, ref)
        note = extract_one_note_openai(
            Path(pdf_path),
            note_ref=ref,
            title=title,
            year=year,
            api_key=key,
            model=model,
            company_slug=company_slug,
            dpi=dpi,
            log=log,
        )
        notes[str(note.get("note_ref") or ref)] = note
    return notes
