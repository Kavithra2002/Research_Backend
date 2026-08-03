"""Clear comb_workbook_data for a company (default: Commercial Bank pilot)."""
from __future__ import annotations

import argparse

from comb_workbook_store import COMB_COLLECTION, get_db
from generate_comb_model import COMMERCIAL_BANK_SLUG


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument(
        "--sheet",
        default="",
        help="Optional sheet filter: FS, Drivers, Ratios, Quarterly (repeatable via comma)",
    )
    ap.add_argument(
        "--report-type",
        default="",
        help="Optional report_type filter: annual or quarterly",
    )
    ap.add_argument(
        "--strip-fs-notes",
        action="store_true",
        help="Clear embedded note breakdown arrays on FS rows (annual only)",
    )
    args = ap.parse_args()

    db, client = get_db()
    coll = db[COMB_COLLECTION]

    if args.strip_fs_notes:
        note_query: dict = {
            "company_slug": args.company_slug.strip(),
            "sheet": "FS",
            "report_type": "annual",
        }
        result = coll.update_many(note_query, {"$set": {"notes": []}})
        print(f"Cleared notes[] on {result.modified_count} FS document(s)")
        client.close()
        return 0

    query: dict = {"company_slug": args.company_slug.strip()}
    sheet_raw = args.sheet.strip()
    if sheet_raw:
        sheets = [s.strip() for s in sheet_raw.split(",") if s.strip()]
        query["sheet"] = sheets[0] if len(sheets) == 1 else {"$in": sheets}
    if args.report_type.strip():
        query["report_type"] = args.report_type.strip()

    before = coll.count_documents(query)
    result = coll.delete_many(query)
    after = coll.count_documents(query)
    client.close()

    print(f"Deleted {result.deleted_count} document(s) from {COMB_COLLECTION}")
    print(f"  company_slug: {query['company_slug']}")
    if len(query) > 1:
        print(f"  filter: {query}")
    print(f"  before: {before}, after: {after}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
