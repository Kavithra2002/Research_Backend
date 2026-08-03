"""
comb_quarterly_validate.py
==========================
Post-extraction validation for DB quarterly cell capture.

Uses quarter-ended columns only (never six/nine-month YTD), computes parent
subtotals when report rows are blank (e.g. Less: Expenses), and retries
missing values before DB save.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from comb_note_extractor import find_quarter_only_value_cols
from comb_quarterly_pdf_verify import (
    QUARTERLY_NON_DATA_LABELS,
    cross_check_values_against_pdf,
    final_pdf_verification_pass,
    is_suspicious_quarterly_value,
    recheck_suspicious_values_against_pdf,
    reconcile_expense_block,
    values_match_report,
)
from extraction_aliases_store import patterns_for_label
from generate_comb_model import QuarterlyExtractor, norm_label, parse_number

MAX_QUARTERLY_CELL_VALIDATION_ROUNDS = 3

QUARTERLY_STATEMENT_RE = re.compile(
    r"^(?:consolidated_|company_)?(?:income_statement|profit_loss|comprehensive_income)",
    re.I,
)

EXPENSE_PARENT_NORMS = frozenset({"less expenses", "expenses"})
EXPENSE_CHILD_NORMS = frozenset(
    {
        "personnel expenses",
        "depreciation and amortisation",
        "depreciation and amortization",
        "other operating expenses",
    }
)

LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def _doc_body_rows(doc: dict) -> tuple[list[list[str]], list[list[str]]]:
    headers = doc.get("header_rows") or []
    body_rows: list[list[str]] = []
    for row in doc.get("rows") or []:
        cells = row.get("cells") if isinstance(row, dict) else row
        if cells:
            body_rows.append([str(c) for c in cells])
    return headers, body_rows


def quarterly_value_column_candidates(
    doc: dict,
    year: int,
    *,
    entity_column: str = "group",
) -> list[int]:
    """Quarter-ended columns for the target year, best first."""
    headers, body_rows = _doc_body_rows(doc)
    return find_quarter_only_value_cols(headers, body_rows, year, entity_column)


def _index_from_doc_column(doc: dict, col: int) -> dict[str, float]:
    index: dict[str, float] = {}
    for row in doc.get("rows") or []:
        cells = row.get("cells") or []
        if not cells or not cells[0]:
            continue
        label = str(cells[0]).strip()
        if col >= len(cells):
            continue
        val = parse_number(cells[col])
        if val is None:
            continue
        nl = norm_label(label)
        if nl and nl not in index:
            index[nl] = val
    return index


def _compute_expenses_subtotal(doc: dict, col: int) -> float | None:
    """Sum personnel + depreciation + other operating when parent row is blank."""
    child_vals: list[float] = []
    in_block = False

    for row in doc.get("rows") or []:
        cells = row.get("cells") or []
        if not cells or not cells[0]:
            continue
        nl = norm_label(str(cells[0]).strip())
        if nl in EXPENSE_PARENT_NORMS:
            in_block = True
            parent_val = parse_number(cells[col]) if col < len(cells) else None
            if parent_val is not None:
                return parent_val
            continue
        if not in_block:
            continue
        if nl in EXPENSE_CHILD_NORMS:
            val = parse_number(cells[col]) if col < len(cells) else None
            if val is not None:
                child_vals.append(val)
            continue
        if nl and nl not in EXPENSE_CHILD_NORMS:
            break

    if len(child_vals) >= 2:
        return sum(child_vals)
    return None


def _token_overlap_lookup(
    index: dict[str, float],
    template_norm: str,
    *,
    min_ratio: float = 0.75,
) -> float | None:
    tmpl_tokens = set(template_norm.split())
    if len(tmpl_tokens) < 3:
        return None
    best_ratio = 0.0
    best_val: float | None = None
    for key, val in index.items():
        key_tokens = set(key.split())
        if not key_tokens:
            continue
        overlap = len(tmpl_tokens & key_tokens) / len(tmpl_tokens)
        if overlap >= min_ratio and overlap > best_ratio:
            best_ratio = overlap
            best_val = val
    return best_val


def lookup_label_in_index(
    index: dict[str, float],
    template_label: str,
    aliases: dict[str, list[str]],
    *,
    allow_token_overlap: bool = False,
) -> float | None:
    patterns = patterns_for_label(
        template_label,
        aliases,
        "quarterly",
        default_to_label=True,
    )
    for pat in patterns:
        np = norm_label(pat)
        if np in index:
            return index[np]
        if np in EXPENSE_PARENT_NORMS:
            continue
        for key, val in index.items():
            if np in key or key in np:
                return val

    nt = norm_label(template_label)
    if nt in index:
        return index[nt]

    # Exact-only for short parent labels (avoid matching "personnel expenses", etc.)
    if nt in EXPENSE_PARENT_NORMS:
        return None

    for key, val in index.items():
        if nt in key or key in nt:
            return val

    if allow_token_overlap:
        return _token_overlap_lookup(index, nt)
    return None


def _is_expenses_template_label(template_label: str) -> bool:
    return norm_label(template_label) in EXPENSE_PARENT_NORMS


def _doc_sort_key(doc: dict) -> tuple[int, int]:
    sk = str(doc.get("statement_key") or "")
    if sk == "income_statement":
        priority = 100
    elif sk.startswith("income_statement"):
        priority = 80
    elif "comprehensive" in sk:
        priority = 60
    else:
        priority = 10
    return (priority, len(doc.get("rows") or []))


def all_quarterly_statement_docs(q_ext: QuarterlyExtractor, quarter: str) -> list[dict]:
    query = {
        "company_slug": q_ext.company_slug,
        "report_type": "quarterly",
        "year": q_ext.year,
        "quarter": quarter,
        "statement_key": {"$regex": QUARTERLY_STATEMENT_RE.pattern, "$options": "i"},
    }
    docs = list(q_ext.db.financial_tables.find(query))
    if not docs:
        docs = list(
            q_ext.db.financial_tables.find(
                {
                    **query,
                    "statement_key": {"$regex": "^income_statement", "$options": "i"},
                }
            )
        )
    return sorted(docs, key=_doc_sort_key, reverse=True)


def extract_value_from_doc(
    doc: dict,
    year: int,
    template_label: str,
    aliases: dict[str, list[str]],
    *,
    entity_column: str = "group",
) -> float | None:
    """Extract one template label from a statement using quarter-only columns."""
    for col in quarterly_value_column_candidates(doc, year, entity_column=entity_column):
        if _is_expenses_template_label(template_label):
            subtotal = _compute_expenses_subtotal(doc, col)
            if subtotal is not None:
                return subtotal

        index = _index_from_doc_column(doc, col)
        val = lookup_label_in_index(index, template_label, aliases, allow_token_overlap=True)
        if val is not None:
            return val

        # Report may label the parent row "Expenses" without "Less:"
        if _is_expenses_template_label(template_label):
            for parent_key in EXPENSE_PARENT_NORMS:
                if parent_key in index:
                    return index[parent_key]

    return None


def aggressive_quarterly_lookup(
    q_ext: QuarterlyExtractor,
    quarter: str,
    template_label: str,
    *,
    allow_token_overlap: bool = True,
) -> float | None:
    """Try every quarterly income doc; quarter columns only; sum expense subtotals."""
    aliases = QuarterlyExtractor.QUARTERLY_LABEL_ALIASES
    for doc in all_quarterly_statement_docs(q_ext, quarter):
        val = extract_value_from_doc(
            doc,
            q_ext.year,
            template_label,
            aliases,
            entity_column="group",
        )
        if val is not None:
            return val
    return None


def source_has_quarterly_value(
    q_ext: QuarterlyExtractor,
    quarter: str,
    template_label: str,
) -> bool:
    return aggressive_quarterly_lookup(q_ext, quarter, template_label) is not None


def verify_quarterly_value(
    q_ext: QuarterlyExtractor,
    quarter: str,
    template_label: str,
    value: float,
    *,
    peer_values: dict[str, float | None] | None = None,
) -> float:
    """Re-read from report tables; reject suspicious Change-% bleed."""
    source = aggressive_quarterly_lookup(q_ext, quarter, template_label)
    if source is None:
        return value
    if is_suspicious_quarterly_value(template_label, source, peer_values):
        return value
    if values_match_report(value, source):
        return source
    if is_suspicious_quarterly_value(template_label, value, peer_values):
        return source
    return source


def validate_and_retry_quarterly_cells(
    q_ext: QuarterlyExtractor,
    quarter: str,
    labels: list[str],
    values: dict[str, float | None],
    *,
    retry_labels: set[str] | None = None,
    max_rounds: int = MAX_QUARTERLY_CELL_VALIDATION_ROUNDS,
    log_fn: LogFn | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Identify missing cells, retry extraction, reconcile expense subtotals,
    then run two PDF verification passes against the source report before save.
    """
    log = log_fn or _default_log
    active = retry_labels if retry_labels is not None else set(labels)
    stats: dict[str, Any] = {
        "initial_filled": sum(1 for lbl in labels if values.get(lbl) is not None),
        "initial_missing": sum(1 for lbl in labels if values.get(lbl) is None),
        "recovered": 0,
        "corrected": 0,
        "still_missing": 0,
        "rounds": 0,
        "missing_labels": [],
        "pdf_corrected": 0,
        "pdf_filled": 0,
    }

    log("  [quarterly-validate] Step A — retry missing values from stored tables")

    for round_num in range(1, max_rounds + 1):
        gaps = list(
            dict.fromkeys(
                lbl
                for lbl in labels
                if lbl in active
                and values.get(lbl) is None
                and source_has_quarterly_value(q_ext, quarter, lbl)
            )
        )
        if not gaps:
            break

        stats["rounds"] = round_num
        log(
            f"  [quarterly-validate] round {round_num}/{max_rounds}: "
            f"retrying {len(gaps)} missing label(s) found in source report"
        )
        q_ext.clear_cache(quarter)

        for lbl in gaps:
            val = aggressive_quarterly_lookup(q_ext, quarter, lbl)
            if val is not None:
                values[lbl] = val
                stats["recovered"] += 1
                log(f"    recovered {lbl!r} = {val:,.4f}".rstrip("0").rstrip("."))

    q_ext.clear_cache(quarter)
    log("  [quarterly-validate] Step B — re-read filled cells from stored tables")
    for lbl in labels:
        val = values.get(lbl)
        if val is None:
            continue
        verified = verify_quarterly_value(
            q_ext, quarter, lbl, val, peer_values=values
        )
        if verified != val:
            values[lbl] = verified
            stats["corrected"] += 1
            log(f"    table corrected {lbl!r}: {val} -> {verified}")

    log("  [quarterly-validate] Step C — derive missing expense subtotals")
    derived = reconcile_expense_block(values, log_fn=log)
    if derived:
        stats["reconciled_expenses"] = derived

    log("  [quarterly-validate] Step D — compare every value with source report PDF")
    values, pdf_stats = cross_check_values_against_pdf(
        q_ext, quarter, labels, values, log_fn=log
    )
    stats["pdf"] = pdf_stats
    stats["pdf_corrected"] += int(pdf_stats.get("pdf_corrected", 0) or 0)

    log("  [quarterly-validate] Step E — final PDF fill + second comparison pass")
    values, pdf_final = final_pdf_verification_pass(
        q_ext, quarter, labels, values, log_fn=log
    )
    stats["pdf_final"] = pdf_final
    stats["pdf_filled"] += int(pdf_final.get("pdf_filled", 0) or 0)
    stats["pdf_corrected"] += int(pdf_final.get("pdf_corrected", 0) or 0)
    stats["corrected"] += int(pdf_final.get("pdf_corrected", 0) or 0)

    log("  [quarterly-validate] Step F — detect suspicious values and re-check PDF")
    values, suspicious_stats = recheck_suspicious_values_against_pdf(
        q_ext, quarter, labels, values, log_fn=log
    )
    stats["suspicious"] = suspicious_stats
    stats["corrected"] += int(suspicious_stats.get("suspicious_corrected", 0) or 0)
    stats["pdf_corrected"] += int(suspicious_stats.get("suspicious_corrected", 0) or 0)

    for lbl in labels:
        if norm_label(lbl) in QUARTERLY_NON_DATA_LABELS:
            values[lbl] = None

    stats["still_missing"] = sum(1 for lbl in labels if values.get(lbl) is None)
    stats["missing_labels"] = [lbl for lbl in labels if values.get(lbl) is None]
    stats["final_filled"] = sum(1 for lbl in labels if values.get(lbl) is not None)
    log(
        f"  [quarterly-validate] done — filled {stats['final_filled']}/{len(labels)}, "
        f"missing {stats['still_missing']}, pdf corrections {stats['pdf_corrected']}"
    )
    return values, stats
