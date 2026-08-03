"""Reseed MongoDB extraction_keywords from COMB model manifest + alias maps."""
from __future__ import annotations

import json
import re
import sys

from pymongo import MongoClient

from comb_workbook_store import get_db
from comb_manifest import load_manifest
from extraction_catalog import build_catalog, manifest_labels_for_scope


def _escape_regex(value: str) -> str:
    return re.escape(value.strip())


def main() -> int:
    catalog = build_catalog(refresh_manifest=True)
    manifest = load_manifest(refresh=True)

    db, client = get_db()
    coll = db["extraction_keywords"]

    seeded = 0
    updated = 0
    total = 0

    for scope in ("fs", "drivers", "quarterly"):
        for row in catalog[scope]:
            total += 1
            label = row["label"].strip()
            aliases = row.get("aliases") or []
            existing = coll.find_one({"scope": scope, "canonical_label": label})
            if not existing:
                coll.insert_one(
                    {
                        "canonical_label": label,
                        "aliases": aliases,
                        "user_aliases": [],
                        "scope": scope,
                        "is_new_keyword": False,
                        "is_builtin": True,
                        "created_by": None,
                        "created_by_user_id": None,
                    }
                )
                seeded += 1
                continue

            user_aliases = existing.get("user_aliases") or []
            merged_aliases = list(
                dict.fromkeys([*(aliases or []), *user_aliases])
            )
            updates: dict[str, object] = {
                "aliases": merged_aliases,
                "is_builtin": True,
            }
            if existing.get("canonical_label") != label:
                updates["canonical_label"] = label
            coll.update_one({"_id": existing["_id"]}, {"$set": updates})
            updated += 1

    # Compare xlsx vs catalog after seed
    compare: dict[str, object] = {"seeded": seeded, "updated": updated, "total": total}
    for scope in ("fs", "drivers", "quarterly"):
        xlsx_labels = set(manifest_labels_for_scope(scope, manifest))
        catalog_labels = {row["label"] for row in catalog[scope]}
        missing = sorted(xlsx_labels - catalog_labels)
        compare[scope] = {
            "xlsx_count": len(xlsx_labels),
            "catalog_count": len(catalog_labels),
            "missing_in_catalog": missing,
            "ok": len(missing) == 0,
        }

    compare["quarterly_has_less_expenses"] = any(
        row["label"] == "Less: Expenses" for row in catalog["quarterly"]
    )
    db_count = coll.count_documents(
        {
            "canonical_label": {"$regex": "^Less: Expenses$", "$options": "i"},
        }
    )
    compare["mongo_less_expenses_count"] = db_count
    # Topic headers must not remain as description keywords.
    if compare["quarterly_has_less_expenses"] or db_count:
        compare["topic_cleanup_ok"] = False
    else:
        compare["topic_cleanup_ok"] = True

    client.close()
    print(json.dumps(compare, indent=2), flush=True)
    ok = all(compare[s]["ok"] for s in ("fs", "drivers", "quarterly"))
    return 0 if ok and compare.get("topic_cleanup_ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
