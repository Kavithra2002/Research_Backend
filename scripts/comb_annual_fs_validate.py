"""
comb_annual_fs_validate.py
============================
Annual FS extraction validation — same methodology as quarterly:

  A) Retry missing labels (aggressive table lookup)
  B) Re-read stored financial_tables (do not overwrite good values)
  C) Memorandum rows — GROUP then BANK/COMPANY (tables + PDF)
  D) Cross-check GROUP column in annual PDF (authoritative)
  E) Final PDF verification pass
  F) Re-check suspicious values against PDF
  G) Refill still-missing labels from PDF
  H) Strict audit of all filled values vs PDF
  I) Targeted re-find for labels still missing or wrong
"""
from __future__ import annotations

from typing import Any, Callable

from comb_annual_fs_pdf_verify import (
    annual_values_match,
    build_annual_fs_pdf_index,
    cross_check_fs_values_against_pdf,
    final_fs_pdf_verification_pass,
    is_suspicious_fs_value,
    recheck_suspicious_fs_values_against_pdf,
    refill_missing_fs_from_pdf,
    strict_audit_filled_fs_against_pdf,
    targeted_refind_fs_labels,
)
from comb_annual_memorandum import (
    is_memorandum_fs_label,
    is_plausible_memorandum_count,
    lookup_memorandum_from_pdf_index,
    lookup_memorandum_from_tables,
)
from comb_note_extractor import resolve_annual_pdf
from comb_quarterly_pdf_verify import values_match_report
from extraction_aliases_store import patterns_for_label
from generate_comb_model import DataExtractor, LABEL_ALIASES, norm_label


OP_PROFIT_BEFORE_FS_TAX = "Operating profit before taxes on financial services"
OP_PROFIT_AFTER_FS_TAX = "Operating profit after taxes on financial services"
FS_TAX_ON_SERVICES = "Less: Taxes on financial services"


def reconcile_operating_profit_fs_tax(
    values: dict[str, float | None],
    *,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Double-check operating profit before/after taxes on financial services.

    Enforces: after ≈ before − tax. Fixes the common PDF fuzzy-match bug where
    "after" is overwritten with the same amount as "before".
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "checked": False,
        "corrected": False,
        "from": None,
        "to": None,
        "expected": None,
    }

    before = values.get(OP_PROFIT_BEFORE_FS_TAX)
    tax = values.get(FS_TAX_ON_SERVICES)
    after = values.get(OP_PROFIT_AFTER_FS_TAX)
    if before is None or tax is None:
        return values, stats

    expected = float(before) - float(tax)
    stats["checked"] = True
    stats["expected"] = expected

    needs_fix = False
    if after is None:
        needs_fix = True
    elif annual_values_match(after, before) and abs(float(tax)) >= 1.0:
        # Classic bug: after cloned from before while tax is non-zero.
        needs_fix = True
    elif not annual_values_match(after, expected):
        # Prefer arithmetic when before/tax are both present and after disagrees.
        needs_fix = True

    if not needs_fix:
        return values, stats

    if after is not None and annual_values_match(after, expected):
        return values, stats

    stats["corrected"] = True
    stats["from"] = after
    stats["to"] = expected
    values[OP_PROFIT_AFTER_FS_TAX] = expected
    if after is None:
        log(
            f"    [fs-tax-check] filled {OP_PROFIT_AFTER_FS_TAX!r} = {expected:,.0f} "
            f"(before - tax)"
        )
    else:
        log(
            f"    [fs-tax-check] corrected {OP_PROFIT_AFTER_FS_TAX!r}: "
            f"{after:,.0f} -> {expected:,.0f} (before - tax double-check)"
        )
    return values, stats


