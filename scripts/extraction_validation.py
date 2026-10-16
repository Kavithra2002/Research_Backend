"""
extraction_validation.py
========================
Post-extraction validation for annual + quarterly pipelines.

Annual — main FS tables only:
  • Income Statement / Statement of Profit or Loss
  • Statement of Comprehensive Income (oci, or combined into income_statement)
  • Statement of Financial Position
  • Statement of Cash Flows

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
    "cash_flows",
)

# COMB / DB-page upload: same main FS set as Run Annual
COMB_ANNUAL_REQUIRED_KEYS = (
    "income_statement",
    "sofp",
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
    return ANNUAL_REQUIRED_KEYS + (ANNUAL_OCI_KEY,)


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
    # Local pdfplumber shape (no status / data wrapper).
    if isinstance(entry.get("rows"), list) and entry.get("rows"):
        return True
    status = entry.get("status")
    if status is not None and status != "ok":
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
    else:
        # OCI may be printed as its own statement or combined into IS.
        # Do not block the run when the PDF only has a combined IS.
        pass

    # Equity / investor / ten-year summaries are optional extras — not required.

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


# ── Printed-statement layout double-check (before save) ───────────────────────

_NOTE_CELL = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")
_COMMA_AMT = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?")
_YEAR_HDR = re.compile(r"^20\d{2}$")


def _is_amount_cell(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_COMMA_AMT.search(text) or re.fullmatch(r"[\(\-]?\d+\.\d+\)?", text))


def _is_note_cell(value: str) -> bool:
    return bool(_NOTE_CELL.fullmatch(str(value or "").strip()))


def validate_printed_layout(
    statement: dict[str, Any],
    *,
    report_year: int | None = None,
) -> ValidationResult:
    """Double-check that Note / year / label columns are not swapped."""
    notes: list[str] = []
    found_ok: list[str] = []
    failed: list[str] = []

    key = str(statement.get("key") or statement.get("title") or "statement")
    rows = statement.get("rows") or []
    headers = statement.get("header_rows") or []
    if not isinstance(rows, list) or len(rows) < 3:
        return ValidationResult(
            ok=False,
            report_type="printed",
            failed_extraction=[key],
            notes=[f"{key}: too few rows"],
        )

    data_rows = [
        r
        for r in rows
        if isinstance(r, dict) and (r.get("style") or "data").lower() != "blank"
    ]
    labels = [str((r.get("cells") or [""])[0] or "").strip() for r in data_rows]
    amount_labels = sum(
        1 for lab in labels if _is_amount_cell(lab) and not re.search(r"[A-Za-z]", lab)
    )
    if labels and amount_labels / max(1, len(labels)) > 0.25:
        failed.append(key)
        notes.append(
            f"{key}: {amount_labels}/{len(labels)} labels look like amounts "
            "(columns likely shifted)"
        )
    else:
        found_ok.append(f"{key}:labels")

    note_col = statement.get("note_column")
    if note_col is not None:
        try:
            note_idx = int(note_col)
        except (TypeError, ValueError):
            note_idx = 1
        note_cells = [
            str((r.get("cells") or [""] * (note_idx + 1))[note_idx] or "").strip()
            for r in data_rows
            if len(r.get("cells") or []) > note_idx
        ]
        nonempty = [c for c in note_cells if c]
        bad_notes = [c for c in nonempty if not _is_note_cell(c) and _is_amount_cell(c)]
        if nonempty and len(bad_notes) / max(1, len(nonempty)) > 0.3:
            failed.append(key)
            notes.append(
                f"{key}: Note column holds amounts ({len(bad_notes)} cells) — misaligned"
            )
        else:
            found_ok.append(f"{key}:notes")

        # Amount columns must not be dominated by bare note refs.
        amount_note_hits = 0
        amount_cells = 0
        for r in data_rows:
            cells = r.get("cells") or []
            for idx, cell in enumerate(cells):
                if idx <= note_idx:
                    continue
                text = str(cell or "").strip()
                if not text:
                    continue
                amount_cells += 1
                if _is_note_cell(text) and not _is_amount_cell(text):
                    amount_note_hits += 1
        if amount_cells and amount_note_hits / amount_cells > 0.15:
            failed.append(key)
            notes.append(
                f"{key}: {amount_note_hits} note-like tokens landed in year columns"
            )
        else:
            found_ok.append(f"{key}:amounts")

    if report_year and headers:
        bottom = headers[-1] if headers else []
        years = [int(c) for c in bottom if _YEAR_HDR.fullmatch(str(c).strip())]
        bad_years = [y for y in years if abs(y - int(report_year)) > 2]
        if bad_years:
            failed.append(key)
            notes.append(
                f"{key}: suspicious year header(s) {bad_years} "
                f"(report year {report_year})"
            )
        elif years:
            found_ok.append(f"{key}:years")

    return ValidationResult(
        ok=len(failed) == 0,
        report_type="printed",
        found_ok=sorted(set(found_ok)),
        failed_extraction=sorted(set(failed)),
        notes=notes,
    )


def double_check_printed_payload(
    payload: dict[str, Any],
) -> ValidationResult:
    """Run layout checks on every statement in a printed extract payload."""
    report_year = payload.get("year")
    try:
        year = int(report_year) if report_year is not None else None
    except (TypeError, ValueError):
        year = None

    all_notes: list[str] = []
    all_ok: list[str] = []
    all_failed: list[str] = []
    for statement in payload.get("statements") or []:
        if not isinstance(statement, dict) or not statement.get("ok"):
            continue
        vr = validate_printed_layout(statement, report_year=year)
        all_notes.extend(vr.notes)
        all_ok.extend(vr.found_ok)
        all_failed.extend(vr.failed_extraction)

    return ValidationResult(
        ok=len(all_failed) == 0,
        report_type="printed",
        found_ok=sorted(set(all_ok)),
        failed_extraction=sorted(set(all_failed)),
        notes=all_notes,
    )
