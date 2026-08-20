"""
Company-agnostic FS note-table capture.

The DB Notes view uses a stable description list from each company's own
income statement / SoFP / cash-flow lines. Only the expanded note-table
columns change with that year's report layout.

This is the default post-extract step for every issuer (not only COMB).
"""
from __future__ import annotations

from typing import Any

from comb_note_capture import capture_note_images_for_company
from comb_note_extractor import resolve_annual_pdf
from comb_note_registry import build_note_capture_plan
from generate_comb_model import COMMERCIAL_BANK_SLUG


def capture_and_fill_company_notes(
    db,
    company_slug: str,
    year: int,
    *,
    force: bool = False,
    pdf_path=None,
) -> dict[str, Any]:
    """Capture note page images and fill ui_extracted_tables for one year."""
    pdf_path = pdf_path or resolve_annual_pdf(db, company_slug, year)
    plan = build_note_capture_plan(
        db, company_slug, year, pdf_path=pdf_path
    )
    with_notes = [p for p in plan if p.get("has_note_table")]
    capture_summary = capture_note_images_for_company(
        db,
        company_slug,
        year,
        plan=plan,
        pdf_path=pdf_path,
        force=force,
    )
    fill_summary: dict[str, Any] = {}
    try:
        from _fill_note_ui_from_pdf import fill_note_ui_for_company

        fill_summary = fill_note_ui_for_company(
            db,
            company_slug,
            [year],
            force=force,
            pdf_path=pdf_path,
        )
    except Exception as exc:
        fill_summary = {"ok": False, "error": str(exc)}

    cap_results = capture_summary.get("capture_results") or []
    captured = sum(1 for r in cap_results if r.get("ok") and not r.get("skipped"))
    filled = int(fill_summary.get("filled") or 0)
    return {
        "ok": bool(capture_summary.get("ok", True))
        and not fill_summary.get("error"),
        "company_slug": company_slug,
        "year": year,
        "notes_planned": len(with_notes),
        "notes_captured": captured,
        "note_tables_filled": filled,
        "pdf": str(pdf_path) if pdf_path else None,
        "capture": capture_summary,
        "fill": fill_summary,
        "comb_template": company_slug == COMMERCIAL_BANK_SLUG,
    }