def aggressive_fs_lookup(
    fs_ext: DataExtractor,
    year: int,
    template_label: str,
) -> float | None:
    """Try exact label, then alias patterns, across all annual statement docs."""
    if is_memorandum_fs_label(template_label):
        return lookup_memorandum_from_tables(fs_ext, year, template_label)

    val = fs_ext.lookup(year, template_label)
    if val is not None:
        return val

    patterns = patterns_for_label(
        template_label, LABEL_ALIASES, "fs", default_to_label=True
    )
    for pat in patterns:
        if pat == template_label:
            continue
        val = fs_ext.lookup(year, pat)
        if val is not None:
            return val

    from generate_comb_model import find_value_col, parse_number

    nt = norm_label(template_label)
    for doc in fs_ext._docs_for_year(year):
        headers = doc.get("header_rows") or []
        col = find_value_col(doc, year)
        for row in doc.get("rows") or []:
            cells = row.get("cells") if isinstance(row, dict) else row
            if not cells:
                continue
            label_cell = str(cells[0] or "").strip()
            if not label_cell:
                continue
            nl = norm_label(label_cell)
            from generate_comb_model import _fs_label_match_ok
            from comb_note_extractor import row_entity_year_col

            if not _fs_label_match_ok(nt, nl):
                continue
            if headers:
                # Respect blank year slots — never fall back to a fixed
                # column that may hold the prior-year amount.
                pick = row_entity_year_col(cells, headers, year)
            else:
                pick = col
            if pick is not None and pick < len(cells):
                v = parse_number(cells[pick])
                if v is not None:
                    return v
    return None


