"""
comb_annual_fs_pdf_verify.py
============================
Cross-check annual FS DB values against the GROUP column in the source
annual report PDF (pdfplumber only — no OpenAI credits).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

from comb_annual_memorandum import (
    is_memorandum_fs_label,
    is_plausible_memorandum_count,
    lookup_memorandum_from_pdf_index,
    merge_memorandum_into_pdf_index,
)
from comb_annual_highlights import (
    find_financial_highlight_pages,
    is_highlight_fs_label,
    lookup_highlight_fs_value,
)
from comb_fs_pdf_extract import (
    FsPdfIndex,
    _best_plumber_index,
    build_fs_section_map,
    find_memorandum_pages,
    find_statement_pages,
)
from comb_note_extractor import resolve_annual_pdf
from comb_quarterly_pdf_verify import is_suspicious_quarterly_value
from extraction_aliases_store import patterns_for_label
from generate_comb_model import LABEL_ALIASES, norm_label, parse_number

FS_SMALL_VALUE_LABELS = frozenset(
    {
        "earnings per share",
        "dividend per share",
        "diviend per share",
        "basic earnings per ordinary share rs",
        "diluted earnings per ordinary share rs",
        "net assets value per ordinary share",
        "net assets value per ordinary share rs",
        "net assets value per share",
        "net assets value per share rs",
    }
)

FS_RATIO_LABELS = frozenset(
    {
        "cash total assets",  # norm_label strips "/"
        "number of employees",
        "number of customer service centres",
    }
)

# Calendar years that routinely appear as FS column headers (e.g. 2018 / 2019).
_YEAR_LIKE_MIN = 2000.0
_YEAR_LIKE_MAX = 2035.0


def is_year_like_amount(value: float | None, *, report_year: int | None = None) -> bool:
    """True when a parsed 'amount' is really a year header / page-era marker."""
    if value is None:
        return False
    v = float(value)
    if abs(v - round(v)) > 1e-9:
        return False
    iv = int(round(abs(v)))
    if report_year is not None and iv in {report_year, report_year - 1, report_year + 1}:
        return True
    # Bare calendar years in statements (not headcount / branch counts).
    return _YEAR_LIKE_MIN <= iv <= _YEAR_LIKE_MAX


def annual_values_match(actual: float | None, expected: float | None) -> bool:
    """
    Stricter than quarterly matching — catches digit transpositions in Rs.'000
  figures (e.g. 129,287,473 vs 129,287,743).
    """
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    diff = abs(float(actual) - float(expected))
    scale = max(abs(float(expected)), abs(float(actual)), 1.0)
    if scale < 10_000:
        return diff <= 0.5
    # 0.0001% — catches single-digit transpositions in Rs.'000 columns.
    return diff <= max(1.0, scale * 1e-6)


def _search_terms_for_fs_label(label: str) -> list[str]:
    pats = patterns_for_label(label, LABEL_ALIASES, "fs", default_to_label=True)
    out = [p.strip() for p in pats if len(p.strip()) >= 6]
    return out or [label]


def _fitz_search_needles(term: str) -> list[str]:
    """
    Short searchable phrases for page.search_for.

    Long OCI labels wrap across lines; searching the full phrase fails, so use
    a stable prefix of the wording that still appears on one visual line.
    """
    raw = (term or "").strip()
    if not raw:
        return []
    needles: list[str] = []
    # Prefer the first ~6-10 words (usually the unique start of the row label).
    words = re.findall(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?", raw)
    if len(words) >= 4:
        needles.append(" ".join(words[:6]))
        needles.append(" ".join(words[:4]))
    needles.append(raw[:48].strip())
    # Dedupe, longest first.
    seen: set[str] = set()
    out: list[str] = []
    for n in needles:
        key = n.lower()
        if len(n) < 8 or key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _strip_note_page_prefix(amounts: list[float]) -> list[float]:
    """
    COMB statement rows often start with Note + Page No. before GROUP/BANK
    amounts (e.g. 24.1, 283, 19.21… or 58, 346, 168.47…).
    """
    if len(amounts) < 3:
        return amounts
    a0, a1 = abs(float(amounts[0])), abs(float(amounts[1]))
    note_like = a0 < 100 and (a0 != int(a0) or a0 <= 80)
    page_like = 40 <= a1 <= 600 and abs(a1 - round(a1)) < 1e-6
    if note_like and page_like:
        return amounts[2:]
    # Integer note + page (e.g. 58, 346).
    if a0 <= 80 and abs(a0 - round(a0)) < 1e-6 and page_like:
        return amounts[2:]
    return amounts


def _fitz_row_amounts(
    page: Any,
    label_rect: Any,
    *,
    year: int,
    is_mem: bool,
    is_small: bool,
) -> list[float]:
    """Collect numeric cells on the same visual row as a label hit (handles wrap)."""
    words = page.get_text("words") or []
    if not words:
        return []
    y0 = float(label_rect.y0) - 2.0
    # Wrapped OCI labels put amounts on the last wrapped line (~15–25pt below).
    y1 = float(label_rect.y0) + 28.0
    x_min = max(180.0, float(label_rect.x0) + 80.0)
    nums: list[float] = []
    for w in sorted(words, key=lambda t: (t[1], t[0])):
        wx0, wy0, _wx1, wy1, token = w[0], w[1], w[2], w[3], w[4]
        y_mid = (wy0 + wy1) / 2.0
        if y_mid < y0 or y_mid > y1:
            continue
        if wx0 < x_min:
            continue
        if not re.search(r"\d", token or ""):
            continue
        v = parse_number(token)
        if v is None:
            continue
        if is_year_like_amount(v, report_year=year):
            continue
        if is_mem:
            if abs(v) > 0:
                nums.append(v)
            continue
        if is_small:
            if abs(v) < 10_000:
                nums.append(v)
            continue
        if abs(v) >= 1_000:
            nums.append(v)
    return _strip_note_page_prefix(nums)


def fitz_lookup_fs_value(
    pdf_path: Path,
    label: str,
    year: int,
    *,
    entity_column: str = "group",
    page_hint: list[int] | None = None,
) -> float | None:
    """
    Direct PDF search for a label, then parse amounts on the same visual row.

    Uses MuPDF word coordinates so wrapped labels (OCI lines) still pick up the
    GROUP/BANK figures on the final wrapped line. Skips Note / Page No. prefixes
    and calendar-year tokens.
    """
    if fitz is None:
        return None

    is_mem = is_memorandum_fs_label(label)
    is_small = norm_label(label) in FS_SMALL_VALUE_LABELS or "per share" in norm_label(
        label
    )
    terms = _search_terms_for_fs_label(label)

    with fitz.open(str(pdf_path)) as doc:
        page_idxs = (
            [p - 1 for p in page_hint if isinstance(p, int) and 1 <= p <= len(doc)]
            if page_hint
            else list(range(len(doc)))
        )
        for pi in page_idxs:
            page = doc[pi]
            for term in terms:
                for needle in _fitz_search_needles(term):
                    try:
                        hits = page.search_for(needle)
                    except Exception:
                        hits = []
                    if not hits:
                        # Soft punctuation: o/a ↔ o a
                        soft = re.sub(r"[\/\-_]+", " ", needle)
                        soft = re.sub(r"\s+", " ", soft).strip()
                        if soft != needle:
                            try:
                                hits = page.search_for(soft)
                            except Exception:
                                hits = []
                    for rect in hits:
                        amounts = _fitz_row_amounts(
                            page,
                            rect,
                            year=year,
                            is_mem=is_mem,
                            is_small=is_small,
                        )
                        if not amounts:
                            continue
                        if is_mem:
                            if not any(
                                is_plausible_memorandum_count(label, a) for a in amounts
                            ):
                                continue
                        elif is_small:
                            # Prefer genuine per-share figures (usually < 500).
                            # Drop bare page numbers (40–600 integers) when a
                            # clearer DPS/EPS/NAV amount is also on the row.
                            plausible = [a for a in amounts if 0 < abs(a) < 500]

                            def _page_like(a: float) -> bool:
                                return (
                                    40.0 <= abs(a) <= 600.0
                                    and abs(a - round(a)) < 1e-6
                                )

                            non_page = [a for a in plausible if not _page_like(a)]
                            if non_page:
                                amounts = non_page
                            elif plausible:
                                amounts = plausible
                        elif not any(abs(a) >= 10_000 for a in amounts):
                            continue
                        if entity_column.lower() == "bank":
                            if is_mem:
                                return amounts[-1] if len(amounts) >= 2 else amounts[0]
                            # GROUP CY, GROUP PY, %chg, BANK CY …
                            return amounts[3] if len(amounts) >= 4 else amounts[-1]
                        return amounts[0]
    return None


def lookup_fs_in_pdf_index(
    pdf_index: FsPdfIndex,
    template_label: str,
    section: str | None,
) -> float | None:
    if is_memorandum_fs_label(template_label):
        return lookup_memorandum_from_pdf_index(
            pdf_index, template_label, section=section
        )
    return pdf_index.lookup(template_label, section=section, entity_column="group")


def build_annual_fs_pdf_index(
    pdf_path: Path,
    manifest: dict,
    year: int,
    *,
    entity_column: str = "group",
) -> FsPdfIndex:
    """Build GROUP (+ BANK for memorandum fallback) annual FS indexes from PDF."""
    section_map = build_fs_section_map(manifest)
    result = FsPdfIndex(section_map=section_map)

    for statement_key in ("income_statement", "sofp", "cash_flows", "oci"):
        # Income statement / OCI often span 3 pages (P&L → EPS → OCI lines).
        if statement_key in {"income_statement", "oci"}:
            max_p = 8
        elif statement_key == "sofp":
            max_p = 8
        else:
            max_p = 6
        # Keep memorandum highlight pages OUT of the SoFP candidate set —
        # they pollute GROUP totals with wrong-year / BANK figures.
        pages = find_statement_pages(pdf_path, statement_key, max_pages=max_p)
        memo_pages: list[int] = []
        if statement_key == "sofp":
            memo_pages = find_memorandum_pages(pdf_path)
        if not pages and not memo_pages:
            continue
        result.pages_by_section[statement_key] = list(
            dict.fromkeys([*(pages or []), *memo_pages])
        )
        group_index = _best_plumber_index(
            pdf_path, pages or memo_pages, statement_key, year, "group"
        )
        if group_index:
            if memo_pages:
                group_index = merge_memorandum_into_pdf_index(
                    group_index, pdf_path, memo_pages, year, "group"
                )
            result.by_section[statement_key] = group_index
        # BANK column only needed for SoFP memorandum fallbacks.
        if statement_key == "sofp":
            bank_index = _best_plumber_index(
                pdf_path, pages or memo_pages, statement_key, year, "bank"
            )
            if bank_index:
                if memo_pages:
                    bank_index = merge_memorandum_into_pdf_index(
                        bank_index, pdf_path, memo_pages, year, "bank"
                    )
                result.by_section_bank[statement_key] = bank_index

    return result


def is_suspicious_fs_value(
    label: str,
    value: float | None,
    values: dict[str, float | None] | None = None,
    *,
    report_year: int | None = None,
) -> bool:
    nl = norm_label(label)
    if value is None:
        return False
    if nl in FS_RATIO_LABELS:
        if nl == "cash total assets":
            # Must be a ratio in (0, 1], not a balance-sheet total.
            return not (0 < abs(float(value)) <= 1.0)
        return False
    # Per-share / EPS / DPS: small currency amounts. Huge values are almost
    # always share counts or P&L totals wrongly mapped onto the EPS row.
    if nl in FS_SMALL_VALUE_LABELS or "per share" in nl or "per ordinary share" in nl:
        return abs(float(value)) >= 1_000.0
    if "number of" in nl:
        return False
    if is_year_like_amount(value, report_year=report_year):
        return True
    return is_suspicious_quarterly_value(label, value, values)


def _pdf_value_is_usable(
    label: str,
    pdf_val: float | None,
    values: dict[str, float | None] | None = None,
    *,
    report_year: int | None = None,
) -> bool:
    if pdf_val is None:
        return False
    if is_memorandum_fs_label(label):
        return is_plausible_memorandum_count(label, pdf_val)
    nl = norm_label(label)
    # Cash / total assets is a ratio — never accept a balance-sheet total.
    if nl == "cash total assets":
        return 0 < abs(float(pdf_val)) <= 1.0
    if is_year_like_amount(pdf_val, report_year=report_year):
        return False
    return not is_suspicious_fs_value(
        label, pdf_val, values, report_year=report_year
    )


def _pdf_lookup_with_fallback(
    pdf_path: Path,
    pdf_index: FsPdfIndex,
    label: str,
    year: int,
    section: str | None,
) -> float | None:
    page_hint = None
    try:
        # Restrict expensive whole-PDF fitz scans to pages already indexed.
        pages: list[int] = []
        for key in (section, "income_statement", "sofp", "cash_flows", "oci"):
            if not key:
                continue
            # FsPdfIndex stores values only; page hints come from build path via attrs if present.
            hint = getattr(pdf_index, "pages_by_section", {}).get(key) or []
            for p in hint:
                if p not in pages:
                    pages.append(p)
        page_hint = pages or None
    except Exception:
        page_hint = None

    # DPS / NAV-style metrics often live only in Financial Highlights / Goals.
    if is_highlight_fs_label(label):
        hl_pages = find_financial_highlight_pages(pdf_path)
        hl_val = lookup_highlight_fs_value(
            pdf_path, label, year, entity_column="group", page_hint=hl_pages
        )
        if _pdf_value_is_usable(label, hl_val, report_year=year):
            return hl_val

    pdf_val = lookup_fs_in_pdf_index(pdf_index, label, section)
    if _pdf_value_is_usable(label, pdf_val, report_year=year):
        return pdf_val
    # OCI lines often sit on the income-statement pages in COMB reports.
    if section == "oci":
        for alt in ("income_statement", "oci"):
            if alt == section:
                continue
            alt_val = lookup_fs_in_pdf_index(pdf_index, label, alt)
            if _pdf_value_is_usable(label, alt_val, report_year=year):
                return alt_val
    fitz_val = fitz_lookup_fs_value(
        pdf_path, label, year, entity_column="group", page_hint=page_hint
    )
    if not _pdf_value_is_usable(label, fitz_val, report_year=year) and page_hint:
        # Label may sit on a continuation page not covered by the statement index.
        fitz_val = fitz_lookup_fs_value(
            pdf_path, label, year, entity_column="group", page_hint=None
        )
    if _pdf_value_is_usable(label, fitz_val, report_year=year):
        return fitz_val
    if is_memorandum_fs_label(label):
        bank_val = lookup_memorandum_from_pdf_index(
            pdf_index, label, section=section or "sofp"
        )
        if bank_val is not None and is_plausible_memorandum_count(label, bank_val):
            return bank_val
        fitz_bank = fitz_lookup_fs_value(
            pdf_path, label, year, entity_column="bank", page_hint=page_hint
        )
        if fitz_bank is not None and is_plausible_memorandum_count(label, fitz_bank):
            return fitz_bank
    # Never hand callers a year-header / junk PDF parse.
    if _pdf_value_is_usable(label, pdf_val, report_year=year):
        return pdf_val
    if _pdf_value_is_usable(label, fitz_val, report_year=year):
        return fitz_val
    return None


def _apply_pdf_value(
    values: dict[str, float | None],
    lbl: str,
    pdf_val: float,
    stats: dict[str, Any],
    log,
    *,
    source: str,
) -> None:
    current = values.get(lbl)
    if current is not None and annual_values_match(current, pdf_val):
        return
    values[lbl] = pdf_val
    stats["pdf_corrected"] += 1
    stats["pdf_corrections"].append(
        {"label": lbl, "from": current, "to": pdf_val, "source": source}
    )
    if current is None:
        log(f"    pdf filled {lbl!r} = {pdf_val:,.4g}")
    else:
        log(
            f"    pdf corrected {lbl!r}: {current:,.4g} -> {pdf_val:,.4g} "
            f"(GROUP annual PDF is authoritative)"
        )


def cross_check_fs_values_against_pdf(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "pdf_used": False,
        "pdf_compared": 0,
        "pdf_corrected": 0,
        "pdf_corrections": [],
    }

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path:
        return values, stats

    manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
    pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)
    if not any(pdf_index.by_section.values()):
        return values, stats

    stats["pdf_used"] = True
    stats["pdf_path"] = str(pdf_path)

    for lbl in labels:
        section = fs_sections.get(lbl)
        pdf_val = _pdf_lookup_with_fallback(pdf_path, pdf_index, lbl, year, section)
        if pdf_val is None:
            continue
        nl = norm_label(lbl)
        if nl in FS_SMALL_VALUE_LABELS or "per share" in nl:
            current = values.get(lbl)
            if abs(pdf_val) < 10_000 and (
                current is None
                or is_suspicious_fs_value(lbl, current, values)
                or not annual_values_match(current, pdf_val)
            ):
                _apply_pdf_value(values, lbl, pdf_val, stats, log, source="pdf_eps")
            continue
        if is_suspicious_fs_value(lbl, pdf_val, values):
            continue
        stats["pdf_compared"] += 1
        current = values.get(lbl)
        if current is None or is_suspicious_fs_value(lbl, current, values):
            _apply_pdf_value(values, lbl, pdf_val, stats, log, source="pdf")
            continue
        if not annual_values_match(current, pdf_val):
            _apply_pdf_value(values, lbl, pdf_val, stats, log, source="pdf")

    return values, stats


def refill_missing_fs_from_pdf(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    pdf_index: FsPdfIndex | None = None,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """Step G — fill any still-missing labels from PDF (GROUP, then BANK for memorandum)."""
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "refilled": 0,
        "refill_labels": [],
    }

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path:
        return values, stats

    if pdf_index is None:
        manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
        pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)

    missing = [lbl for lbl in labels if values.get(lbl) is None]
    if not missing:
        return values, stats

    log(f"  [annual-validate] Step G — refill {len(missing)} missing from PDF")
    for lbl in missing:
        section = fs_sections.get(lbl)
        pdf_val = _pdf_lookup_with_fallback(pdf_path, pdf_index, lbl, year, section)
        if pdf_val is None or not _pdf_value_is_usable(
            lbl, pdf_val, values, report_year=year
        ):
            continue
        values[lbl] = pdf_val
        stats["refilled"] += 1
        stats["refill_labels"].append(lbl)
        log(f"    pdf refill {lbl!r} = {pdf_val:,.4g}")

    return values, stats


def strict_audit_filled_fs_against_pdf(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    pdf_index: FsPdfIndex | None = None,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """Step H — double-check every filled value against PDF; correct mismatches."""
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "audited": 0,
        "mismatches": 0,
        "corrected": 0,
        "mismatch_labels": [],
        "rejected_pdf": 0,
    }

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path:
        return values, stats

    if pdf_index is None:
        manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
        pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)

    filled = [lbl for lbl in labels if values.get(lbl) is not None]
    if not filled:
        return values, stats

    log(f"  [annual-validate] Step H — strict audit of {len(filled)} filled value(s)")
    for lbl in filled:
        current = values.get(lbl)
        if current is None:
            continue
        nl = norm_label(lbl)
        # Ratios / derived memorandum metrics are not reliable PDF row matches.
        if nl in FS_RATIO_LABELS:
            continue
        section = fs_sections.get(lbl)
        pdf_val = _pdf_lookup_with_fallback(pdf_path, pdf_index, lbl, year, section)
        if pdf_val is None:
            continue
        if not _pdf_value_is_usable(lbl, pdf_val, values, report_year=year):
            stats["rejected_pdf"] += 1
            continue
        stats["audited"] += 1
        if annual_values_match(current, pdf_val):
            continue
        # Keep a plausible DB/table value when the PDF parse is orders of magnitude
        # smaller (classic year-header / page-no mixup) — EXCEPT for per-share /
        # EPS rows, where the correct PDF value is often much smaller than a
        # wrongly mapped share-count / profit total.
        nl = norm_label(lbl)
        is_per_share = (
            nl in FS_SMALL_VALUE_LABELS
            or "per share" in nl
            or "per ordinary share" in nl
        )
        current_suspicious = is_suspicious_fs_value(
            lbl, current, values, report_year=year
        )
        if (
            not is_per_share
            and not current_suspicious
            and abs(float(current)) >= 10_000
            and abs(float(pdf_val)) < max(1_000.0, abs(float(current)) * 0.05)
        ):
            stats["rejected_pdf"] += 1
            continue
        stats["mismatches"] += 1
        stats["mismatch_labels"].append(lbl)
        values[lbl] = pdf_val
        stats["corrected"] += 1
        log(
            f"    audit corrected {lbl!r}: {current:,.4g} -> {pdf_val:,.4g} "
            f"(PDF authoritative)"
        )

    return values, stats


def targeted_refind_fs_labels(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    target_labels: list[str] | None = None,
    pdf_index: FsPdfIndex | None = None,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Step I — re-find specific labels still missing or mismatched after audit,
    using direct PDF text search.
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "refind_attempted": 0,
        "refind_found": 0,
        "refind_labels": [],
    }

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path:
        return values, stats

    if pdf_index is None:
        manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
        pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)

    if target_labels is None:
        target_labels = [
            lbl
            for lbl in labels
            if values.get(lbl) is None
            or is_suspicious_fs_value(lbl, values.get(lbl), values, report_year=year)
        ]

    if not target_labels:
        return values, stats

    log(
        f"  [annual-validate] Step I — targeted re-find for "
        f"{len(target_labels)} label(s)"
    )
    for lbl in target_labels:
        stats["refind_attempted"] += 1
        section = fs_sections.get(lbl)
        pdf_val = _pdf_lookup_with_fallback(pdf_path, pdf_index, lbl, year, section)
        if pdf_val is None or not _pdf_value_is_usable(
            lbl, pdf_val, values, report_year=year
        ):
            continue
        current = values.get(lbl)
        if current is not None and annual_values_match(current, pdf_val):
            continue
        # Never replace a large plausible table value with a tiny PDF parse —
        # except per-share / EPS where the correct amount is often tiny.
        nl = norm_label(lbl)
        is_per_share = (
            nl in FS_SMALL_VALUE_LABELS
            or "per share" in nl
            or "per ordinary share" in nl
        )
        if (
            current is not None
            and not is_per_share
            and not is_suspicious_fs_value(lbl, current, values, report_year=year)
            and abs(float(current)) >= 10_000
            and abs(float(pdf_val)) < max(1_000.0, abs(float(current)) * 0.05)
        ):
            continue
        values[lbl] = pdf_val
        stats["refind_found"] += 1
        stats["refind_labels"].append(lbl)
        log(f"    refind {lbl!r} = {pdf_val:,.4g} (was {current})")

    return values, stats


def final_fs_pdf_verification_pass(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "pdf_used": False,
        "pdf_compared": 0,
        "pdf_corrected": 0,
        "pdf_corrections": [],
    }

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path:
        return values, stats

    manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
    pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)
    if not any(pdf_index.by_section.values()):
        return values, stats

    stats["pdf_used"] = True
    stats["pdf_path"] = str(pdf_path)

    for lbl in labels:
        section = fs_sections.get(lbl)
        pdf_val = _pdf_lookup_with_fallback(pdf_path, pdf_index, lbl, year, section)
        if pdf_val is None:
            continue
        nl = norm_label(lbl)
        if nl in FS_SMALL_VALUE_LABELS or "per share" in nl:
            current = values.get(lbl)
            if abs(pdf_val) < 10_000 and (
                current is None
                or is_suspicious_fs_value(lbl, current, values)
                or not annual_values_match(current, pdf_val)
            ):
                _apply_pdf_value(
                    values, lbl, pdf_val, stats, log, source="pdf_final_eps"
                )
            continue
        if is_suspicious_fs_value(lbl, pdf_val, values):
            continue
        stats["pdf_compared"] += 1
        current = values.get(lbl)
        if annual_values_match(current, pdf_val):
            continue
        _apply_pdf_value(values, lbl, pdf_val, stats, log, source="pdf_final")

    return values, stats


def recheck_suspicious_fs_values_against_pdf(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    pdf_index: FsPdfIndex | None = None,
    log_fn=None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "suspicious_found": 0,
        "suspicious_corrected": 0,
        "suspicious_corrections": [],
    }

    suspicious = [
        lbl
        for lbl in labels
        if is_suspicious_fs_value(lbl, values.get(lbl), values, report_year=year)
    ]
    if not suspicious:
        return values, stats

    stats["suspicious_found"] = len(suspicious)
    log(
        f"  [annual-validate] Step F — {len(suspicious)} suspicious FS value(s); "
        f"re-checking GROUP annual PDF"
    )

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path:
        return values, stats

    if pdf_index is None:
        manifest = {"fs": {"rows": [{"label": lbl, "kind": "data"} for lbl in labels]}}
        pdf_index = build_annual_fs_pdf_index(pdf_path, manifest, year)

    for lbl in suspicious:
        current = values.get(lbl)
        section = fs_sections.get(lbl)
        pdf_val = _pdf_lookup_with_fallback(pdf_path, pdf_index, lbl, year, section)
        if pdf_val is None or not _pdf_value_is_usable(
            lbl, pdf_val, values, report_year=year
        ):
            log(f"    suspicious {lbl!r} = {current} — PDF re-check failed; clearing")
            values[lbl] = None
            continue
        if annual_values_match(current, pdf_val):
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
        log(f"    suspicious corrected {lbl!r}: {current} -> {pdf_val:,.0f}")

    return values, stats
