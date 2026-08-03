"""
extraction_validation.py
========================
Post-extraction validation for annual + quarterly pipelines.

Annual — ALL of the following must be extracted with table data:
  • Income Statement
  • Statement of Profit or Loss and Other Comprehensive Income (oci key,
    or a combined income_statement whose title includes comprehensive income)
  • Statement of Financial Position
  • Statement of Changes in Equity
  • Statement of Cash Flows
  • Investor Information
  • Ten Year Summary OR Five Year Summary (at least one)

Quarterly — ALL financial statement families must be extracted:
  • Income Statement
  • Statement of Profit or Loss / Other Comprehensive Income
  • Statement of Financial Position
  • Statement of Changes in Equity
  • Statement of Cash Flows
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── Annual (fixed required set — no peer softening) ───────────────────────────

ANNUAL_REQUIRED_KEYS = (
    "income_statement",
    "sofp",
    "equity",
    "cash_flows",
    "investor_info",
)

# COMB / DB-page upload: core statements only (notes via separate comb extract)
COMB_ANNUAL_REQUIRED_KEYS = (
    "income_statement",
    "sofp",
    "equity",
    "cash_flows",
)

ANNUAL_OCI_KEY = "oci"

ANNUAL_SUMMARY_KEYS = ("ten_year_summary", "five_year_summary")

ANNUAL_DISPLAY_NAMES: dict[str, str] = {
    "income_statement": "Income Statement",
    "oci": "Statement of Profit or Loss and Other Comprehensive Income",
    "sofp": "Statement of Financial Position",
    "equity": "Statement of Changes in Equity",
    "cash_flows": "Statement of Cash Flows",
    "investor_info": "Investor Information",
    "ten_year_summary": "Ten Year Summary",
    "five_year_summary": "Five Year Summary",
    "year_summary": "Ten / Five Year Summary",
}

# ── Quarterly families (keys may be suffixed: income_statement_2, etc.) ───────

QUARTERLY_FAMILY_PATTERNS: dict[str, re.Pattern[str]] = {
    "oci": re.compile(
        r"profit_loss_comprehensive|"
        r"(?:consolidated_|company_)?comprehensive_income|^comprehensive_income",
        re.I,
    ),
    "income": re.compile(
        r"^(?:consolidated_|company_)?income_statement",
        re.I,
    ),
    "sofp": re.compile(r"^financial_position", re.I),
    "equity": re.compile(r"^changes_in_equity", re.I),
    "cash_flows": re.compile(r"^cash_flow", re.I),
}

QUARTERLY_REQUIRED_FAMILIES = (
    "income",
    "oci",
    "sofp",
    "equity",
    "cash_flows",
)

QUARTERLY_FAMILY_DISPLAY: dict[str, str] = {
    "income": "Income Statement",
    "oci": "Statement of Profit or Loss and Other Comprehensive Income",
    "sofp": "Statement of Financial Position",
    "equity": "Statement of Changes in Equity",
    "cash_flows": "Statement of Cash Flows",
}

QUARTERLY_FAMILY_TO_BASE_KEYS: dict[str, list[str]] = {
    "income": ["income_statement", "consolidated_income_statement", "company_income_statement"],
    "oci": [
        "profit_loss_comprehensive",
        "comprehensive_income",
        "consolidated_comprehensive_income",
        "company_comprehensive_income",
    ],
    "sofp": ["financial_position"],
    "equity": ["changes_in_equity"],
    "cash_flows": ["cash_flow_statement"],
}

DEFAULT_MONGO_URI = "mongodb://localhost:27017"
DEFAULT_DB_NAME = "Research_Project"


@dataclass
class ValidationResult:
    ok: bool
    report_type: str
    found_ok: list[str] = field(default_factory=list)
    missing_manifest: list[str] = field(default_factory=list)
    failed_extraction: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def all_gaps(self) -> list[str]:
        return sorted(set(self.missing_manifest + self.failed_extraction))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "report_type": self.report_type,
            "found_ok": self.found_ok,
            "missing_manifest": self.missing_manifest,
            "failed_extraction": self.failed_extraction,
            "notes": self.notes,
            "gap_count": len(self.all_gaps()),
            "required_annual": list(annual_required_checklist()),
            "required_quarterly": list(QUARTERLY_REQUIRED_FAMILIES),
        }


def annual_required_checklist() -> tuple[str, ...]:
    return ANNUAL_REQUIRED_KEYS + (ANNUAL_OCI_KEY, "year_summary")


def _table_row_count(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    tables = data.get("tables")
    if not isinstance(tables, list):
        return 0
    total = 0
    for tbl in tables:
        if not isinstance(tbl, dict):
            continue
        rows = tbl.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            style = (row.get("style") or "data").lower()
            if style == "blank":
                continue
            cells = row.get("cells")
            if isinstance(cells, list) and any(str(c).strip() for c in cells):
                total += 1
    return total


def statement_extraction_ok(entry: dict | None) -> bool:
    if not entry or not isinstance(entry, dict):
        return False
    if entry.get("status") != "ok":
        return False
    data = entry.get("data")
    if not isinstance(data, dict):
        return False
    return _table_row_count(data) > 0


def _oci_satisfied(results: dict[str, Any]) -> bool:
    if statement_extraction_ok(results.get(ANNUAL_OCI_KEY)):
        return True
    inc = results.get("income_statement") or {}
    if not statement_extraction_ok(inc):
        return False
    blob = " ".join([
        str(inc.get("title") or ""),
        str((inc.get("data") or {}).get("statement_title") or ""),
    ]).lower()
    return (
        "comprehensive income" in blob
        or "profit or loss and other" in blob
        or "profit and loss and other" in blob
    )


def _summary_satisfied(results: dict[str, Any]) -> bool:
    return any(statement_extraction_ok(results.get(k)) for k in ANNUAL_SUMMARY_KEYS)


def _quarterly_family(key: str) -> str | None:
    for family in QUARTERLY_REQUIRED_FAMILIES:
        if QUARTERLY_FAMILY_PATTERNS[family].search(key):
            return family
    return None


def _gap_status(
    key: str,
    results: dict[str, Any],
    manifest_keys: set[str],
) -> str | None:
    """Return 'ok', 'failed', or 'missing' for one annual statement key."""
    in_manifest = key in manifest_keys
    in_results = key in results
    if in_results and statement_extraction_ok(results.get(key)):
        return "ok"
    if in_manifest:
        return "failed"
    return "missing"


def validate_annual_results(
    results: dict[str, Any],
    manifest: dict[str, Any],
    *,
    option: str = "1",
    comb_mode: bool = False,
) -> ValidationResult:
    """Validate annual extraction against the fixed required statement list."""
    required_keys = COMB_ANNUAL_REQUIRED_KEYS if comb_mode else ANNUAL_REQUIRED_KEYS
    manifest_keys = set((manifest.get("statements") or {}).keys())
    found_ok: list[str] = []
    failed: list[str] = []
    missing: list[str] = []

    for key in required_keys:
        status = _gap_status(key, results, manifest_keys)
        if status == "ok":
            found_ok.append(key)
        elif status == "failed":
            failed.append(key)
        else:
            missing.append(key)

    if _oci_satisfied(results):
        found_ok.append(ANNUAL_OCI_KEY)
    elif comb_mode:
        pass  # OCI optional for COMB pilot when combined in income_statement
    elif ANNUAL_OCI_KEY in manifest_keys or ANNUAL_OCI_KEY in results:
        failed.append(ANNUAL_OCI_KEY)
    else:
        missing.append(ANNUAL_OCI_KEY)

    if _summary_satisfied(results):
        found_ok.append("year_summary")
        for k in ANNUAL_SUMMARY_KEYS:
            if statement_extraction_ok(results.get(k)):
                found_ok.append(k)
    elif not comb_mode:
        missing.append("year_summary")
        for k in ANNUAL_SUMMARY_KEYS:
            if k in manifest_keys or k in results:
                failed.append(k)

    if option == "2" and "notes" in manifest_keys:
        if statement_extraction_ok(results.get("notes")):
            found_ok.append("notes")
        elif "notes" in results:
            failed.append("notes")
        elif not comb_mode:
            missing.append("notes")

    vr = ValidationResult(
        ok=len(missing) == 0 and len(failed) == 0,
        report_type="annual",
        found_ok=sorted(set(found_ok)),
        missing_manifest=sorted(set(missing)),
        failed_extraction=sorted(set(failed)),
    )
    if missing:
        names = [ANNUAL_DISPLAY_NAMES.get(k, k) for k in missing]
        vr.notes.append(f"Not found in PDF scan: {', '.join(names)}")
    if failed:
        names = [ANNUAL_DISPLAY_NAMES.get(k, k) for k in failed]
        vr.notes.append(f"Detected but extraction failed/empty: {', '.join(names)}")
    return vr


def annual_keys_to_fix(report: ValidationResult) -> list[str]:
    """Map validation gaps back to step1/3 statement keys for re-extraction."""
    keys: list[str] = []
    for gap in report.all_gaps():
        if gap == "year_summary":
            keys.extend(ANNUAL_SUMMARY_KEYS)
        elif gap != "notes":
            keys.append(gap)
    return sorted(set(keys))


def validate_quarterly_results(
    result_doc: dict[str, Any],
) -> ValidationResult:
    """Validate that every required quarterly financial family is extracted."""
    statements = result_doc.get("statements") or {}
    if not isinstance(statements, dict):
        statements = {}

    ok_keys: list[str] = []
    failed: list[str] = []
    families_ok: set[str] = set()

    for key, entry in statements.items():
        if statement_extraction_ok(entry):
            ok_keys.append(key)
            fam = _quarterly_family(key)
            if fam:
                families_ok.add(fam)
        elif entry.get("status") not in ("dry_run", "no_api_key", "skipped"):
            failed.append(key)

    missing_families: list[str] = []
    for fam in QUARTERLY_REQUIRED_FAMILIES:
        if fam not in families_ok:
            missing_families.append(fam)

    vr = ValidationResult(
        ok=len(missing_families) == 0 and len(failed) == 0,
        report_type="quarterly",
        found_ok=sorted(ok_keys),
        missing_manifest=missing_families,
        failed_extraction=sorted(set(failed)),
    )
    if missing_families:
        names = [QUARTERLY_FAMILY_DISPLAY.get(f, f) for f in missing_families]
        vr.notes.append(f"Missing quarterly statements: {', '.join(names)}")
    if failed:
        vr.notes.append(f"{len(failed)} extracted key(s) failed or empty")
    return vr


def quarterly_keys_for_missing_families(
    missing_families: list[str],
) -> list[str]:
    out: list[str] = []
    for fam in missing_families:
        out.extend(QUARTERLY_FAMILY_TO_BASE_KEYS.get(fam, []))
    return sorted(set(out))