def retry_missing_fs_labels(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    *,
    retry_labels: set[str] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {"retried": 0, "filled": 0, "still_missing": []}

    missing = [
        lbl
        for lbl in labels
        if values.get(lbl) is None and (retry_labels is None or lbl in retry_labels)
    ]
    if not missing:
        return values, stats

    log(f"  [annual-validate] Step A — retry {len(missing)} missing FS label(s)")
    for lbl in missing:
        stats["retried"] += 1
        val = aggressive_fs_lookup(fs_ext, year, lbl)
        if val is not None:
            values[lbl] = val
            stats["filled"] += 1
            log(f"    filled {lbl!r} = {val:,.0f}")
        else:
            stats["still_missing"].append(lbl)

    return values, stats


def verify_fs_value(
    fs_ext: DataExtractor,
    year: int,
    template_label: str,
    value: float | None,
    *,
    peer_values: dict[str, float | None] | None = None,
) -> float | None:
    """
    Re-read from stored tables. Only adopt the table value when it agrees with
    the current value — never overwrite a good figure with a conflicting table
    parse (PDF steps correct mismatches later).
    """
    if is_memorandum_fs_label(template_label):
        source = lookup_memorandum_from_tables(fs_ext, year, template_label)
    else:
        source = aggressive_fs_lookup(fs_ext, year, template_label)
    if source is None:
        return value
    if is_suspicious_fs_value(template_label, source, peer_values):
        return value
    if value is None:
        return source
    if annual_values_match(value, source) or values_match_report(value, source):
        return value
    return value


def verify_fs_values_from_tables(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    *,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {"verified": 0, "corrected": 0, "filled": 0}

    log(f"  [annual-validate] Step B — re-read {len(labels)} label(s) from tables")
    for lbl in labels:
        stats["verified"] += 1
        old = values.get(lbl)
        new = verify_fs_value(fs_ext, year, lbl, old, peer_values=values)
        if new != old:
            if old is None and new is not None:
                stats["filled"] += 1
            elif old is not None and new is not None:
                stats["corrected"] += 1
            values[lbl] = new

    return values, stats


def fill_memorandum_fs_values(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    *,
    pdf_index=None,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """Step C — employees / customer service centres (GROUP then BANK, tables + PDF)."""
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {"memorandum_filled": 0, "memorandum_labels": []}

    mem_labels = [lbl for lbl in labels if is_memorandum_fs_label(lbl)]
    if not mem_labels:
        return values, stats

    log(f"  [annual-validate] Step C — memorandum ({len(mem_labels)} label(s))")
    for lbl in mem_labels:
        current = values.get(lbl)
        table_val = lookup_memorandum_from_tables(fs_ext, year, lbl)
        pdf_val = None
        if pdf_index is not None:
            pdf_val = lookup_memorandum_from_pdf_index(
                pdf_index, lbl, section="sofp"
            )

        best: float | None = None
        for candidate in (table_val, pdf_val):
            if candidate is None:
                continue
            if not is_plausible_memorandum_count(lbl, candidate):
                continue
            if best is None:
                best = candidate

        if best is None:
            continue
        if current is not None and annual_values_match(current, best):
            continue
        values[lbl] = best
        stats["memorandum_filled"] += 1
        stats["memorandum_labels"].append(lbl)
        if current is None:
            log(f"    memorandum filled {lbl!r} = {best:,.0f}")
        else:
            log(f"    memorandum corrected {lbl!r}: {current} -> {best:,.0f}")

    return values, stats


def validate_and_retry_annual_fs_cells(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    retry_labels: set[str] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    log = log_fn or (lambda msg: print(msg, flush=True))
    company_slug = fs_ext.company_slug
    db = fs_ext.db

    all_stats: dict[str, Any] = {}

    values, s_a = retry_missing_fs_labels(
        fs_ext, year, labels, values, retry_labels=retry_labels, log_fn=log
    )
    all_stats["retry"] = s_a

    values, s_b = verify_fs_values_from_tables(fs_ext, year, labels, values, log_fn=log)
    all_stats["table_verify"] = s_b

    values, s_tax0 = reconcile_operating_profit_fs_tax(values, log_fn=log)
    all_stats["fs_tax_check_early"] = s_tax0

    values, s_c = fill_memorandum_fs_values(
        fs_ext, year, labels, values, log_fn=log
    )
    all_stats["memorandum"] = s_c

    values, s_d = cross_check_fs_values_against_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        log_fn=log,
    )
    all_stats["pdf"] = s_d

    # Step C2 — memorandum again now that PDF index exists from step D
    if s_d.get("pdf_used"):
        from comb_annual_fs_pdf_verify import build_annual_fs_pdf_index

        pdf_path = resolve_annual_pdf(db, company_slug, year)
        if pdf_path:
            manifest = {
                "fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}
            }
            pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)
            values, s_c2 = fill_memorandum_fs_values(
                fs_ext, year, labels, values, pdf_index=pdf_index, log_fn=log
            )
            all_stats["memorandum_pdf"] = s_c2

    values, s_e = final_fs_pdf_verification_pass(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        log_fn=log,
    )
    all_stats["pdf_final"] = s_e

    values, s_f = recheck_suspicious_fs_values_against_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        log_fn=log,
    )
    all_stats["suspicious"] = s_f

    values, s_g = refill_missing_fs_from_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        log_fn=log,
    )
    all_stats["refill"] = s_g

    values, s_h = strict_audit_filled_fs_against_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        log_fn=log,
    )
    all_stats["audit"] = s_h

    mismatch_targets = list(
        dict.fromkeys(
            (s_h.get("mismatch_labels") or [])
            + (s_g.get("refill_labels") or [])
            + (s_a.get("still_missing") or [])
        )
    )
    values, s_i = targeted_refind_fs_labels(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        target_labels=mismatch_targets if mismatch_targets else None,
        log_fn=log,
    )
    all_stats["refind"] = s_i

    # Final strict audit after targeted re-find
    values, s_h2 = strict_audit_filled_fs_against_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        log_fn=log,
    )
    all_stats["audit_final"] = s_h2

    from comb_annual_fs_ai_confirm import run_release_fs_validation_gate

    pdf_mismatch = set(
        (s_h.get("mismatch_labels") or []) + (s_h2.get("mismatch_labels") or [])
    )
    values, gate = run_release_fs_validation_gate(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        pdf_mismatch_labels=pdf_mismatch,
        use_ai_confirm=True,
        double_confirm=True,
        log_fn=log,
    )
    all_stats["release_gate"] = gate
    all_stats["fs_tax_check"] = (gate.get("fs_tax_check") or {}) if gate else {}
    all_stats["ai_confirm"] = (gate.get("ai_confirm") or {}) if gate else {}

    return values, all_stats


