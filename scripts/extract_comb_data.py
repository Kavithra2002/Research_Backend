"""
Build comb_workbook_data using the COMB FS template for every company.

Annual DB run (same pipeline as Commercial Bank):
  1. FS Description values from financial_tables
  2. PDF statement tables (pdfplumber, OpenAI fallback when --use-pdf-extract)
  3. Note + Page No. from statements, note page PNG captures
  4. Note-table fill for the DB Notes dropdown (pdfplumber from crops)

Drivers and Ratios are not populated on this path.

Usage:
    python extract_comb_data.py --pilot-2022
    python extract_comb_data.py --year 2022 --use-pdf-extract
    python extract_comb_data.py --pilot-2022 --use-openai-notes --force-note-capture
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from comb_cell_status import (
    STATUS_CONFIRMED_ABSENT,
    STATUS_EXTRACTION_FAILED,
    STATUS_FILLED,
    STATUS_PENDING,
    is_workbook_year,
)
from comb_manifest import load_manifest
from comb_fs_pdf_extract import (
    build_fs_pdf_index,
    build_fs_section_by_row,
    build_fs_section_map,
    label_absent_in_pdf,
)
from comb_annual_fast import (
    fast_extract_fs_values,
    fast_extract_fs_values_for_entity,
    load_existing_fs_cells,
)
from comb_annual_fs_validate import validate_annual_fs_against_report
from comb_annual_fs_pdf_verify import is_suspicious_fs_value
from comb_note_capture import (
    _note_statement_key,
    capture_note_images_for_company,
    capture_note_images_for_plan,
)
from comb_note_openai import extract_notes_openai_for_company
from comb_note_extractor import resolve_annual_pdf
from comb_note_registry import build_fs_row_note_map, build_note_capture_plan, build_label_note_index
from comb_fs_pdf_extract import build_fs_pdf_index
from comb_workbook_store import (
    COMB_COLLECTION,
    clear_workbook_sheets,
    ensure_indexes,
    get_db,
    upsert_cells,
)
from comb_quarterly_validate import validate_and_retry_quarterly_cells
from comb_annual_memorandum import (
    is_memorandum_fs_label,
    lookup_memorandum_from_pdf_index,
    lookup_memorandum_from_tables,
)
from generate_comb_model import (
    COMMERCIAL_BANK_SLUG,
    DataExtractor,
    PILOT_YEARS,
    QUARTERLY_PILOT_QUARTERS,
    QuarterlyExtractor,
    load_env,
)


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _cell_doc(
    *,
    company_slug: str,
    year: int,
    sheet: str,
    label: str,
    value: float | None,
    status: str,
    report_type: str = "annual",
    quarter: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "company_slug": company_slug,
        "year": year,
        "report_type": report_type,
        "quarter": quarter,
        "sheet": sheet,
        "label": label,
        "value": value,
        "status": status,
        **extra,
    }


def _sync_note_sources_to_workbook(
    db,
    company_slug: str,
    year: int,
    row_note_ref: dict[str, str | None],
    note_sources: dict[str, dict[str, Any]],
) -> int:
    """Attach capture metadata to FS workbook cells after note images are saved."""
    if not note_sources:
        return 0
    docs: list[dict[str, Any]] = []
    for label, note_ref in row_note_ref.items():
        if not note_ref:
            continue
        source = note_sources.get(note_ref)
        if not source:
            continue
        cell = db[COMB_COLLECTION].find_one(
            {
                "company_slug": company_slug,
                "year": year,
                "report_type": "annual",
                "sheet": "FS",
                "label": label,
            }
        )
        if not cell:
            continue
        if cell.get("note_source") == source and cell.get("note_ref") == note_ref:
            continue
        cell = dict(cell)
        cell["note_source"] = source
        cell["note_ref"] = note_ref
        docs.append(cell)
    if docs:
        upsert_cells(db, docs)
    return len(docs)


def _batch_note_source_meta(
    db,
    company_slug: str,
    year: int,
    note_refs: set[str],
) -> dict[str, dict[str, Any]]:
    if not note_refs:
        return {}
    sk_to_ref = {_note_statement_key(ref): ref for ref in note_refs}
    out: dict[str, dict[str, Any]] = {}
    for doc in db.financial_tables.find(
        {
            "company_slug": company_slug,
            "year": year,
            "statement_key": {"$in": list(sk_to_ref.keys())},
        },
        {
            "statement_key": 1,
            "note_ref": 1,
            "source_pdf": 1,
            "source_page": 1,
            "source_pages": 1,
            "capture_files": 1,
        },
    ):
        sk = str(doc.get("statement_key") or "")
        ref = doc.get("note_ref") or sk_to_ref.get(sk)
        if not ref:
            continue
        out[str(ref)] = {
            "note_ref": ref,
            "source_pdf": doc.get("source_pdf"),
            "source_page": doc.get("source_page"),
            "source_pages": doc.get("source_pages") or [],
            "capture_files": doc.get("capture_files") or [],
            "statement_key": sk,
        }
    return out


def _note_source_meta(db, company_slug: str, year: int, note_ref: str | None) -> dict[str, Any]:
    if not note_ref:
        return {}
    sources = _batch_note_source_meta(db, company_slug, year, {note_ref})
    meta = sources.get(note_ref)
    return {"note_source": meta} if meta else {}


def _cell_already_filled(
    db,
    company_slug: str,
    year: int,
    sheet: str,
    label: str,
    *,
    report_type: str = "annual",
    quarter: str | None = None,
    drivers_row: int | None = None,
    existing_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if existing_cache is not None and sheet == "FS" and report_type == "annual":
        doc = existing_cache.get(label)
    else:
        from comb_workbook_store import fetch_cell

        doc = fetch_cell(
            db,
            company_slug,
            year,
            sheet,
            label,
            report_type=report_type,
            quarter=quarter,
        )
    if not doc:
        return None
    if doc.get("status") == STATUS_FILLED and doc.get("value") is not None:
        return doc
    return None


def _status_for_value(
    value: float | None,
    *,
    year: int,
    pilot_years: list[int],
    required: bool = True,
) -> str:
    if not is_workbook_year(year):
        return STATUS_PENDING
    if value is not None:
        return STATUS_FILLED
    if not required:
        return STATUS_CONFIRMED_ABSENT
    return STATUS_EXTRACTION_FAILED


def _resolve_fs_value(
    label: str,
    year: int,
    fs_ext: DataExtractor,
    pdf_index,
    fs_sections: dict[str, str],
) -> float | None:
    section = fs_sections.get(label)
    if is_memorandum_fs_label(label):
        ft_val = lookup_memorandum_from_tables(fs_ext, year, label)
    else:
        ft_val = fs_ext.lookup(year, label)
    if pdf_index is None:
        return ft_val
    if is_memorandum_fs_label(label):
        pdf_val = lookup_memorandum_from_pdf_index(
            pdf_index, label, section=section
        )
    else:
        pdf_val = pdf_index.lookup(label, section=section)
    if pdf_val is None:
        return ft_val
    if ft_val is None:
        return pdf_val
    # When both exist but disagree by orders of magnitude, keep the larger
    # (guards against wrong-column pdfplumber parses on adjacent pages).
    if ft_val != 0 and pdf_val != 0:
        big = max(abs(ft_val), abs(pdf_val))
        small = min(abs(ft_val), abs(pdf_val))
        if small > 0 and big / small > 100:
            return ft_val if abs(ft_val) > abs(pdf_val) else pdf_val
    return pdf_val


def _status_after_pdf_check(
    label: str,
    val: float | None,
    year: int,
    *,
    pdf_path: Path | None,
    pdf_index,
    fs_sections: dict[str, str],
    use_pdf_extract: bool,
    pilot_years: list[int],
) -> str:
    status = _status_for_value(val, year=year, pilot_years=pilot_years)
    if (
        status == STATUS_EXTRACTION_FAILED
        and use_pdf_extract
        and pdf_path
        and label_absent_in_pdf(
            pdf_path,
            label,
            section=fs_sections.get(label),
            pdf_index=pdf_index,
        )
    ):
        return STATUS_CONFIRMED_ABSENT
    return status


def extract_annual_comb(
    db,
    company_slug: str,
    year: int,
    *,
    use_note_extract: bool = True,
    force_note_capture: bool = False,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = False,
    skip_existing: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest()
    fs_ext = DataExtractor(db, company_slug)
    pdf_path = resolve_annual_pdf(db, company_slug, year)
    entity_column = manifest.get("entity_column", "group")
    fs_sections = build_fs_section_map(manifest)
    section_by_row = build_fs_section_by_row(manifest)
    pdf_index = None

    clear_workbook_sheets(db, company_slug, year, ["Drivers", "Ratios"])

    label_note_index = build_label_note_index(
        db,
        company_slug,
        year,
        pdf_path=pdf_path,
    )
    fs_row_notes = build_fs_row_note_map(manifest, label_note_index)
    note_plan = build_note_capture_plan(
        db,
        company_slug,
        year,
        manifest=manifest,
        pdf_path=pdf_path,
    )
    plan_by_ref = {
        str(p["note_ref"]): p
        for p in note_plan
        if p.get("has_note_table") and p.get("note_ref")
    }

    if pdf_path and pdf_path.exists():
        print(f"  [annual] FS note links from statements: {len(fs_row_notes)}", flush=True)
        print(
            f"  [annual] Note tables to capture: "
            f"{sum(1 for p in note_plan if p.get('has_note_table'))}",
            flush=True,
        )

    fs_data_rows = [row for row in manifest["fs"]["rows"] if row["kind"] == "data"]
    fs_labels = [row["label"] for row in fs_data_rows]
    fs_links = manifest["fs"]["driver_links"]

    existing_fs = load_existing_fs_cells(db, company_slug, year) if skip_existing else {}
    fs_values: dict[str, float | None] = {}
    fs_values_bank: dict[str, float | None] = {}
    fs_skipped: set[str] = set()
    skipped_existing = 0

    print(
        f"  [annual] FS values for {year} ({len(fs_labels)} labels)…",
        flush=True,
    )
    labels_to_extract = []
    for row in fs_data_rows:
        label = row["label"]
        if skip_existing:
            existing = _cell_already_filled(
                db,
                company_slug,
                year,
                "FS",
                label,
                existing_cache=existing_fs,
            )
            if existing:
                existing_val = existing.get("value")
                if not is_suspicious_fs_value(label, existing_val, fs_values):
                    fs_values[label] = existing_val
                    fs_values_bank[label] = existing.get("value_bank")
                    fs_skipped.add(label)
                    skipped_existing += 1
                    continue
        labels_to_extract.append(label)

    if labels_to_extract:
        # Prefer section-aware lookup when the same label appears in multiple statements.
        sections_for_extract = {
            lbl: fs_sections.get(lbl) for lbl in labels_to_extract if fs_sections.get(lbl)
        }
        extracted = fast_extract_fs_values(
            fs_ext, year, labels_to_extract, sections=sections_for_extract
        )
        fs_values.update(extracted)
        bank_extracted = fast_extract_fs_values_for_entity(
            fs_ext,
            year,
            labels_to_extract,
            entity_column="bank",
            sections=sections_for_extract,
        )
        fs_values_bank.update(bank_extracted)

    # Same PDF statement audit used for Commercial Bank: fill missing /
    # suspicious FS cells from the annual report, then validate. Required so
    # non-bank issuers are not stuck on the financial_tables label mapper.
    comb_pdf_audit = bool(pdf_path and pdf_path.exists())

    if comb_pdf_audit:
        pdf_index = build_fs_pdf_index(
            pdf_path,
            manifest,
            year,
            entity_column=entity_column,
            use_openai=use_openai_notes or use_pdf_extract,
        )
        filled_from_pdf = 0
        filled_bank_from_pdf = 0
        for label in labels_to_extract:
            current = fs_values.get(label)
            section = fs_sections.get(label)
            if current is None or is_suspicious_fs_value(
                label, current, fs_values
            ):
                pdf_val = pdf_index.lookup(
                    label, section=section, entity_column=entity_column
                )
                if pdf_val is not None and not is_suspicious_fs_value(
                    label, pdf_val, fs_values
                ):
                    fs_values[label] = pdf_val
                    filled_from_pdf += 1
            if fs_values_bank.get(label) is None:
                pdf_bank = pdf_index.lookup(
                    label, section=section, entity_column="bank"
                )
                if pdf_bank is not None and not is_suspicious_fs_value(
                    label, pdf_bank, fs_values_bank
                ):
                    fs_values_bank[label] = pdf_bank
                    filled_bank_from_pdf += 1
        if filled_from_pdf:
            print(
                f"  [annual] FS PDF statement tables filled {filled_from_pdf} value(s)",
                flush=True,
            )
        if filled_bank_from_pdf:
            print(
                f"  [annual] FS PDF BANK columns filled {filled_bank_from_pdf} value(s)",
                flush=True,
            )

    validation_stats: dict[str, Any] = {}
    if comb_pdf_audit and pdf_path and pdf_path.exists():
        print(
            f"  [annual] Validate FS values against report for {year}…",
            flush=True,
        )
        fs_values, validation_stats = validate_annual_fs_against_report(
            fs_ext,
            year,
            fs_labels,
            fs_values,
            fs_sections,
            retry_labels=set(labels_to_extract),
            pdf_index=pdf_index,
            log_fn=lambda msg: print(msg, flush=True),
        )
        # After GROUP audit, fill any still-missing BANK cells from the PDF index.
        if pdf_index is not None:
            for label in fs_labels:
                if fs_values_bank.get(label) is not None:
                    continue
                section = fs_sections.get(label)
                pdf_bank = pdf_index.lookup(
                    label, section=section, entity_column="bank"
                )
                if pdf_bank is not None and not is_suspicious_fs_value(
                    label, pdf_bank, fs_values_bank
                ):
                    fs_values_bank[label] = pdf_bank

    # Resolve duplicate FS labels (e.g. P&L vs SoFP "Non-controlling interest")
    # into per-template-row values so both can be stored and AI-confirmed.
    from collections import Counter

    from comb_annual_fs_ai_confirm import ai_confirm_fs_labels

    label_counts = Counter(str(r["label"]) for r in fs_data_rows)
    dup_labels = {lbl for lbl, n in label_counts.items() if n > 1}
    row_values: dict[int, float | None] = {}
    row_values_bank: dict[int, float | None] = {}
    if dup_labels:
        print(
            f"  [annual] Resolving {len(dup_labels)} duplicate FS label(s) by section…",
            flush=True,
        )
        for row in fs_data_rows:
            label = str(row["label"])
            trow = int(row["row"])
            section = section_by_row.get(trow) or fs_sections.get(label)
            if label not in dup_labels:
                row_values[trow] = fs_values.get(label)
                row_values_bank[trow] = fs_values_bank.get(label)
                continue
            val = fs_ext.lookup(year, label, section=section)
            bank_val = fs_ext.lookup(
                year, label, entity_column="bank", section=section
            )
            if pdf_index is not None:
                pdf_val = pdf_index.lookup(
                    label, section=section, entity_column=entity_column
                )
                if pdf_val is not None and not is_suspicious_fs_value(
                    label, pdf_val, fs_values
                ):
                    # Always prefer the section-specific PDF figure for duplicate
                    # labels — table indexes often only keep one statement's row.
                    val = pdf_val
                pdf_bank = pdf_index.lookup(
                    label, section=section, entity_column="bank"
                )
                if pdf_bank is not None:
                    bank_val = pdf_bank
            row_values[trow] = val
            row_values_bank[trow] = bank_val
            print(
                f"    row {trow} [{section}] {label!r} = {val}",
                flush=True,
            )

        # AI-confirm each duplicate occurrence only when still missing.
        # Section-specific PDF/table values are authoritative for P&L vs SoFP
        # siblings (AI row crops often land on the wrong statement page).
        if comb_pdf_audit and pdf_path and pdf_path.exists():
            for row in fs_data_rows:
                label = str(row["label"])
                if label not in dup_labels:
                    continue
                trow = int(row["row"])
                if row_values.get(trow) is not None:
                    continue
                section = section_by_row.get(trow) or fs_sections.get(label)
                single_sections = {label: section} if section else fs_sections
                single_vals = {label: row_values.get(trow)}
                single_vals, ai_dup = ai_confirm_fs_labels(
                    db,
                    company_slug,
                    year,
                    [label],
                    single_vals,
                    single_sections,
                    pdf_index=pdf_index,
                    pdf_mismatch_labels={label},
                    double_confirm=True,
                    log_fn=lambda msg: print(msg, flush=True),
                )
                if single_vals.get(label) is not None:
                    row_values[trow] = single_vals.get(label)
                if isinstance(validation_stats, dict):
                    validation_stats.setdefault("duplicate_ai", []).append(
                        {"row": trow, "label": label, "ai": ai_dup}
                    )
    else:
        for row in fs_data_rows:
            trow = int(row["row"])
            label = str(row["label"])
            row_values[trow] = fs_values.get(label)
            row_values_bank[trow] = fs_values_bank.get(label)

    note_capture_summary: dict[str, Any] = {}
    if use_note_extract:
        if use_openai_notes:
            if pdf_path:
                note_capture_summary = extract_notes_openai_for_company(
                    db,
                    company_slug,
                    year,
                    pdf_path,
                    entity_column=entity_column,
                    force=force_note_capture,
                )
            else:
                note_capture_summary = {"ok": False, "error": "annual_pdf_not_found"}
        else:
            note_capture_summary = capture_note_images_for_company(
                db,
                company_slug,
                year,
                plan=note_plan,
                pdf_path=pdf_path,
                force=force_note_capture,
            )
            cap_results = note_capture_summary.get("capture_results") or []
            failed = [
                r
                for r in cap_results
                if not r.get("ok") and not r.get("skipped")
            ]
            # Retry notes that failed to locate or ended before Total (reuse warm caches).
            retry_failed = [
                r
                for r in failed
                if r.get("reason")
                in {"table_region_not_found", "pdf_not_found", "incomplete_table_capture"}
            ]
            if retry_failed and pdf_path and pdf_path.exists():
                print(
                    f"  [annual] Retry {len(retry_failed)} note capture(s) "
                    f"for {year}…",
                    flush=True,
                )
                retry_refs = {str(r.get("note_ref") or "") for r in retry_failed}
                retry_plan = [
                    p
                    for p in note_plan
                    if p.get("has_note_table")
                    and str(p.get("note_ref") or "") in retry_refs
                ]
                retry_results = capture_note_images_for_plan(
                    db,
                    company_slug,
                    year,
                    retry_plan,
                    pdf_path=pdf_path,
                    force=True,
                )
                by_ref = {
                    str(r.get("note_ref") or ""): r
                    for r in cap_results
                    if r.get("note_ref")
                }
                for r in retry_results:
                    by_ref[str(r.get("note_ref") or "")] = r
                note_capture_summary["capture_results"] = list(by_ref.values())
            elif failed:
                print(
                    f"  [annual] Note capture incomplete for {len(failed)} note(s)",
                    flush=True,
                )

            cap_results = note_capture_summary.get("capture_results") or cap_results
            png_count = sum(int(r.get("images_saved") or 0) for r in cap_results)
            note_ok = sum(1 for r in cap_results if r.get("ok"))
            print(
                f"  [annual] Note page images for {year}: "
                f"{png_count} PNG(s) across {note_ok} note(s)",
                flush=True,
            )

        # Local note-table transcription for the DB Notes dropdown.
        try:
            from _fill_note_ui_from_pdf import fill_note_ui_for_company

            fill_summary = fill_note_ui_for_company(
                db,
                company_slug,
                [year],
                force=force_note_capture,
                pdf_path=pdf_path,
            )
            note_capture_summary["note_tables"] = fill_summary
            filled_tables = int(fill_summary.get("filled") or 0)
            print(
                f"  [annual] Note tables filled for {year}: {filled_tables}",
                flush=True,
            )
        except Exception as exc:
            note_capture_summary["note_tables"] = {"ok": False, "error": str(exc)}
            print(
                f"  [annual] Note table fill failed for {year}: {exc}",
                flush=True,
            )

    note_refs_needed: set[str] = set()
    row_note_ref: dict[str, str | None] = {}
    row_has_notes: dict[str, bool] = {}
    for row in fs_data_rows:
        label = row["label"]
        drivers_row = fs_links.get(label)
        note_meta = fs_row_notes.get(label)
        note_ref = str(note_meta["note_ref"]) if note_meta else None
        row_note_ref[label] = note_ref
        row_has_notes[label] = bool(note_ref)
        if note_ref:
            note_refs_needed.add(note_ref)

    note_sources = _batch_note_source_meta(db, company_slug, year, note_refs_needed)
    if use_note_extract and note_sources:
        synced = _sync_note_sources_to_workbook(
            db, company_slug, year, row_note_ref, note_sources
        )
        if synced:
            print(
                f"  [annual] Linked note captures to {synced} FS cell(s) for {year}",
                flush=True,
            )

    docs: list[dict[str, Any]] = []
    filled = missing = confirmed_absent = 0
    confidence_map = (
        ((validation_stats.get("ai_confirm") or {}).get("confidence") or {})
        if isinstance(validation_stats, dict)
        else {}
    )

    for row in fs_data_rows:
        label = row["label"]
        template_row = int(row["row"]) if row.get("row") is not None else None
        drivers_row = fs_links.get(label)
        note_ref = row_note_ref.get(label)

        if label in fs_skipped and template_row is None:
            existing = existing_fs.get(label)
            if existing:
                existing = {
                    **existing,
                    "notes": [],
                    "has_notes": row_has_notes.get(label, False),
                    "drivers_row": drivers_row,
                    "note_ref": note_ref,
                }
                source = note_sources.get(note_ref or "")
                if source:
                    existing["note_source"] = source
                docs.append(existing)
            continue

        val = (
            row_values.get(template_row)
            if template_row is not None
            else fs_values.get(label)
        )
        bank_val = (
            row_values_bank.get(template_row)
            if template_row is not None
            else fs_values_bank.get(label)
        )
        status = _status_after_pdf_check(
            label,
            val,
            year,
            pdf_path=pdf_path,
            pdf_index=pdf_index,
            fs_sections=fs_sections,
            use_pdf_extract=use_pdf_extract,
            pilot_years=PILOT_YEARS,
        )
        note_source_extra: dict[str, Any] = {}
        if note_ref and note_ref in note_sources:
            note_source_extra = {"note_source": note_sources[note_ref]}
        conf = confidence_map.get(label)
        if conf:
            note_source_extra["confidence"] = conf
        if template_row is not None:
            note_source_extra["template_row"] = template_row
            sec = section_by_row.get(template_row)
            if sec:
                note_source_extra["statement_section"] = sec

        docs.append(
            _cell_doc(
                company_slug=company_slug,
                year=year,
                sheet="FS",
                label=label,
                value=val,
                status=status,
                value_group=val,
                value_bank=bank_val,
                has_notes=row_has_notes.get(label, False),
                drivers_row=drivers_row,
                notes=[],
                note_ref=note_ref,
                source_collection=(
                    "pdf_extract+financial_tables"
                    if pdf_index is not None
                    else "financial_tables"
                ),
                extraction_method=(
                    "openai_vision" if use_openai_notes and note_ref else None
                ),
                **note_source_extra,
            )
        )
        if status == STATUS_FILLED:
            filled += 1
        elif status == STATUS_CONFIRMED_ABSENT:
            confirmed_absent += 1
        else:
            missing += 1

    filled += skipped_existing
    upserted = upsert_cells(db, docs)
    return {
        "ok": True,
        "company_slug": company_slug,
        "year": year,
        "report_type": "annual",
        "cells_upserted": upserted,
        "cells_filled": filled,
        "cells_missing": missing,
        "cells_confirmed_absent": confirmed_absent,
        "pdf_used": str(pdf_path) if pdf_path else None,
        "note_extract_used": bool(use_note_extract),
        "openai_notes_used": bool(use_openai_notes),
        "pdf_extract_used": bool(use_pdf_extract),
        "note_capture": note_capture_summary,
        "collection": COMB_COLLECTION,
        "cells_skipped_existing": skipped_existing,
        "validation": validation_stats or {"fast_path": not bool(validation_stats)},
    }


def extract_quarterly_comb(
    db,
    company_slug: str,
    year: int,
    quarter: str,
    *,
    skip_existing: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest()
    labels = manifest["quarterly"]["labels"]
    q_ext = QuarterlyExtractor(db, company_slug, year)
    docs: list[dict[str, Any]] = []
    values: dict[str, float | None] = {}
    skipped_labels: set[str] = set()
    skipped_existing = 0

    print(
        f"  [quarterly] Pass 1 — initial extraction for {year} {quarter} "
        f"({len(labels)} labels)…",
        flush=True,
    )
    for label in labels:
        if skip_existing:
            existing = _cell_already_filled(
                db,
                company_slug,
                year,
                "Quarterly",
                label,
                report_type="quarterly",
                quarter=quarter,
            )
            if existing:
                docs.append(existing)
                values[label] = existing.get("value")
                skipped_labels.add(label)
                skipped_existing += 1
                continue
        values[label] = q_ext.lookup(quarter, label)

    validation_stats: dict[str, Any] = {}
    if labels:
        print(
            f"  [quarterly] Pass 2 — validate, retry missing, PDF verify (×2) "
            f"for {year} {quarter}…",
            flush=True,
        )
        values, validation_stats = validate_and_retry_quarterly_cells(
            q_ext,
            quarter,
            labels,
            values,
            retry_labels={lbl for lbl in labels if lbl not in skipped_labels},
            log_fn=lambda msg: print(msg, flush=True),
        )

    filled = missing = confirmed_absent = 0
    for label in labels:
        if label in skipped_labels:
            continue
        val = values.get(label)
        status = _status_for_value(val, year=year, pilot_years=PILOT_YEARS)
        used_pdf = bool((validation_stats.get("pdf") or {}).get("pdf_used")) or bool(
            (validation_stats.get("pdf_final") or {}).get("pdf_used")
        )
        docs.append(
            _cell_doc(
                company_slug=company_slug,
                year=year,
                sheet="Quarterly",
                label=label,
                value=val,
                status=status,
                report_type="quarterly",
                quarter=quarter,
                has_notes=False,
                notes=[],
                source_collection=(
                    "financial_tables+pdf_verify" if used_pdf else "financial_tables"
                ),
                validation=validation_stats if validation_stats else None,
            )
        )
        if status == STATUS_FILLED:
            filled += 1
        elif status == STATUS_CONFIRMED_ABSENT:
            confirmed_absent += 1
        else:
            missing += 1

    filled += skipped_existing
    upserted = upsert_cells(db, docs)
    return {
        "ok": True,
        "company_slug": company_slug,
        "year": year,
        "report_type": "quarterly",
        "quarter": quarter,
        "cells_upserted": upserted,
        "cells_filled": filled,
        "cells_missing": missing,
        "cells_confirmed_absent": confirmed_absent,
        "collection": COMB_COLLECTION,
        "cells_skipped_existing": skipped_existing,
        "validation": validation_stats,
    }


def extract_comb_pilot_2022(
    db,
    company_slug: str = COMMERCIAL_BANK_SLUG,
    *,
    use_note_extract: bool = True,
    force_note_capture: bool = False,
    use_openai_notes: bool = False,
    use_pdf_extract: bool = False,
) -> dict[str, Any]:
    results = [
        extract_annual_comb(
            db,
            company_slug,
            2022,
            use_note_extract=use_note_extract,
            force_note_capture=force_note_capture,
            use_openai_notes=use_openai_notes,
            use_pdf_extract=use_pdf_extract,
        )
    ]
    for q in QUARTERLY_PILOT_QUARTERS:
        results.append(extract_quarterly_comb(db, company_slug, 2022, q))
    return {
        "ok": True,
        "company_slug": company_slug,
        "pilot_year": 2022,
        "steps": results,
        "cells_filled": sum(r.get("cells_filled", 0) for r in results),
        "cells_missing": sum(r.get("cells_missing", 0) for r in results),
        "cells_confirmed_absent": sum(
            r.get("cells_confirmed_absent", 0) for r in results
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract COMB DB-page data into MongoDB")
    ap.add_argument("--company-slug", default=COMMERCIAL_BANK_SLUG)
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--quarter", default="")
    ap.add_argument("--pilot-2022", action="store_true")
    ap.add_argument(
        "--no-note-extract",
        action="store_true",
        help="Skip note page capture for annual reports",
    )
    ap.add_argument(
        "--force-note-capture",
        action="store_true",
        help="Re-extract note tables from PDF into financial_tables",
    )
    ap.add_argument(
        "--use-openai-notes",
        action="store_true",
        help="Capture note tables via OpenAI vision (in-memory PNG, full tables)",
    )
    ap.add_argument(
        "--use-pdf-extract",
        action="store_true",
        help="Extract FS values from PDF (pdfplumber + OpenAI); implies --use-openai-notes",
    )
    args = ap.parse_args()

    use_note_extract = not args.no_note_extract
    force_note_capture = args.force_note_capture
    use_pdf_extract = args.use_pdf_extract
    use_openai_notes = args.use_openai_notes or use_pdf_extract

    try:
        db, client = get_db()
        ensure_indexes(db)
        slug = args.company_slug.strip()

        if args.pilot_2022:
            result = extract_comb_pilot_2022(
                db,
                slug,
                use_note_extract=use_note_extract,
                force_note_capture=force_note_capture,
                use_openai_notes=use_openai_notes,
                use_pdf_extract=use_pdf_extract,
            )
        elif args.quarter.strip():
            year = args.year or PILOT_YEARS[0]
            result = extract_quarterly_comb(
                db, slug, year, args.quarter.strip().upper()
            )
        else:
            year = args.year or PILOT_YEARS[0]
            result = extract_annual_comb(
                db,
                slug,
                year,
                use_note_extract=use_note_extract,
                force_note_capture=force_note_capture,
                use_openai_notes=use_openai_notes,
                use_pdf_extract=use_pdf_extract,
            )

        client.close()
        _emit(result)
        return 0
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
