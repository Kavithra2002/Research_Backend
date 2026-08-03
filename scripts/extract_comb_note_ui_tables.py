"""Extract selected COMB note captures for the DB table UI with OpenAI vision.

This is intentionally a small UI pilot: three note descriptions across
2019–2021. It reuses the verbatim table-transcription prompt used by the
Extracted Tables pipeline and stores the result alongside capture metadata.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

from comb_note_capture import DEMO_CAPTURES_OUT
from comb_note_openai import resolve_api_key
from comb_workbook_store import get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG
from step3_send_to_openai import (
    PROMPTS,
    _load_b64,
    call_gpt4o,
    extract_json,
)

DEFAULT_YEARS = (2019, 2020, 2021)
TARGETS = (
    ("note_12", "Gross income"),
    ("note_13_1", "Interest income"),
    ("note_13_2", "Less: Interest expense"),
)

_YEAR_ONLY_RE = re.compile(r"^(19|20)\d{2}$")
_UNIT_RE = re.compile(
    r"(?i)^(rs\.?|lkr)(\s*'?0{3})?$|^'?0{3}$|^(rs\.?|lkr)\s*'000$"
)


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\ufffd", "\u2013")


def _clean_row_label(value: Any) -> str:
    """Collapse whitespace and strip trailing footnote markers like (*)."""
    text = re.sub(r"\s+", " ", _clean_text(value)).strip()
    text = re.sub(r"\s*\(\*+\)\s*$", "", text)
    text = re.sub(r"\s*\*+\s*$", "", text)
    return text.strip()


def _normalize_label_key(value: Any) -> str:
    text = _clean_row_label(value)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return text.casefold()


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
            label = cells[0] if cells else ""
            style = str(row.get("style") or "").strip().lower()
            if label and re.fullmatch(r"(?i)totals?", label) and style != "total":
                style = "total"
            cleaned_rows.append({**row, "cells": cells, "style": style or row.get("style")})
        # Keep Total at the bottom of each transcribed table.
        non_totals = [
            r
            for r in cleaned_rows
            if str(r.get("style") or "").lower() != "total"
            and not re.fullmatch(
                r"(?i)totals?",
                _clean_row_label((r.get("cells") or [""])[0]),
            )
        ]
        totals = [
            r
            for r in cleaned_rows
            if str(r.get("style") or "").lower() == "total"
            or re.fullmatch(
                r"(?i)totals?",
                _clean_row_label((r.get("cells") or [""])[0]),
            )
        ]
        for total_row in totals:
            total_row["style"] = "total"
        valid.append(
            {
                "caption": _clean_text(table.get("caption")) or None,
                "header_rows": _merge_year_unit_header_rows(header_rows),
                "rows": non_totals + totals,
            }
        )
    return valid


def _merge_segment_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stitch multi-image segments into one table (headers from first segment)."""
    if len(tables) <= 1:
        return tables
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
            if not label:
                continue
            style = str(row.get("style") or "").lower()
            is_total = style == "total" or bool(re.fullmatch(r"(?i)totals?", label))
            norm = _normalize_label_key(label)
            if norm in seen_labels:
                continue
            seen_labels.add(norm)
            cleaned_row = {**row, "cells": cells}
            if is_total:
                pending_totals.append({**cleaned_row, "style": "total"})
            else:
                base["rows"].append(cleaned_row)
    base["rows"].extend(pending_totals)
    return [base] if base["rows"] else tables


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


def run(*, years: list[int], model: str, force: bool) -> dict[str, Any]:
    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured")

    db, mongo_client = get_db()
    openai_client = OpenAI(api_key=api_key)
    results: list[dict[str, Any]] = []

    try:
        for year in years:
            for statement_key, description in TARGETS:
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
                    "The supplied image(s) are already cropped to the target note table.\n"
                    "If multiple images are provided, they are sequential segments of THE SAME "
                    "table (continuation pages). Merge them into ONE table in reading order.\n"
                    "Transcribe EVERY data row exactly as printed — do not skip middle rows.\n"
                    "Put the Total row last and mark it with style \"total\".\n"
                    "Do not invent rows, and do not include the next note section "
                    "(e.g. 13.2 after 13.1).\n\n"
                    "HEADER RULE: Year and unit belong in ONE cell when printed that way "
                    '(example: "2020\\nRs. \'000"). Never put "Rs. \'000" on its own '
                    "header_rows line under the year.\n\n"
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
    args = parser.parse_args()
    years = [int(value.strip()) for value in args.years.split(",") if value.strip()]
    result = run(years=years, model=args.model, force=args.force)
    for item in result["results"]:
        print(item, flush=True)
    print(
        f"Completed {result['completed']}/{result['attempted']} UI note tables",
        flush=True,
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
