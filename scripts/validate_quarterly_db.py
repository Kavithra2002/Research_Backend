"""
Validate all quarterly DB cells against source report PDFs.
Reports counts of correct, wrong, missing, and non-data rows.
"""
from __future__ import annotations

from comb_quarterly_pdf_verify import (
    QUARTERLY_NON_DATA_LABELS,
    build_quarterly_pdf_index,
    lookup_in_pdf_index,
    resolve_quarterly_pdf,
    values_match_report,
)
from comb_workbook_store import CombWorkbookStore, get_db
from comb_manifest import load_manifest
from generate_comb_model import (
    COMMERCIAL_BANK_SLUG,
    QUARTERLY_PILOT_QUARTERS,
    QuarterlyExtractor,
    norm_label,
)
from extract_comb_data import extract_quarterly_comb


def validate_workbook_against_pdfs(
    company_slug: str = COMMERCIAL_BANK_SLUG,
    *,
    years: list[int] | None = None,
    run_extraction: bool = True,
) -> dict:
    manifest = load_manifest()
    labels = manifest["quarterly"]["labels"]
    year_list = years or list(range(2017, 2026))

    db, client = get_db()
    store = CombWorkbookStore(db, company_slug)

    if run_extraction:
        print("Running quarterly extraction for all quarters…")
        for year in sorted(year_list, reverse=True):
            for quarter in QUARTERLY_PILOT_QUARTERS:
                doc = db.financial_tables.find_one(
                    {
                        "company_slug": company_slug,
                        "report_type": "quarterly",
                        "year": year,
                        "quarter": quarter,
                        "statement_key": "income_statement",
                    }
                )
                if not doc:
                    continue
                print(f"  {year} {quarter}…", flush=True)
                extract_quarterly_comb(
                    db, company_slug, year, quarter, skip_existing=False
                )

    summary = {
        "company_slug": company_slug,
        "total_cells": 0,
        "correct": 0,
        "wrong": 0,
        "missing_db": 0,
        "missing_pdf": 0,
        "skipped_non_data": 0,
        "no_pdf": 0,
        "wrong_cells": [],
        "missing_cells": [],
        "by_quarter": {},
    }

    aliases = QuarterlyExtractor.QUARTERLY_LABEL_ALIASES
    pdf_cache: dict[tuple[int, str], dict] = {}

    for year in year_list:
        for quarter in QUARTERLY_PILOT_QUARTERS:
            qkey = f"{year}_{quarter}"
            qstats = {
                "total": 0,
                "correct": 0,
                "wrong": 0,
                "missing_db": 0,
                "missing_pdf": 0,
                "skipped": 0,
                "no_pdf": False,
            }

            pdf_path = resolve_quarterly_pdf(db, company_slug, year, quarter)
            if not pdf_path:
                qstats["no_pdf"] = True
                summary["no_pdf"] += 1
                summary["by_quarter"][qkey] = qstats
                continue

            cache_key = (year, quarter)
            if cache_key not in pdf_cache:
                pdf_cache[cache_key] = build_quarterly_pdf_index(pdf_path, year)
            pdf_index = pdf_cache[cache_key]

            for label in labels:
                if norm_label(label) in QUARTERLY_NON_DATA_LABELS:
                    qstats["skipped"] += 1
                    summary["skipped_non_data"] += 1
                    continue

                qstats["total"] += 1
                summary["total_cells"] += 1

                db_val = store.lookup(
                    year,
                    label,
                    sheet="Quarterly",
                    report_type="quarterly",
                    quarter=quarter,
                )
                pdf_val = lookup_in_pdf_index(pdf_index, label, aliases)

                if pdf_val is None:
                    qstats["missing_pdf"] += 1
                    summary["missing_pdf"] += 1
                    if db_val is None:
                        qstats["missing_db"] += 1
                        summary["missing_db"] += 1
                        summary["missing_cells"].append(
                            {"year": year, "quarter": quarter, "label": label, "reason": "absent_in_pdf_and_db"}
                        )
                    else:
                        summary["missing_cells"].append(
                            {
                                "year": year,
                                "quarter": quarter,
                                "label": label,
                                "db": db_val,
                                "reason": "not_in_pdf_cannot_verify",
                            }
                        )
                    continue

                if db_val is None:
                    qstats["missing_db"] += 1
                    summary["missing_db"] += 1
                    summary["missing_cells"].append(
                        {
                            "year": year,
                            "quarter": quarter,
                            "label": label,
                            "pdf": pdf_val,
                            "reason": "missing_in_db",
                        }
                    )
                    continue

                if values_match_report(db_val, pdf_val):
                    qstats["correct"] += 1
                    summary["correct"] += 1
                else:
                    qstats["wrong"] += 1
                    summary["wrong"] += 1
                    summary["wrong_cells"].append(
                        {
                            "year": year,
                            "quarter": quarter,
                            "label": label,
                            "db": db_val,
                            "pdf": pdf_val,
                            "diff": db_val - pdf_val,
                        }
                    )

            summary["by_quarter"][qkey] = qstats

    client.close()
    return summary


def main() -> int:
    result = validate_workbook_against_pdfs(run_extraction=True)

    print("\n" + "=" * 60)
    print("QUARTERLY VALIDATION RESULT (DB vs source PDF)")
    print("=" * 60)
    print(f"Company: {result['company_slug']}")
    print(f"Total comparable cells: {result['total_cells']}")
    print(f"  Correct:              {result['correct']}")
    print(f"  WRONG:                {result['wrong']}")
    print(f"  Missing in DB:        {result['missing_db']}")
    print(f"  Not found in PDF:     {result['missing_pdf']}")
    print(f"  Skipped (headers):    {result['skipped_non_data']}")
    print(f"  Quarters without PDF: {result['no_pdf']}")

    if result["wrong_cells"]:
        print(f"\nWrong values ({len(result['wrong_cells'])}):")
        for item in result["wrong_cells"][:30]:
            print(
                f"  {item['year']} {item['quarter']} | {item['label']!r}: "
                f"DB={item['db']:,.0f} PDF={item['pdf']:,.0f} "
                f"(diff={item['diff']:,.0f})"
            )
        if len(result["wrong_cells"]) > 30:
            print(f"  … and {len(result['wrong_cells']) - 30} more")

    quarters_with_issues = [
        k
        for k, v in result["by_quarter"].items()
        if v.get("wrong") or v.get("missing_db")
    ]
    if quarters_with_issues:
        print("\nQuarters with issues:")
        for k in sorted(quarters_with_issues):
            v = result["by_quarter"][k]
            print(
                f"  {k}: wrong={v.get('wrong',0)} missing_db={v.get('missing_db',0)} "
                f"correct={v.get('correct',0)}/{v.get('total',0)}"
            )

    return 0 if result["wrong"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

