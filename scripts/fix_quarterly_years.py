"""One-off fix: correct quarterly year/quarter on stored financial_tables rows.

Some rows picked up a comparative column year from table headers (e.g. 2023
beside 2022), which made the Database viewer show duplicate/missing quarters.
"""
from __future__ import annotations

import os
import re
import sys

from pymongo import MongoClient

URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGO_DB_NAME", "Research_Project")

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_QUARTER_RE = re.compile(r"\bQ\s*([1-4])\b", re.IGNORECASE)
_QUARTER_YEAR_RE = re.compile(r"\b(19|20)\d{2}\s*Q\s*([1-4])\b", re.IGNORECASE)


def year_from_name(*sources: str | None) -> int | None:
    for s in sources:
        if not s:
            continue
        m = _YEAR_RE.search(s)
        if m:
            return int(m.group(0))
    return None


def quarter_from_name(*sources: str | None) -> str | None:
    for s in sources:
        if not s:
            continue
        m = _QUARTER_RE.search(s)
        if m:
            return f"Q{m.group(1)}"
    return None


def canonical_period(doc: dict) -> tuple[int | None, str | None]:
    key = doc.get("report_key") or ""
    group = doc.get("report_group") or ""
    period = doc.get("period_label") or ""
    sources = (key, group, period)

    year = year_from_name(*sources)
    quarter = quarter_from_name(*sources)
    if year is None:
        year = doc.get("year")
    if quarter is None:
        quarter = doc.get("quarter")
    return year, quarter


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    client = MongoClient(URI, serverSelectionTimeoutMS=5000)
    coll = client[DB_NAME]["financial_tables"]

    cursor = coll.find(
        {"report_type": "quarterly"},
        {
            "report_key": 1,
            "report_group": 1,
            "period_label": 1,
            "year": 1,
            "quarter": 1,
        },
    )

    updated = 0
    checked = 0
    for doc in cursor:
        checked += 1
        new_year, new_quarter = canonical_period(doc)
        old_year = doc.get("year")
        old_quarter = doc.get("quarter")
        if new_year == old_year and new_quarter == old_quarter:
            continue
        updated += 1
        if dry_run:
            print(
                f"  {doc.get('report_key')!r}: "
                f"{old_quarter} {old_year} -> {new_quarter} {new_year}"
            )
            continue
        coll.update_one(
            {"_id": doc["_id"]},
            {"$set": {"year": new_year, "quarter": new_quarter}},
        )

    mode = "would update" if dry_run else "updated"
    print(f"{mode} {updated} / {checked} quarterly table rows")
    client.close()


if __name__ == "__main__":
    main()
