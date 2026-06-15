"""
classify_sectors.py
===================
Identify the business SECTOR of each demo company from its earliest annual
report and store it on the ``companies`` document in MongoDB.

For every company under ``backend/Demo_Data/<Company>/Annual`` this script:

  1. Picks the FIRST (earliest year) annual report PDF.
  2. Extracts text from the opening pages (cover / chairman / "about us").
  3. Asks OpenAI to classify the company into a single high-level sector
     (Banks, Diversified Holdings, Telecommunications, Hotels & Travel,
     Healthcare, Insurance, Plantations, Beverage Food & Tobacco, ...).
  4. Upserts ``sector`` + ``sector_detail`` onto the matching company doc
     (matched by the same sanitized slug the extraction pipeline uses).

Usage
-----
    python classify_sectors.py
    python classify_sectors.py --only "Commercial Bank of Ceylon PLC"
    python classify_sectors.py --model gpt-4o-mini --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pdfplumber
from openai import OpenAI

import Data_retrive as data_retrive
import db_uploader

SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEMO_DATA   = BACKEND_DIR / "Demo_Data"

PDF_EXT = (".pdf", ".PDF")
_YEAR_RE = re.compile(r"(19|20)\d{2}")

# Canonical sector list (CSE / GICS flavoured) the model must choose from.
SECTORS = [
    "Banks",
    "Finance",
    "Insurance",
    "Diversified Holdings",
    "Telecommunications",
    "Hotels & Travel",
    "Healthcare / Hospitals",
    "Beverage, Food & Tobacco",
    "Consumer Staples",
    "Plantations",
    "Manufacturing",
    "Construction & Engineering",
    "Power & Energy",
    "Real Estate",
    "Information Technology",
    "Retail & Trading",
    "Transportation & Logistics",
    "Chemicals & Pharmaceuticals",
]

SYSTEM_PROMPT = (
    "You are a financial analyst that classifies listed companies into ONE "
    "high-level business sector. Respond with strict JSON only."
)


def _earliest_annual_pdf(company_dir: Path) -> tuple[int | None, Path | None]:
    annual = company_dir / "Annual"
    if not annual.is_dir():
        return None, None
    best_year: int | None = None
    best_pdf: Path | None = None
    for report_dir in annual.iterdir():
        if not report_dir.is_dir():
            continue
        years = [int(m.group(0)) for m in _YEAR_RE.finditer(report_dir.name)]
        years = [y for y in years if 1990 <= y <= 2100]
        if not years:
            continue
        year = min(years)
        pdfs = [p for p in report_dir.iterdir()
                if p.is_file() and p.suffix in PDF_EXT]
        if not pdfs:
            continue
        named = [p for p in pdfs if "annual report" in p.name.lower()]
        pdf = max(named or pdfs, key=lambda p: p.stat().st_size)
        if best_year is None or year < best_year:
            best_year, best_pdf = year, pdf
    return best_year, best_pdf


def _front_text(pdf_path: Path, max_pages: int = 14, max_chars: int = 9000) -> str:
    chunks: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages[:max_pages]:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    continue
    except Exception as ex:
        print(f"    [warn] could not read text: {ex}")
    text = "\n".join(chunks)
    text = re.sub(r"[ \t]+", " ", text)
    return text[:max_chars]


def classify(client: OpenAI, model: str, company: str, text: str) -> dict:
    prompt = (
        f"Company name: {company}\n\n"
        "Below is text from the opening pages of the company's annual report.\n"
        "Classify the company's PRIMARY business sector.\n\n"
        f"Choose the single best sector from this list:\n{', '.join(SECTORS)}\n\n"
        "If none fit well, pick the closest. Return JSON exactly like:\n"
        '{\"sector\": \"<one sector from the list>\", '
        '\"sector_detail\": \"<3-8 word description of what the company does>\"}\n\n'
        f"--- REPORT TEXT START ---\n{text}\n--- REPORT TEXT END ---"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=120,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    sector = str(data.get("sector") or "").strip() or None
    detail = str(data.get("sector_detail") or "").strip() or None
    return {"sector": sector, "sector_detail": detail}


def run(*, demo_root: Path, only: str | None, model: str,
        api_key: str | None, force: bool) -> None:
    if not api_key:
        print("ERROR: OpenAI API key not found (set OPENAI_API_KEY or backend/.env)")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    uri, db_name = db_uploader.resolve_mongo_config(None, None)
    from pymongo import MongoClient
    mc = MongoClient(uri, serverSelectionTimeoutMS=5000)
    mc.admin.command("ping")
    companies_col = mc[db_name]["companies"]

    company_dirs = sorted(
        p for p in demo_root.iterdir()
        if p.is_dir() and (p / "Annual").is_dir()
    )
    if only:
        want = only.strip().lower()
        company_dirs = [c for c in company_dirs if c.name.lower() == want]

    print(f"Classifying {len(company_dirs)} company sector(s) with {model}\n")

    for company_dir in company_dirs:
        company = company_dir.name
        slug = data_retrive._sanitize_company_key(company)

        existing = companies_col.find_one({"slug": slug}) or {}
        if not force and existing.get("sector"):
            print(f"  [skip] {company}: already '{existing['sector']}'")
            continue

        year, pdf = _earliest_annual_pdf(company_dir)
        if not pdf:
            print(f"  [skip] {company}: no annual PDF found")
            continue

        print(f"  - {company}  (from {year} report: {pdf.name})")
        text = _front_text(pdf)
        if not text.strip():
            print("      [warn] no extractable text; using name only")
            text = company

        try:
            result = classify(client, model, company, text)
        except Exception as ex:
            print(f"      [error] classification failed: {ex}")
            continue

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        companies_col.update_one(
            {"slug": slug},
            {"$set": {
                "sector": result["sector"],
                "sector_detail": result["sector_detail"],
                "name": company,
                "updated_at": now,
            },
             "$setOnInsert": {"slug": slug, "created_at": now}},
            upsert=True,
        )
        print(f"      => {result['sector']}  ({result['sector_detail']})")

    mc.close()
    print("\nDone.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEMO_DATA)
    ap.add_argument("--only", default=None)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    run(
        demo_root=args.source.resolve(),
        only=args.only,
        model=args.model,
        api_key=data_retrive._resolve_api_key(args.apikey),
        force=args.force,
    )
