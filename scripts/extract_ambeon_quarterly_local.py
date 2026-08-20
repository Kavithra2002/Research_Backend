"""
Local (no OpenAI) quarterly P&L extract for Ambeon Holdings PLC.

Parses Group current-quarter figures from CSE interim PDFs and uploads
income_statement rows to financial_tables.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

import pdfplumber

from db_uploader import MongoUploader, resolve_mongo_config, upload_results_data

COMPANY = "Ambeon Holdings PLC"
SLUG = "Ambeon_Holdings_PLC"
DEMO = Path(__file__).resolve().parent.parent / "Demo_Data" / COMPANY / "Quarterly"

AMT_TOKEN = re.compile(r"\(\s*[\d,]+\s*\)|-?[\d,]+%?|-")


def _clean_spaces(line: str) -> str:
    # Keep column-separated numbers apart; only glue thousands like "2 ,356,450".
    return " ".join(re.sub(r"(\d)\s+,", r"\1,", line).split())


def _num(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s or s == "-":
        return None
    if s.endswith("%"):
        return None
    neg = s.startswith("(") or s.startswith("-")
    s = s.replace("(", "").replace(")", "").replace(",", "").replace("-", "")
    if not s:
        return None
    try:
        n = float(s)
        return -n if neg else n
    except ValueError:
        return None


def _split_label_amounts(line: str) -> tuple[str, list[float]]:
    line = _clean_spaces(line)
    tokens = AMT_TOKEN.findall(line)
    # Rebuild label by removing amount tokens from the left/right.
    label = line
    for tok in tokens:
        label = label.replace(tok, " ", 1)
    label = re.sub(r"\s+", " ", label).strip(" :-")
    amounts: list[float] = []
    for tok in tokens:
        n = _num(tok)
        if n is not None:
            amounts.append(n)
    return label, amounts


def _is_pnl_page(text: str) -> bool:
    low = text.lower()
    if "profit or loss" not in low and "income statement" not in low:
        return False
    if "notes to" in low[:200]:
        return False
    return True


def _page_score(text: str) -> int:
    low = text.lower()
    if not _is_pnl_page(text):
        return -1
    if "profit or loss - company" in low and " - group" not in low:
        return -1
    score = 0
    if "profit or loss - group" in low or "statement of profit or loss - group" in low:
        score += 50
    if "statement of profit or loss" in low and "group" in low:
        score += 20
    if "quarter ended" in low or "three months" in low:
        score += 15
    if "for the year ended" in low and "quarter ended" not in low:
        score -= 20
    if "twelve months" in low and "quarter ended" not in low:
        score -= 10
    return score


def parse_group_quarter_pnl(pdf_path: Path) -> tuple[list[dict[str, Any]], str]:
    chosen = ""
    rows: list[dict[str, Any]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        best = ""
        best_score = -1
        for page in pdf.pages:
            text = page.extract_text() or ""
            score = _page_score(text)
            if score > best_score:
                best_score = score
                best = text
        if best_score < 0 or not best:
            return [], ""
        chosen = best

    seen: set[str] = set()
    header_bits = (
        "ended",
        "rs.",
        "rs 000",
        "unaudited",
        "audited",
        "continuing operations",
        "discontinued operations",
        "group company",
    )
    for raw in chosen.splitlines():
        line = raw.strip()
        if not line:
            continue
        starts_with_amt = bool(
            re.match(r"^\(?\s*-?[\d,]", _clean_spaces(line))
        )
        label, amounts = _split_label_amounts(line)
        if len(label) < 3 or not amounts:
            continue
        low = label.lower()
        if any(bit in low for bit in header_bits):
            continue
        if re.fullmatch(r"[\d.%]+", label):
            continue
        current = amounts[1] if starts_with_amt and len(amounts) > 1 else amounts[0]
        if current is None:
            continue
        # Day / year leftovers from header rows.
        if abs(current) <= 31 or 2010 <= abs(current) <= 2035:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", low).strip()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"label": label, "current": current})
    return rows, chosen[:120]


def fmt(n: float | None) -> str:
    if n is None:
        return "-"
    neg = n < 0
    s = f"{abs(n):,.0f}"
    return f"({s})" if neg else s


def upload_quarter(uploader: MongoUploader, year: int, quarter: str, pdf: Path, rows: list[dict[str, Any]]) -> int:
    header_rows = [
        ["", "Group"],
        ["", f"{quarter} {year}"],
        ["", "LKR '000"],
    ]
    table_rows = [{"cells": [r["label"], fmt(r["current"])], "style": "data"} for r in rows]
    doc = {
        "company": COMPANY,
        "period": f"{quarter} {year}",
        "source_pdf": str(pdf),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "local-pdf-text",
        "statements": {
            "income_statement": {
                "status": "ok",
                "title": "INCOME STATEMENT",
                "data": {
                    "statement_title": "INCOME STATEMENT",
                    "tables": [{"header_rows": header_rows, "rows": table_rows}],
                },
            }
        },
    }
    summary = upload_results_data(
        doc,
        "quarterly",
        company_slug=SLUG,
        company_name=COMPANY,
        report_group=f"Quarterly Report {year} {quarter}",
        source_pdf=str(pdf),
        uploader=uploader,
        quiet=True,
    )
    return int(summary.get("tables") or 0)


def iter_targets() -> list[tuple[int, str, Path]]:
    out: list[tuple[int, str, Path]] = []
    for year in (2019, 2020, 2021, 2022):
        for q in (1, 2, 3, 4):
            folder = DEMO / f"Quarterly Report {year} Q{q}"
            pdfs = sorted(folder.glob("*.pdf"))
            if pdfs:
                out.append((year, f"Q{q}", pdfs[0]))
    return out


def main() -> int:
    dry = "--dry-run" in sys.argv
    targets = iter_targets()
    print(f"quarters={len(targets)} dry={dry}")
    uploader = None
    if not dry:
        uri, db_name = resolve_mongo_config()
        uploader = MongoUploader(uri, db_name)

    ok = 0
    try:
        for year, quarter, pdf in targets:
            rows, hint = parse_group_quarter_pnl(pdf)
            rev = next((r["current"] for r in rows if r["label"].lower().startswith("revenue")), None)
            print(f"{year} {quarter}: rows={len(rows)} revenue={rev} pdf={pdf.name}")
            if rows[:6]:
                for r in rows[:6]:
                    print(f"    {r['label'][:48]:48} {r['current']}")
            if dry or not rows or uploader is None:
                if rows:
                    ok += 1
                continue
            n = upload_quarter(uploader, year, quarter, pdf, rows)
            print(f"    uploaded tables={n}")
            ok += 1
    finally:
        if uploader is not None:
            uploader.close()
    print(f"done ok={ok}/{len(targets)}")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
