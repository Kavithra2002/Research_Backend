"""
comb_quarterly_pdf_verify.py
============================
Cross-check quarterly DB values against the original quarterly report PDF.

When financial_tables holds a wrong figure (wrong column parse, OCR drift, etc.),
this module re-reads the GROUP income-statement page and corrects mismatches
before values are saved to comb_workbook_data.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from comb_note_extractor import (
    _PERIOD_QUARTER_RE,
    _is_change_column,
    _period_sections,
    _words_table_rows,
    _year_at_column,
)
from extraction_aliases_store import patterns_for_label
from generate_comb_model import QuarterlyExtractor, norm_label, parse_number

GROUP_INCOME_PAGE_RE = re.compile(r"income\s+statement\s*-\s*group", re.I)
QUARTER_COL_DEFAULT = 4

# Template subsection / header rows — never store numeric values from PDF.
QUARTERLY_NON_DATA_LABELS = frozenset(
    {
        "profit attributable to",
    }
)

# Rows where small decimals are valid (not Rs.'000 amounts).
QUARTERLY_SMALL_VALUE_LABELS = frozenset(
    {
        "earnings per share",
    }
)

# Rows that may legitimately be below Rs.'000 scale.
QUARTERLY_LOW_AMOUNT_LABELS = frozenset(
    {
        "add/(less): share of profit/(loss) of associate, net of tax",
    }
)


def resolve_quarterly_pdf(db, company_slug: str, year: int, quarter: str) -> Path | None:
    """Locate the quarterly report PDF path stored on financial_tables."""
    doc = db.financial_tables.find_one(
        {
            "company_slug": company_slug,
            "report_type": "quarterly",
            "year": year,
            "quarter": quarter,
            "statement_key": {"$regex": "^income_statement$", "$options": "i"},
            "source_pdf": {"$exists": True, "$ne": ""},
        },
        {"source_pdf": 1},
    )
    if not doc:
        doc = db.financial_tables.find_one(
            {
                "company_slug": company_slug,
                "report_type": "quarterly",
                "year": year,
                "quarter": quarter,
                "source_pdf": {"$exists": True, "$ne": ""},
            },
            {"source_pdf": 1},
        )
    if not doc or not doc.get("source_pdf"):
        return None
    path = Path(str(doc["source_pdf"]))
    return path if path.is_file() else None


def _is_data_row(row: list[str], value_col: int) -> bool:
    if len(row) <= value_col:
        return False
    nums = sum(1 for c in row[1 : value_col + 3] if parse_number(c) is not None)
    return nums >= 2


def _label_text_from_row(row: list[str]) -> str:
    parts: list[str] = []
    for ci in range(min(2, len(row))):
        cell = str(row[ci]).strip()
        if not cell:
            continue
        if parse_number(cell) is not None:
            continue
        if re.fullmatch(r"[\d\.,\s()%\-]+", cell):
            continue
        parts.append(cell)
    return " ".join(parts).strip()


def _merge_income_statement_rows(
    raw_rows: list[list[str]],
    *,
    value_col: int = QUARTER_COL_DEFAULT,
) -> list[tuple[str, float]]:
    """Merge multi-line labels and pair each data row with its description."""
    merged: list[tuple[str, float]] = []
    pending_label = ""

    for row in raw_rows:
        if not row:
            continue
        chunk = _label_text_from_row(row)
        if chunk:
            pending_label = f"{pending_label} {chunk}".strip()

        if not _is_data_row(row, value_col):
            continue

        val = parse_number(row[value_col])
        if val is None:
            continue

        label = pending_label or chunk
        if label:
            merged.append((label, val))
        pending_label = ""

    return merged


def _pdf_income_header_rows(raw_rows: list[list[str]]) -> list[list[str]]:
    """Collect banner, date, and unit rows above the first data line."""
    header: list[list[str]] = []
    for row in raw_rows[:8]:
        if not row:
            continue
        text = " ".join(str(c) for c in row).lower()
        if re.search(r"months?\s+ended|quarter\s+ended", text):
            header.append(row)
            continue
        if re.search(r"\d{2}\.\d{2}\.\d{4}", text) or "change" in text:
            header.append(row)
            continue
        if "rs" in text and "000" in text:
            header.append(row)
            continue
        if text.strip() in {"%", "change %"}:
            header.append(row)
            continue
        if row[0] and parse_number(row[0]) is None and not re.match(
            r"^income statement", str(row[0]), re.I
        ):
            break
    return header


def _detect_quarter_col(header_rows: list[list[str]], year: int) -> int:
    """
    Pick the current-year Rs.'000 column under 'For the quarter ended'.

    CSE quarterly tables use: [label, 6mo Y, 6mo Y-1, ch%, quarter Y, quarter Y-1, ch%]
  so the quarter current-year column is the first date column in the quarter block.
    """
    year_s = str(year)

    sections = _period_sections(header_rows)
    quarter_sections = [
        s
        for s in sections
        if _PERIOD_QUARTER_RE.search(str(s.get("label") or ""))
    ]
    for section in quarter_sections:
        for ci in section.get("date_cols", []):
            if _year_at_column(header_rows, ci) == year_s:
                return ci
        # PDF word-grid may not expose years in header rows — use first Rs.'000 col.
        for ci in range(int(section["start"]), int(section["end"]) + 1):
            if _is_change_column(header_rows, ci):
                continue
            unit = ""
            for hrow in header_rows:
                if ci < len(hrow):
                    unit = str(hrow[ci]).strip().lower()
                    if unit:
                        break
            if unit == "%":
                continue
            if "rs" in unit and "000" in unit:
                if _year_at_column(header_rows, ci) in (None, year_s):
                    return ci

    for hrow in header_rows:
        year_cols = [
            ci
            for ci, cell in enumerate(hrow)
            if re.search(rf"\d{{2}}\.\d{{2}}\.{year_s}", str(cell))
            and not _is_change_column(header_rows, ci)
        ]
        if len(year_cols) >= 2:
            return year_cols[1]
        if len(year_cols) == 1:
            return year_cols[0]

    # Quarter block is usually cols 4/5/6 when six-month block is 1/2/3.
    return QUARTER_COL_DEFAULT


def _find_group_income_page(pdf: Any) -> int | None:
    for idx, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if GROUP_INCOME_PAGE_RE.search(text):
            return idx
    return None


def build_quarterly_pdf_index(
    pdf_path: Path,
    year: int,
    *,
    entity: str = "group",
) -> dict[str, float]:
    """
    Build norm_label -> quarter-ended value index from the GROUP income statement PDF.
    """
    if pdfplumber is None or not pdf_path.is_file():
        return {}

    index: dict[str, float] = {}

    with pdfplumber.open(str(pdf_path)) as pdf:
        page_idx = _find_group_income_page(pdf)
        if page_idx is None:
            return {}
        page = pdf.pages[page_idx]
        raw_rows = _words_table_rows(page)
        if not raw_rows:
            return {}

        header_rows = _pdf_income_header_rows(raw_rows)
        value_col = _detect_quarter_col(header_rows, year)
        merged = _merge_income_statement_rows(raw_rows, value_col=value_col)

        for label, val in merged:
            nl = norm_label(label)
            if nl and nl not in index:
                index[nl] = val

    return index


def lookup_in_pdf_index(
    index: dict[str, float],
    template_label: str,
    aliases: dict[str, list[str]] | None = None,
) -> float | None:
    """Match a template label to a value in the PDF index."""
    alias_map = aliases or QuarterlyExtractor.QUARTERLY_LABEL_ALIASES
    patterns = patterns_for_label(
        template_label,
        alias_map,
        "quarterly",
        default_to_label=True,
    )

    for pat in patterns:
        np = norm_label(pat)
        if np in index:
            return index[np]
        for key, val in index.items():
            if np in key or key in np:
                return val

    nt = norm_label(template_label)
    if nt in index:
        return index[nt]

    # Profit rows are often labelled Profit/(loss) before income tax in PDFs.
    if "profit" in nt and "before" in nt and "income tax" in nt:
        for key, val in index.items():
            if (
                "profit" in key
                and "before" in key
                and "income tax" in key
                and "operating" not in key
            ):
                return val

    # EPS rows are labelled Basic/Diluted earnings per ordinary share in PDFs.
    if "earnings per share" in nt or nt == "earnings per share":
        for key, val in index.items():
            if "basic earnings per ordinary share" in key:
                return val
            if "earnings per share" in key and "basic" in key:
                return val

    # Operating-profit rows are often split across lines in PDF parses.
    if "operating profit" in nt and "before" in nt:
        for key, val in index.items():
            if "operating profit" in key and "before" in key and "tax" in key:
                return val
    if "operating profit" in nt and "after" in nt:
        for key, val in index.items():
            if "operating profit" in key and "after" in key and "tax" in key:
                return val

    for key, val in index.items():
        if nt in key or key in nt:
            return val
    return None


def values_match_report(actual: float | None, expected: float | None) -> bool:
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    tol = max(1.0, abs(expected) * 0.0001)
    return abs(actual - expected) <= tol


def _peer_amount_scale(values: dict[str, float | None], label: str) -> float:
    """Typical magnitude of filled Rs.'000 rows (ignoring EPS and headers)."""
    peers: list[float] = []
    skip = norm_label(label)
    for lbl, val in values.items():
        if val is None:
            continue
        nl = norm_label(lbl)
        if nl in QUARTERLY_NON_DATA_LABELS or nl in QUARTERLY_SMALL_VALUE_LABELS:
            continue
        if nl in QUARTERLY_LOW_AMOUNT_LABELS or "share of profit" in nl:
            continue
        av = abs(float(val))
        if av >= 10_000:
            peers.append(av)
    if not peers:
        return 0.0
    peers.sort()
    return peers[len(peers) // 2]


def is_suspicious_quarterly_value(
    label: str,
    value: float | None,
    values: dict[str, float | None] | None = None,
) -> bool:
    """
    Flag values that look like Change-% columns (e.g. 5.36) sitting among
    millions-scale P&L figures.
    """
    if value is None:
        return False
    nl = norm_label(label)
    if nl in QUARTERLY_NON_DATA_LABELS or nl in QUARTERLY_SMALL_VALUE_LABELS:
        return False
    if nl in QUARTERLY_LOW_AMOUNT_LABELS or "share of profit" in nl:
        return False

    v = float(value)
    if abs(v) >= 10_000:
        return False

    peer_scale = _peer_amount_scale(values or {}, label)
    if peer_scale < 50_000:
        return False

    # Decimals in the hundreds/thousands range while peers are in millions.
    if abs(v) < 1_000:
        return True

    # Whole numbers below 10k when most P&L lines are 100k+ are still suspect.
    if abs(v) < 10_000 and peer_scale >= 500_000:
        return True

    return False


def recheck_suspicious_values_against_pdf(
    q_ext: QuarterlyExtractor,
    quarter: str,
    labels: list[str],
    values: dict[str, float | None],
    *,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Re-read suspicious cells from the GROUP quarter-ended PDF column only.
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "suspicious_found": 0,
        "suspicious_corrected": 0,
        "suspicious_corrections": [],
    }

    suspicious = [
        lbl
        for lbl in labels
        if norm_label(lbl) not in QUARTERLY_NON_DATA_LABELS
        and is_suspicious_quarterly_value(lbl, values.get(lbl), values)
    ]
    if not suspicious:
        return values, stats

    stats["suspicious_found"] = len(suspicious)
    log(
        f"  [quarterly-validate] Step F — {len(suspicious)} suspicious value(s); "
        f"re-checking GROUP quarter-ended PDF column"
    )

    pdf_path = resolve_quarterly_pdf(q_ext.db, q_ext.company_slug, q_ext.year, quarter)
    if not pdf_path:
        for lbl in suspicious:
            log(f"    suspicious {lbl!r} = {values.get(lbl)} (no PDF to verify)")
        return values, stats

    pdf_index = build_quarterly_pdf_index(pdf_path, q_ext.year)
    if not pdf_index:
        return values, stats

    aliases = QuarterlyExtractor.QUARTERLY_LABEL_ALIASES
    for lbl in suspicious:
        current = values.get(lbl)
        pdf_val = lookup_in_pdf_index(pdf_index, lbl, aliases)
        if pdf_val is None or is_suspicious_quarterly_value(lbl, pdf_val, values):
            log(
                f"    suspicious {lbl!r} = {current} — PDF re-check could not "
                f"find a quarter-ended GROUP amount"
            )
            continue
        if values_match_report(current, pdf_val):
            continue
        values[lbl] = pdf_val
        stats["suspicious_corrected"] += 1
        stats["suspicious_corrections"].append(
            {
                "label": lbl,
                "from": current,
                "to": pdf_val,
                "source": "pdf_suspicious_recheck",
            }
        )
        log(
            f"    suspicious corrected {lbl!r}: {current} -> {pdf_val:,.0f} "
            f"(quarter-ended GROUP PDF)"
        )

    return values, stats


def cross_check_values_against_pdf(
    q_ext: QuarterlyExtractor,
    quarter: str,
    labels: list[str],
    values: dict[str, float | None],
    *,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Compare every filled value with the source PDF and replace mismatches.
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "pdf_used": False,
        "pdf_compared": 0,
        "pdf_corrected": 0,
        "pdf_corrections": [],
    }

    pdf_path = resolve_quarterly_pdf(q_ext.db, q_ext.company_slug, q_ext.year, quarter)
    if not pdf_path:
        return values, stats

    pdf_index = build_quarterly_pdf_index(pdf_path, q_ext.year)
    if not pdf_index:
        return values, stats

    stats["pdf_used"] = True
    stats["pdf_path"] = str(pdf_path)
    aliases = QuarterlyExtractor.QUARTERLY_LABEL_ALIASES

    for lbl in labels:
        if norm_label(lbl) in QUARTERLY_NON_DATA_LABELS:
            continue
        pdf_val = lookup_in_pdf_index(pdf_index, lbl, aliases)
        if pdf_val is None:
            continue
        nl = norm_label(lbl)
        current = values.get(lbl)
        if nl in QUARTERLY_SMALL_VALUE_LABELS:
            if pdf_val is not None and abs(pdf_val) < 1_000:
                if current is None or is_suspicious_quarterly_value(
                    lbl, current, values
                ):
                    values[lbl] = pdf_val
                    stats["pdf_corrected"] += 1
                    stats["pdf_corrections"].append(
                        {
                            "label": lbl,
                            "from": current,
                            "to": pdf_val,
                            "source": "pdf_eps",
                        }
                    )
                    log(f"    pdf filled {lbl!r} = {pdf_val}")
            continue
        if is_suspicious_quarterly_value(lbl, pdf_val, values):
            continue
        stats["pdf_compared"] += 1
        current = values.get(lbl)
        if values_match_report(current, pdf_val):
            if current is None:
                values[lbl] = pdf_val
                stats["pdf_corrected"] += 1
                stats["pdf_corrections"].append(
                    {"label": lbl, "from": None, "to": pdf_val, "source": "pdf"}
                )
                log(f"    pdf filled {lbl!r} = {pdf_val:,.0f}")
            elif is_suspicious_quarterly_value(lbl, current, values):
                values[lbl] = pdf_val
                stats["pdf_corrected"] += 1
                stats["pdf_corrections"].append(
                    {
                        "label": lbl,
                        "from": current,
                        "to": pdf_val,
                        "source": "pdf_suspicious",
                    }
                )
                log(
                    f"    pdf fixed suspicious {lbl!r}: {current} -> {pdf_val:,.0f}"
                )
            continue

        values[lbl] = pdf_val
        stats["pdf_corrected"] += 1
        stats["pdf_corrections"].append(
            {"label": lbl, "from": current, "to": pdf_val, "source": "pdf"}
        )
        log(
            f"    pdf corrected {lbl!r}: {current} -> {pdf_val:,.0f} "
            f"(report PDF is authoritative)"
        )

    return values, stats


def reconcile_expense_block(
    values: dict[str, float | None],
    *,
    log_fn=None,
) -> int:
    """
    Derive Other operating expenses only when it is still missing but
    parent + child rows are available. Never override values already extracted
    or corrected from the report PDF.
    """
    log = log_fn or (lambda _msg: None)
    parent = values.get("Less: Expenses")
    personnel = values.get("Personnel expenses")
    depreciation = values.get("Depreciation and amortisation")
    other = values.get("Other operating expenses")

    if other is not None:
        return 0

    if parent is None or personnel is None or depreciation is None:
        return 0

    expected_other = parent - personnel - depreciation
    values["Other operating expenses"] = expected_other
    log(f"    derived Other operating expenses = {expected_other:,.0f}")
    return 1


def fill_missing_from_pdf_index(
    labels: list[str],
    values: dict[str, float | None],
    pdf_index: dict[str, float],
    *,
    log_fn=None,
) -> int:
    """Fill cells that are still empty when the PDF has the value."""
    log = log_fn or (lambda _msg: None)
    aliases = QuarterlyExtractor.QUARTERLY_LABEL_ALIASES
    filled = 0
    for lbl in labels:
        if norm_label(lbl) in QUARTERLY_NON_DATA_LABELS:
            continue
        if values.get(lbl) is not None:
            continue
        pdf_val = lookup_in_pdf_index(pdf_index, lbl, aliases)
        if pdf_val is None:
            continue
        values[lbl] = pdf_val
        filled += 1
        log(f"    pdf filled missing {lbl!r} = {pdf_val:,.0f}")
    return filled


def final_pdf_verification_pass(
    q_ext: QuarterlyExtractor,
    quarter: str,
    labels: list[str],
    values: dict[str, float | None],
    *,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Build PDF index once, fill any remaining gaps, then compare every value
    again and replace mismatches with the report figure.
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "pdf_used": False,
        "pdf_compared": 0,
        "pdf_filled": 0,
        "pdf_corrected": 0,
        "pdf_corrections": [],
    }

    pdf_path = resolve_quarterly_pdf(q_ext.db, q_ext.company_slug, q_ext.year, quarter)
    if not pdf_path:
        log("  [quarterly-validate] no source PDF — skip final PDF verification")
        return values, stats

    pdf_index = build_quarterly_pdf_index(pdf_path, q_ext.year)
    if not pdf_index:
        log("  [quarterly-validate] could not parse PDF income statement")
        return values, stats

    stats["pdf_used"] = True
    stats["pdf_path"] = str(pdf_path)

    filled = fill_missing_from_pdf_index(labels, values, pdf_index, log_fn=log)
    stats["pdf_filled"] = filled

    aliases = QuarterlyExtractor.QUARTERLY_LABEL_ALIASES
    for lbl in labels:
        if norm_label(lbl) in QUARTERLY_NON_DATA_LABELS:
            continue
        nl = norm_label(lbl)
        pdf_val = lookup_in_pdf_index(pdf_index, lbl, aliases)
        if pdf_val is None:
            continue
        if nl in QUARTERLY_SMALL_VALUE_LABELS:
            current = values.get(lbl)
            if abs(pdf_val) < 1_000 and (
                current is None
                or is_suspicious_quarterly_value(lbl, current, values)
                or not values_match_report(current, pdf_val)
            ):
                values[lbl] = pdf_val
                stats["pdf_corrected"] += 1
                stats["pdf_corrections"].append(
                    {"label": lbl, "from": current, "to": pdf_val, "source": "pdf_final_eps"}
                )
                log(f"    pdf final check corrected {lbl!r}: {current} -> {pdf_val}")
            continue
        if is_suspicious_quarterly_value(lbl, pdf_val, values):
            continue
        stats["pdf_compared"] += 1
        current = values.get(lbl)
        if values_match_report(current, pdf_val):
            continue
        values[lbl] = pdf_val
        stats["pdf_corrected"] += 1
        stats["pdf_corrections"].append(
            {"label": lbl, "from": current, "to": pdf_val, "source": "pdf_final"}
        )
        log(
            f"    pdf final check corrected {lbl!r}: {current} -> {pdf_val:,.0f}"
        )

    return values, stats
