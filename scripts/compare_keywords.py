"""Compare COMB model xlsx labels with extraction keyword catalogs and MongoDB."""
from __future__ import annotations

import json
from pathlib import Path

from comb_manifest import build_manifest, load_manifest, resolve_template
from comb_workbook_store import get_db
from extraction_catalog import build_catalog, manifest_labels_for_scope
from generate_comb_model import SKIP_LABELS


def main() -> int:
    template = resolve_template()
    manifest = load_manifest(refresh=True)
    catalog = build_catalog(refresh_manifest=False)

    report: dict[str, object] = {
        "template": str(template),
        "skip_labels": sorted(SKIP_LABELS),
        "sheets": {},
    }

    for scope in ("fs", "drivers", "quarterly"):
        xlsx_set = set(manifest_labels_for_scope(scope, manifest))
        cat_set = {row["label"] for row in catalog[scope]}
        missing = sorted(xlsx_set - cat_set)
        extra = sorted(cat_set - xlsx_set)
        report["sheets"][scope] = {
            "xlsx_count": len(xlsx_set),
            "catalog_count": len(cat_set),
            "missing_in_catalog": missing,
            "extra_in_catalog": extra,
            "ok": len(missing) == 0,
        }
        print(f"\n=== {scope.upper()} ===")
        print(f"  xlsx: {len(xlsx_set)}, catalog: {len(cat_set)}, ok: {len(missing) == 0}")
        if missing:
            print(f"  missing in catalog ({len(missing)}):")
            for label in missing:
                print(f"    - {label!r}")

    ratios = manifest.get("ratios", {}).get("labels", [])
    report["sheets"]["ratios"] = {
        "xlsx_count": len(ratios),
        "note": "No extraction_keywords scope for Ratios sheet",
        "labels": ratios,
    }
    report["sheets"]["cover"] = {
        "note": "Cover sheet has entity names only; no keyword scope",
    }

    print(f"\n=== RATIOS (no DB scope) ===")
    print(f"  labels: {len(ratios)}")
    print(f"\n=== COVER ===")
    print("  no keyword scope (entity header only)")

    try:
        db, client = get_db()
        mongo_by_scope: dict[str, object] = {}
        for scope in ("fs", "drivers", "quarterly"):
            xlsx_set = set(manifest_labels_for_scope(scope, manifest))
            docs = list(
                db["extraction_keywords"].find(
                    {"scope": scope, "is_builtin": True},
                    {"canonical_label": 1},
                )
            )
            mongo_labels = {str(d["canonical_label"]) for d in docs}
            missing_mongo = sorted(xlsx_set - mongo_labels)
            mongo_by_scope[scope] = {
                "mongo_builtin_count": len(mongo_labels),
                "missing_in_mongo": missing_mongo,
                "ok": len(missing_mongo) == 0,
            }
            print(f"\n=== MONGO {scope.upper()} ===")
            print(
                f"  builtin docs: {len(mongo_labels)}, "
                f"missing vs xlsx: {len(missing_mongo)}, ok: {len(missing_mongo) == 0}"
            )
        less_exp = db["extraction_keywords"].count_documents(
            {
                "scope": "quarterly",
                "canonical_label": {"$regex": "^Less: Expenses$", "$options": "i"},
            }
        )
        report["mongo_less_expenses"] = less_exp
        print(f"\nMongoDB 'Less: Expenses' (quarterly): {less_exp}")
        client.close()
        report["mongo"] = mongo_by_scope
    except Exception as exc:
        report["mongo_error"] = str(exc)
        print(f"\nMongoDB check skipped: {exc}")

    out = (
        Path(__file__).resolve().parent.parent
        / "New_Updates"
        / "keyword_compare_report.json"
    )
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    all_ok = all(
        report["sheets"][s].get("ok") is True for s in ("fs", "drivers", "quarterly")
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