def validate_annual_fs_against_report(
    fs_ext: DataExtractor,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    retry_labels: set[str] | None = None,
    pdf_index=None,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Targeted annual FS validation (release path):

      A) Retry missing labels from financial_tables
      F) Re-check suspicious values against PDF
      G) Refill still-missing labels from PDF
      H) Strict audit — flag mismatches (high-risk deferred)
      I) Targeted re-find for missing / mismatched labels
      J) Release gate — rule check → AI confirm (double vision)

    Builds the annual FS PDF index at most once and reuses it across steps.
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    company_slug = fs_ext.company_slug
    db = fs_ext.db
    all_stats: dict[str, Any] = {}

    values, s_a = retry_missing_fs_labels(
        fs_ext, year, labels, values, retry_labels=retry_labels, log_fn=log
    )
    all_stats["retry"] = s_a

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    shared_index = pdf_index
    if shared_index is None and pdf_path and pdf_path.exists():
        log("  [annual-validate] Building shared FS PDF index (one pass)…")
        shared_index = build_annual_fs_pdf_index(
            pdf_path,
            {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}},
            year,
        )

    values, s_f = recheck_suspicious_fs_values_against_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        pdf_index=shared_index,
        log_fn=log,
    )
    all_stats["suspicious"] = s_f

    values, s_g = refill_missing_fs_from_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        pdf_index=shared_index,
        log_fn=log,
    )
    all_stats["refill"] = s_g

    values, s_h = strict_audit_filled_fs_against_pdf(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        pdf_index=shared_index,
        log_fn=log,
    )
    all_stats["audit"] = s_h

    mismatch_targets = list(
        dict.fromkeys(
            (s_h.get("mismatch_labels") or [])
            + [
                lbl
                for lbl in (s_g.get("refill_labels") or [])
                if is_suspicious_fs_value(lbl, values.get(lbl), values, report_year=year)
            ]
            + (s_a.get("still_missing") or [])
        )
    )
    # Also re-find labels that are still missing after refill.
    for lbl in labels:
        if values.get(lbl) is None and lbl not in mismatch_targets:
            mismatch_targets.append(lbl)
    if mismatch_targets:
        values, s_i = targeted_refind_fs_labels(
            db,
            company_slug,
            year,
            labels,
            values,
            fs_sections,
            target_labels=mismatch_targets,
            pdf_index=shared_index,
            log_fn=log,
        )
        all_stats["refind"] = s_i

    # Release gate: rule check → AI confirm high-risk / deferred / missing cells.
    from comb_annual_fs_ai_confirm import run_release_fs_validation_gate

    pdf_mismatch = set(s_h.get("mismatch_labels") or [])
    values, gate = run_release_fs_validation_gate(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        pdf_index=shared_index,
        pdf_mismatch_labels=pdf_mismatch,
        use_ai_confirm=True,
        double_confirm=True,
        log_fn=log,
    )
    all_stats["release_gate"] = gate
    s_tax = (gate.get("fs_tax_check") or {}) if isinstance(gate, dict) else {}
    ai_stats = (gate.get("ai_confirm") or {}) if isinstance(gate, dict) else {}
    all_stats["fs_tax_check"] = s_tax
    all_stats["ai_confirm"] = ai_stats

    all_stats["still_missing"] = [
        lbl for lbl in labels if values.get(lbl) is None
    ]
    all_stats["recovered"] = (
        int(s_a.get("filled") or 0)
        + int(s_f.get("suspicious_corrected") or 0)
        + int(s_g.get("refilled") or 0)
        + int((all_stats.get("refind") or {}).get("refind_found") or 0)
        + int(ai_stats.get("filled") or 0)
        + (1 if s_tax.get("corrected") else 0)
    )
    all_stats["corrected"] = (
        int(s_f.get("suspicious_corrected") or 0)
        + int(s_h.get("corrected") or 0)
        + int(ai_stats.get("corrected") or 0)
        + (1 if s_tax.get("corrected") else 0)
    )
    all_stats["ai_confirmed"] = int(ai_stats.get("confirmed") or 0)
    return values, all_stats
