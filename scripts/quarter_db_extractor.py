"""
quarter_db_extractor.py
=======================
Run DB Quarter pipeline:

  1. Locate the quarterly PDF for the ticked year/quarter
  2. Capture statement pages + OpenAI vision extract (when API key set)
     with an accuracy recheck / re-extract for missing families
  3. Upload tables to financial_tables
  4. Map values into comb_workbook_data for the DB page

Skips cells that already have filled values unless --force is passed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from comb_workbook_store import ensure_indexes, get_db
from extract_comb_data import extract_quarterly_comb
from generate_comb_model import COMMERCIAL_BANK_SLUG, QUARTERLY_PILOT_QUARTERS
from runner_common import configure_stdio, emit, emit_log

configure_stdio()

_YEAR_RE = re.compile(r"(20\d{2})")
_QUARTER_RE = re.compile(r"\bQ([1-4])\b", re.I)


def _year_quarter_from_item(raw: dict[str, Any]) -> tuple[int, str] | None:
    """Resolve (year, Qn) from a ticked local report item."""
    year_raw = raw.get("year")
    quarter_raw = raw.get("quarter")
    sources = [
        raw.get("group"),
        raw.get("file_name"),
        raw.get("rel_path"),
        raw.get("group_name"),
    ]

    year: int | None = None
    if year_raw is not None:
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            year = None
    if year is None:
        for s in sources:
            if not s:
                continue
            m = _YEAR_RE.search(str(s))
            if m:
                year = int(m.group(1))
                break
    if year is None or year < 1990 or year > 2100:
        return None

    quarter: str | None = None
    if quarter_raw is not None:
        q = str(quarter_raw).strip().upper()
        if q.isdigit() and q in {"1", "2", "3", "4"}:
            quarter = f"Q{q}"
        elif q.startswith("Q") and len(q) == 2 and q[1].isdigit():
            quarter = q
    if quarter is None:
        for s in sources:
            if not s:
                continue
            m = _QUARTER_RE.search(str(s))
            if m:
                quarter = f"Q{m.group(1)}"
                break
    if quarter is None:
        return None

    return year, quarter


def _parse_steps(payload: dict[str, Any]) -> list[tuple[int, str]]:
    """Return [(year, quarter), ...] from request payload."""
    steps: list[tuple[int, str]] = []
    items = payload.get("items") or []
    item_paths: dict[tuple[int, str], dict[str, Any]] = {}
    if isinstance(items, list) and items:
        seen: set[tuple[int, str]] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            parsed = _year_quarter_from_item(raw)
            if not parsed or parsed in seen:
                continue
            seen.add(parsed)
            steps.append(parsed)
            item_paths[parsed] = raw
        if steps:
            # stash on function for later PDF resolve
            _parse_steps._item_meta = item_paths  # type: ignore[attr-defined]
            return sorted(steps, key=lambda t: (-t[0], t[1]))

    years = payload.get("years")
    quarters = payload.get("quarters") or QUARTERLY_PILOT_QUARTERS
    if isinstance(years, list):
        for year in sorted({int(y) for y in years}, reverse=True):
            for quarter in quarters:
                steps.append((year, str(quarter)))
    return steps


def _resolve_pdf_from_item(raw: dict[str, Any] | None) -> Path | None:
    if not raw:
        return None
    from runner_common import DEMO_DATA_DIR

    rel = raw.get("rel_path")
    if rel:
        p = (DEMO_DATA_DIR / str(rel)).resolve()
        if p.exists():
            return p
    company = str(raw.get("company") or "").strip()
    group = str(raw.get("group") or "").strip()
    file_name = str(raw.get("file_name") or "").strip()
    report_type = str(raw.get("report_type") or "Quarterly").strip() or "Quarterly"
    if company and group and file_name:
        p = (DEMO_DATA_DIR / company / report_type / group / file_name).resolve()
        if p.exists():
            return p
    return None


def _openai_refresh_quarterly(
    *,
    company_slug: str,
    company_name: str,
    year: int,
    quarter: str,
    pdf_path: Path,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Capture + OpenAI extract one quarterly PDF into financial_tables."""
    import db_uploader
    import Q_data_extraction as q_ext

    with tempfile.TemporaryDirectory(
        prefix=f"ambeon-db-q-{year}-{quarter}-"
    ) as tmp:
        work = Path(tmp)
        emit_log(
            f"  OpenAI quarterly extract {year} {quarter} "
            f"from {pdf_path.name} (model={model})"
        )
        res = q_ext.process_quarterly_for_company(
            company_key=company_slug,
            pdf_path=pdf_path,
            testing_dir=work,
            api_key=api_key,
            model=model,
            dry_run=False,
            force=True,
        )
        status = res.get("status") if isinstance(res, dict) else "error"
        results_path = (
            work / company_slug / f"{company_slug}_quarterly_results.json"
        )
        if not results_path.exists():
            return {
                "ok": False,
                "status": status,
                "error": "quarterly results JSON not produced",
            }

        doc = json.loads(results_path.read_text(encoding="utf-8"))
        # Accuracy: Q_data_extraction already runs validation+re-extract when
        # OpenAI is enabled; surface residual gaps here.
        validation = (res or {}).get("validation") if isinstance(res, dict) else None
        if isinstance(validation, dict) and not validation.get("ok", True):
            gaps = validation.get("missing_manifest") or validation.get(
                "failed_extraction"
            ) or []
            emit_log(
                f"  [accuracy] quarterly gaps after OpenAI recheck: "
                f"{', '.join(gaps) if gaps else 'see validation'}",
                level="warn",
            )
        else:
            emit_log("  [accuracy] quarterly statements verified / rechecked")

        summary = db_uploader.upload_results_data(
            doc,
            "quarterly",
            company_slug=company_slug,
            company_name=company_name,
            report_group=f"Quarterly report {year} {quarter}",
            source_pdf=str(pdf_path),
            quiet=True,
        )
        return {
            "ok": bool(summary.get("ok")),
            "status": status,
            "tables": summary.get("tables", 0),
            "validation": validation,
        }


def run_quarter_db(
    steps: list[tuple[int, str]],
    *,
    company_slug: str = COMMERCIAL_BANK_SLUG,
    skip_existing: bool = True,
    item_meta: dict[tuple[int, str], dict[str, Any]] | None = None,
    api_key: str | None = None,
    model: str = "gpt-5",
) -> int:
    if not steps:
        emit({"type": "error", "message": "No quarterly steps selected"})
        emit({"type": "done", "ok": 0, "failed": 1})
        return 1

    import extraction_accuracy as acc
    from comb_quarterly_pdf_verify import resolve_quarterly_pdf

    resolved_key = acc.resolve_api_key(api_key)
    item_meta = item_meta or {}

    emit(
        {
            "type": "start",
            "mode": "db-quarterly",
            "company_slug": company_slug,
            "totalSteps": len(steps),
            "skipExisting": skip_existing,
            "openai": bool(resolved_key),
            "model": model if resolved_key else "comb-mapper",
        }
    )
    if resolved_key:
        emit_log(
            f"Run DB Quarter: OpenAI vision refresh + COMB map (model={model})"
        )
    else:
        emit_log(
            "OPENAI_API_KEY not set — mapping existing financial_tables only",
            level="warning",
        )

    db, client = get_db()
    ensure_indexes(db)
    ok = failed = 0
    cells_filled = cells_missing = cells_skipped = 0

    def _company_name() -> str:
        doc = db.companies.find_one({"slug": company_slug}, {"name": 1})
        if doc and doc.get("name"):
            return str(doc["name"]).strip()
        return company_slug.replace("_", " ").strip()

    try:
        for idx, (year, quarter) in enumerate(steps, 1):
            label = (
                f"Quarter {int(str(quarter).lstrip('Qq')):02d} - {year}"
                if quarter
                else f"Quarterly report {year}"
            )
            emit(
                {
                    "type": "stage-start",
                    "year": year,
                    "quarter": quarter,
                    "stage": "quarterly",
                    "label": label,
                    "index": idx,
                    "totalSteps": len(steps),
                }
            )
            emit_log(f"DB quarterly extraction for {label}…")
            try:
                # ── OpenAI refresh from source PDF when possible ──────────
                if resolved_key:
                    meta = item_meta.get((year, quarter))
                    pdf_path = _resolve_pdf_from_item(meta)
                    if pdf_path is None:
                        pdf_path = resolve_quarterly_pdf(
                            db, company_slug, year, quarter
                        )
                    if pdf_path and pdf_path.exists():
                        refresh = _openai_refresh_quarterly(
                            company_slug=company_slug,
                            company_name=_company_name(),
                            year=year,
                            quarter=quarter,
                            pdf_path=pdf_path,
                            api_key=resolved_key,
                            model=model,
                        )
                        emit_log(
                            f"  OpenAI refresh: status={refresh.get('status')} "
                            f"tables={refresh.get('tables', 0)}"
                        )
                    else:
                        emit_log(
                            f"  No quarterly PDF found for {label} — "
                            "mapping existing tables only",
                            level="warn",
                        )

                result = extract_quarterly_comb(
                    db,
                    company_slug,
                    year,
                    quarter,
                    skip_existing=skip_existing,
                )
                filled = int(result.get("cells_filled", 0) or 0)
                missing = int(result.get("cells_missing", 0) or 0)
                skipped = int(result.get("cells_skipped_existing", 0) or 0)
                validation = result.get("validation") or {}
                cells_filled += filled
                cells_missing += missing
                cells_skipped += skipped
                ok += 1
                if validation.get("recovered"):
                    emit_log(
                        f"  Validation recovered {validation['recovered']} cell(s), "
                        f"corrected {validation.get('corrected', 0)}"
                    )
                if validation.get("pdf_corrected"):
                    emit_log(
                        f"  PDF cross-check corrected {validation['pdf_corrected']} value(s) "
                        f"against source report"
                    )
                if validation.get("still_missing"):
                    emit_log(
                        f"  Still missing after validation: "
                        f"{validation['still_missing']} cell(s)",
                        level="warn",
                    )
                emit(
                    {
                        "type": "stage-done",
                        "year": year,
                        "quarter": quarter,
                        "label": (
                            f"Quarter {int(str(quarter).lstrip('Qq')):02d} - {year}"
                            if quarter
                            else f"Quarterly report {year}"
                        ),
                        "ok": True,
                        "company": company_slug,
                        "cells_filled": filled,
                        "cells_missing": missing,
                        "cells_skipped_existing": skipped,
                        "validation": validation,
                    }
                )
            except Exception as exc:
                failed += 1
                emit(
                    {
                        "type": "stage-done",
                        "year": year,
                        "quarter": quarter,
                        "stage": "quarterly",
                        "ok": False,
                        "error": str(exc),
                    }
                )
                emit_log(traceback.format_exc(), level="error")

        emit(
            {
                "type": "done",
                "ok": ok,
                "failed": failed,
                "cells_filled": cells_filled,
                "cells_missing": cells_missing,
                "cells_skipped_existing": cells_skipped,
            }
        )
        return 0 if failed == 0 else 1
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items-stdin", action="store_true")
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--model", default="gpt-5")
    args = ap.parse_args(argv)

    if not args.items_stdin:
        emit({"type": "error", "message": "Provide --items-stdin with years/quarters JSON."})
        return 2

    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        emit({"type": "error", "message": f"Invalid stdin JSON: {exc!r}"})
        return 2

    payload_dict = payload if isinstance(payload, dict) else {}
    steps = _parse_steps(payload_dict)
    item_meta = getattr(_parse_steps, "_item_meta", {}) or {}
    return run_quarter_db(
        steps,
        company_slug=args.company_slug,
        skip_existing=not args.force,
        item_meta=item_meta,
        api_key=args.apikey,
        model=args.model,
    )


if __name__ == "__main__":
    raise SystemExit(main())
