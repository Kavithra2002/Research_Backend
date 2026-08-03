"""
Local PDF note-table extraction for COMB Drivers breakdowns (no OpenAI).

Uses PyMuPDF for page discovery and pdfplumber coordinate-based table parsing
to read Bank-column values from annual report note pages.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from comb_reconcile import parse_number

NOTE_SEARCH_TERMS: dict[str, list[str]] = {
    "Fee and commission income": ["14.1", "fee and commission income"],
    "Fee and commission expense": ["14.2", "fee and commission expense"],
    "Interest income": ["interest income", "5.", "note 5"],
    "Less: Interest expense": ["interest expense", "6.", "note 6"],
    "Net gains/(losses) from trading": ["trading", "fair value through profit"],
    "Net other operating income": ["other operating income"],
    "Impairment charges and other losses": ["impairment", "provision"],
    "Other operating expenses": ["other operating expenses"],
    "Placements with banks": ["placements with banks"],
    "Gross Loans and advances": ["loans and advances", "gross loans"],
    "Gross loans and advances": ["loans and advances", "gross loans"],
}

_VAL_TOKEN_RE = re.compile(
    r"^(?:"
    r"[\(\-\u2013\u2014]?\d[\d,\.]*\)?%?"
    r"|[\(\)\-\u2013\u2014]"
    r")$"
)
_ANCHOR_TOKEN_RE = re.compile(r"\d{2,}")


def norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_annual_pdf(db, company_slug: str, year: int) -> Path | None:
    doc = db.financial_tables.find_one(
        {
            "company_slug": company_slug,
            "year": year,
            "report_type": "annual",
            "source_pdf": {"$exists": True, "$ne": ""},
        },
        {"source_pdf": 1},
        sort=[("updated_at", -1)],
    )
    if doc and doc.get("source_pdf"):
        p = Path(str(doc["source_pdf"]))
        if p.exists():
            return p
    demo = (
        Path(__file__).resolve().parent.parent
        / "Demo_Data"
        / "Commercial Bank of Ceylon PLC"
        / "Annual"
        / f"Annual report {year}"
    )
    if demo.exists():
        pdfs = list(demo.glob("*.pdf"))
        if pdfs:
            return pdfs[0]
    return None


def find_note_pages(
    pdf_path: Path,
    parent_label: str,
    *,
    note_hint: str | None = None,
    max_pages: int = 6,
) -> list[int]:
    """Return 1-based PDF page numbers likely containing the note table."""
    if fitz is None:
        return []
    terms = list(NOTE_SEARCH_TERMS.get(parent_label, []))
    if note_hint:
        terms = [note_hint] + terms
    terms.append(parent_label.split(":")[-1].strip().lower())
    terms = [t.lower() for t in terms if t]

    hits: list[tuple[int, int]] = []
    with fitz.open(str(pdf_path)) as doc:
        for i in range(len(doc)):
            text = (doc[i].get_text() or "").lower()
            score = sum(1 for t in terms if t in text)
            if score:
                hits.append((score, i + 1))
    hits.sort(reverse=True)
    return [p for _, p in hits[:max_pages]]


def _is_val_token(tok: str) -> bool:
    return bool(_VAL_TOKEN_RE.match(tok.strip()))


def _is_anchor_token(tok: str) -> bool:
    return bool(_ANCHOR_TOKEN_RE.search(tok.strip()))


def _words_table_rows(page) -> list[list[str]]:
    """Coordinate-based row grid from a pdfplumber page."""
    if page is None:
        return []
    try:
        words = page.extract_words(
            x_tolerance=1.5,
            y_tolerance=2,
            keep_blank_chars=False,
            use_text_flow=False,
        )
    except Exception:
        return []
    if not words:
        return []

    words.sort(key=lambda w: (round(w["top"], 0), w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) < 3:
            lines[-1].append(w)
        else:
            lines.append([w])

    anchor_x1: list[float] = []
    for ln in lines:
        for w in ln:
            if _is_anchor_token(w["text"]):
                anchor_x1.append(float(w["x1"]))
    if len(anchor_x1) < 4:
        return []

    anchor_x1.sort()
    clusters: list[list[float]] = []
    current: list[float] = [anchor_x1[0]]
    for x in anchor_x1[1:]:
        if x - current[-1] < 6.0:
            current.append(x)
        else:
            clusters.append(current)
            current = [x]
    clusters.append(current)

    col_anchors = sorted(sum(c) / len(c) for c in clusters if len(c) >= 3)
    if len(col_anchors) < 2:
        col_anchors = sorted(sum(c) / len(c) for c in clusters if len(c) >= 2)
    if len(col_anchors) < 2:
        return []

    n_cols = len(col_anchors)
    raw_rows: list[list[str]] = []

    for ln in lines:
        if not ln:
            continue
        ln.sort(key=lambda w: w["x0"])
        phrases: list[list[dict]] = [[ln[0]]]
        for w in ln[1:]:
            gap = w["x0"] - phrases[-1][-1]["x1"]
            if _is_val_token(phrases[-1][-1]["text"]) and _is_val_token(w["text"]):
                phrases.append([w])
            elif gap <= 8.0:
                phrases[-1].append(w)
            else:
                phrases.append([w])

        first_val_x0: float | None = None
        for w in ln:
            if _is_val_token(w["text"]):
                first_val_x0 = float(w["x0"])
                break

        row = [""] * (n_cols + 1)
        for phrase in phrases:
            text = " ".join(w["text"] for w in phrase).strip()
            if not text:
                continue
            x_mid = sum(w["x0"] + w["x1"] for w in phrase) / (2 * len(phrase))
            if first_val_x0 is not None and x_mid < first_val_x0 - 2:
                row[0] = (row[0] + " " + text).strip() if row[0] else text
            else:
                best_ci = min(
                    range(n_cols),
                    key=lambda ci: abs(col_anchors[ci] - x_mid),
                )
                row[best_ci + 1] = (
                    (row[best_ci + 1] + " " + text).strip()
                    if row[best_ci + 1]
                    else text
                )
        if any(c.strip() for c in row):
            raw_rows.append(row)
    return raw_rows


def _split_header_body(rows: list[list[str]]) -> tuple[list[list[str]], list[list[str]]]:
    """
    Split title/header rows from numeric body rows.

    Ignores footer page numbers and keeps GROUP/BANK year banners in the
    header block so entity-column detection still works.
    """
    if not rows:
        return [], []

    def _substantial_amounts(row: list[str]) -> int:
        n = 0
        for c in row[1:]:
            v = parse_number(c)
            if v is None:
                continue
            # Page numbers / tiny note refs are not FS amounts.
            if abs(v) < 1_000 and abs(v - round(v)) < 1e-9:
                continue
            if 2000 <= abs(v) <= 2035 and abs(v - round(v)) < 1e-9:
                continue
            n += 1
        return n

    # Prefer to cut after the GROUP/BANK + year header block when present.
    header_end = 0
    for i, row in enumerate(rows[:20]):
        joined = " ".join(str(c or "") for c in row).upper()
        if "GROUP" in joined and ("BANK" in joined or "COMPANY" in joined):
            header_end = max(header_end, i + 1)
        if re.search(r"\b20\d{2}\b", joined) and (
            "RS" in joined or "CHANGE" in joined or "%" in joined or "NOTE" in joined
        ):
            header_end = max(header_end, i + 1)
        if "AS AT" in joined or "FOR THE YEAR ENDED" in joined:
            header_end = max(header_end, i + 1)

    first_data = None
    for i, row in enumerate(rows):
        if i < header_end:
            continue
        label = (row[0] or "").strip()
        if label and _substantial_amounts(row) >= 1 and not re.match(r"^\d{4}$", label):
            first_data = i
            break

    if first_data is None:
        # Fallback: original heuristic but still skip page-number-only rows.
        first_data = 0
        for i, row in enumerate(rows):
            label = (row[0] or "").strip()
            if label and _substantial_amounts(row) >= 1 and not re.match(r"^\d{4}$", label):
                first_data = i
                break

    return rows[:first_data], rows[first_data:]


def _entity_header_positions(
    header_rows: list[list[str]],
) -> tuple[int | None, int | None]:
    group_ci = other_ci = None
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            cu = str(cell).strip().upper()
            if cu == "GROUP":
                group_ci = ci
            elif cu in {"BANK", "COMPANY"}:
                other_ci = ci
    return group_ci, other_ci


_PERIOD_QUARTER_RE = re.compile(
    r"(?:for\s+the\s+)?(?:(?:three|3)\s+months|quarter)\s+ended",
    re.IGNORECASE,
)
_PERIOD_NON_QUARTER_RE = re.compile(
    r"(?:six|nine|twelve|12)\s+months\s+ended|(?:for\s+the\s+)?year\s+ended|\bytd\b|half[\s-]?year",
    re.IGNORECASE,
)


def _is_period_header(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"group|bank|company", t, re.IGNORECASE):
        return False
    return bool(
        _PERIOD_QUARTER_RE.search(t)
        or _PERIOD_NON_QUARTER_RE.search(t)
        or re.search(r"\bmonths?\s+ended\b", t, re.IGNORECASE)
    )


def _column_period_labels(header_rows: list[list[str]]) -> dict[int, str]:
    """Map each data column to its period banner (six months vs quarter ended, etc.)."""
    if not header_rows:
        return {}
    h0 = header_rows[0]
    banners: list[tuple[int, str]] = []
    for ci, cell in enumerate(h0):
        text = str(cell).strip()
        if _is_period_header(text):
            banners.append((ci, text))
    if not banners:
        return {}

    labels: dict[int, str] = {}
    for idx, (start, label) in enumerate(banners):
        end = banners[idx + 1][0] if idx + 1 < len(banners) else len(h0)
        for ci in range(start, end):
            labels[ci] = label
    return labels


def _period_sections(header_rows: list[list[str]]) -> list[dict[str, Any]]:
    """Return period blocks with the date columns that belong to each banner."""
    if not header_rows:
        return []
    h0 = header_rows[0]
    banners: list[tuple[int, str]] = []
    for ci, cell in enumerate(h0):
        text = str(cell).strip()
        if _is_period_header(text):
            banners.append((ci, text))

    sections: list[dict[str, Any]] = []
    for idx, (start, label) in enumerate(banners):
        end = banners[idx + 1][0] if idx + 1 < len(banners) else len(h0)
        date_cols = [
            ci
            for ci in range(start, end)
            if not _is_change_column(header_rows, ci)
            and _year_at_column(header_rows, ci) is not None
        ]
        sections.append({"label": label, "start": start, "end": end, "date_cols": date_cols})
    return sections


def _year_at_column(header_rows: list[list[str]], ci: int) -> str | None:
    for hrow in header_rows[1:5]:
        if ci >= len(hrow):
            continue
        cell = str(hrow[ci]).strip()
        m = re.search(r"\b(20\d{2})\b", cell)
        if m:
            return m.group(1)
    return None


def _is_change_column(header_rows: list[list[str]], ci: int) -> bool:
    for hrow in header_rows:
        if ci >= len(hrow):
            continue
        cell = str(hrow[ci]).strip().lower()
        if cell in {"change %", "change", "%", "variance", "var %"}:
            return True
    return False


def _is_note_ref_cell(cell: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}(\.\d{1,2})?", str(cell).strip()))


def _is_likely_page_no_value(cell: str, value: float | None) -> bool:
    """Body tables often insert a page-number column between Note ref and amounts."""
    if value is None:
        return False
    raw = str(cell).strip()
    if "," in raw or raw.startswith("("):
        return False
    return abs(value) < 1_000


def _is_likely_year_header_value(value: float | None, year: int | None = None) -> bool:
    """Reject calendar years that appear as FS column headers (2018/2019), not amounts."""
    if value is None:
        return False
    v = float(value)
    if abs(v - round(v)) > 1e-9:
        return False
    iv = int(round(abs(v)))
    if year is not None and iv in {int(year), int(year) - 1, int(year) + 1}:
        return True
    return 2000 <= iv <= 2035


def _first_amount_body_col(row: list[str]) -> int | None:
    for ci in range(1, len(row)):
        cell = str(row[ci]).strip()
        if _is_note_ref_cell(cell):
            continue
        value = parse_number(row[ci])
        if value is None:
            continue
        if _is_likely_page_no_value(cell, value):
            continue
        return ci
    return None


def _group_year_order(header_rows: list[list[str]], entity_column: str = "group") -> list[str]:
    """Ordered GROUP (or BANK) year labels left-to-right from headers."""
    group_ci, bank_ci = _entity_header_positions(header_rows)
    years: list[tuple[int, str]] = []
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            if _is_change_column(header_rows, ci):
                continue
            if entity_column.lower() == "bank":
                if bank_ci is not None and ci < bank_ci:
                    continue
            elif group_ci is not None and bank_ci is not None:
                if not (group_ci <= ci < bank_ci):
                    continue
            elif group_ci is not None and ci < group_ci:
                continue
            text = str(cell).strip()
            match = re.search(r"\b(20\d{2})\b", text)
            if match:
                years.append((ci, match.group(1)))
    years.sort(key=lambda item: item[0])
    ordered: list[str] = []
    seen: set[str] = set()
    for _, year_s in years:
        if year_s not in seen:
            seen.add(year_s)
            ordered.append(year_s)
    return ordered


def row_entity_year_col(
    row: list[str],
    header_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> int | None:
    """
    Body column for the GROUP/BANK amount for `year` on one row.

    Skips note-reference and page-number cells; never returns Note or Page No.
    """
    if not row:
        return None
    year_s = str(year)
    year_order = _group_year_order(header_rows, entity_column)
    if year_s not in year_order:
        return None
    year_slot = year_order.index(year_s)

    ci = 1
    if len(row) > ci and _is_note_ref_cell(str(row[ci])):
        ci += 1
    # Skip blank spacer cells (some totals omit Note / Page No.).
    while len(row) > ci and not str(row[ci]).strip():
        ci += 1
    if len(row) > ci:
        page_val = parse_number(row[ci])
        if _is_likely_page_no_value(str(row[ci]), page_val):
            ci += 1
            while len(row) > ci and not str(row[ci]).strip():
                ci += 1

    target = ci + year_slot
    if target >= len(row):
        return None

    val = parse_number(row[target])
    if val is None:
        # Prefer the first non-page, non-year amount at/after the expected slot.
        for ai in range(ci, len(row)):
            if _is_change_column(header_rows, ai):
                continue
            candidate = parse_number(row[ai])
            if candidate is None:
                continue
            if _is_likely_page_no_value(str(row[ai]), candidate):
                continue
            if _is_likely_year_header_value(candidate, year):
                continue
            return ai
        return None
    if _is_likely_page_no_value(str(row[target]), val):
        return None
    if _is_likely_year_header_value(val, year):
        return None
    return target


def body_column_offset(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
) -> int:
    """
    Stored quarterly tables often insert a blank column after the row label,
    shifting body values one column right relative to the header grid.
    Annual tables may insert a Note column (e.g. 13.1) before values.
    """
    header_start: int | None = None
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            text = str(cell).strip()
            if not text:
                continue
            # Only treat a cell as a year/date header when the year is the
            # primary token — not titles like "Annual Report 2022".
            if re.fullmatch(r"20\d{2}", text):
                header_start = ci if header_start is None else min(header_start, ci)
                continue
            if re.search(r"\d{2}\.\d{2}\.\d{4}", text):
                header_start = ci if header_start is None else min(header_start, ci)
                continue
            # "2022 Rs.'000" style unit headers
            if re.fullmatch(r"20\d{2}\b.*", text) and len(text) <= 24:
                header_start = ci if header_start is None else min(header_start, ci)

    body_start: int | None = None
    for row in body_rows:
        label = str(row[0]).strip() if row else ""
        if not label or re.match(r"^income statement", label, re.I):
            continue
        body_start = _first_amount_body_col(row)
        if body_start is not None:
            break

    if header_start is None or body_start is None:
        return 0
    return body_start - header_start


def find_annual_group_body_col(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> int | None:
    """
    Body-row column for GROUP current-year Rs.'000 in annual FS tables.

    Never returns BANK, prior-year, or Change % columns.
    """
    year_s = str(year)
    group_ci, bank_ci = _entity_header_positions(header_rows)

    header_col: int | None = None
    year_positions: list[int] = []
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            if _is_change_column(header_rows, ci):
                continue
            text = str(cell).strip()
            if text == year_s or re.search(rf"\b{year_s}\b", text):
                year_positions.append(ci)

    if year_positions:
        if group_ci is not None and bank_ci is not None:
            group_hits = [ci for ci in year_positions if group_ci <= ci < bank_ci]
        elif group_ci is not None:
            group_hits = [ci for ci in year_positions if ci >= group_ci]
        else:
            group_hits = year_positions
        if group_hits:
            header_col = min(group_hits)

    if header_col is None:
        header_col = _entity_year_col(header_rows, body_rows, year, entity_column)
        if header_col is not None and _is_change_column(header_rows, header_col):
            header_col = None

    if header_col is None:
        return None

    for hrow in header_rows:
        if header_col < len(hrow):
            unit = str(hrow[header_col]).strip().lower()
            if unit == "%":
                return None

    return header_col_to_body_col(header_col, header_rows, body_rows)


def find_annual_bank_body_col(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
) -> int | None:
    """
    Body-row column for BANK current-year in annual FS tables.

    Used when memorandum information rows have no GROUP figure.
    Never returns GROUP, prior-year, or Change % columns.
    """
    year_s = str(year)
    group_ci, bank_ci = _entity_header_positions(header_rows)

    header_col: int | None = None
    year_positions: list[int] = []
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            if _is_change_column(header_rows, ci):
                continue
            text = str(cell).strip()
            if text == year_s or re.search(rf"\b{year_s}\b", text):
                year_positions.append(ci)

    if year_positions and bank_ci is not None:
        bank_hits = [ci for ci in year_positions if ci >= bank_ci]
        if bank_hits:
            header_col = min(bank_hits)

    if header_col is None:
        header_col = _entity_year_col(header_rows, body_rows, year, "bank")
        if header_col is not None and _is_change_column(header_rows, header_col):
            header_col = None

    if header_col is None:
        return None

    for hrow in header_rows:
        if header_col < len(hrow):
            unit = str(hrow[header_col]).strip().lower()
            if unit == "%":
                return None

    return header_col_to_body_col(header_col, header_rows, body_rows)


def header_col_to_body_col(
    header_col: int,
    header_rows: list[list[str]],
    body_rows: list[list[str]],
) -> int:
    return header_col + body_column_offset(header_rows, body_rows)


def _quarter_ended_header_col(
    header_rows: list[list[str]],
    year: int,
    *,
    entity_column: str = "group",
) -> int | None:
    """
    Header-grid column for the current-year amount under 'For the quarter ended'.

    Never returns six/nine/twelve-month YTD or Change % columns.
    """
    year_s = str(year)
    group_ci, other_ci = _entity_header_positions(header_rows)

    dated_cols: list[int] = []
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            if _is_change_column(header_rows, ci):
                continue
            if re.search(rf"\d{{2}}\.\d{{2}}\.{year_s}", str(cell)):
                dated_cols.append(ci)

    if dated_cols:
        # Standard CSE layout: [6mo Y, 6mo Y-1, ch%, quarter Y, quarter Y-1, ch%]
        if len(dated_cols) >= 2:
            return dated_cols[1]
        return dated_cols[0]

    plain_year_cols: list[int] = []
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            if _is_change_column(header_rows, ci):
                continue
            if str(cell).strip() == year_s:
                plain_year_cols.append(ci)

    if group_ci is not None and other_ci is not None:
        plain_year_cols = [ci for ci in plain_year_cols if group_ci <= ci < other_ci]
    elif group_ci is not None and entity_column.lower() == "group":
        plain_year_cols = [ci for ci in plain_year_cols if ci >= group_ci]

    if plain_year_cols:
        return plain_year_cols[0]

    # Fallback: quarter-ended section from period banners.
    sections = _period_sections(header_rows)
    quarter_sections = [
        s for s in sections if _PERIOD_QUARTER_RE.search(str(s.get("label") or ""))
    ]
    for section in quarter_sections:
        for ci in section.get("date_cols", []):
            if _year_at_column(header_rows, ci) == year_s:
                return ci
        for ci in range(int(section["start"]), int(section["end"]) + 1):
            if _is_change_column(header_rows, ci):
                continue
            if _year_at_column(header_rows, ci) == year_s:
                return ci

    return None


def find_quarter_ended_body_col(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> int | None:
    """Body-row column index for GROUP quarter-ended current-year Rs.'000."""
    header_col = _quarter_ended_header_col(
        header_rows, year, entity_column=entity_column
    )
    if header_col is None:
        return None
    if _is_change_column(header_rows, header_col):
        return None
    return header_col_to_body_col(header_col, header_rows, body_rows)


def _column_data_score(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    ci: int,
    *,
    year_s: str | None = None,
    require_year: bool = False,
) -> float:
    """Prefer columns with report-year headers and large financial magnitudes."""
    if _is_change_column(header_rows, ci):
        return -1.0
    year_match = year_s is not None and _year_at_column(header_rows, ci) == year_s
    if require_year and not year_match:
        return -1.0
    mags: list[float] = []
    for row in body_rows[:30]:
        if ci >= len(row):
            continue
        val = parse_number(row[ci])
        if val is not None:
            mags.append(abs(val))
    if not mags:
        return -1.0
    median = sorted(mags)[len(mags) // 2]
    if median < 1000:
        return -1.0
    return (1_000_000.0 if year_match else 0.0) + median


def find_quarterly_value_col(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> int | None:
    """
    Pick the body-row value column for quarterly income statements.

    GROUP only, 'For the quarter ended' only — never six/nine/twelve-month YTD.
    """
    return find_quarter_ended_body_col(
        header_rows, body_rows, year, entity_column=entity_column
    )


def find_quarter_only_value_cols(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> list[int]:
    """
    Return quarter-ended body column indices for the target year only.

    Excludes six/nine/twelve-month YTD columns, Change %, and prior-year columns.
    """
    col = find_quarter_ended_body_col(
        header_rows, body_rows, year, entity_column=entity_column
    )
    return [col] if col is not None else []


def _year_col_for_entity(
    header_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> int | None:
    """Return column index for entity (GROUP or BANK) + year in CSE note/FS tables."""
    year_s = str(year)
    entity = entity_column.lower()
    group_ci, bank_ci = _entity_header_positions(header_rows)

    if group_ci is not None or bank_ci is not None:
        for hrow in header_rows:
            for ci, cell in enumerate(hrow):
                if str(cell).strip() != year_s:
                    continue
                if entity == "group":
                    if bank_ci is None or ci < bank_ci:
                        return ci
                elif entity == "bank":
                    if bank_ci is not None and ci >= bank_ci - 1:
                        return ci
        # year row may be offset from GROUP/BANK header
        anchor = group_ci if entity == "group" else bank_ci
        if anchor is not None:
            for hrow in header_rows:
                for ci, cell in enumerate(hrow):
                    if str(cell).strip() == year_s:
                        if entity == "group" and (bank_ci is None or ci < bank_ci):
                            return ci
                        if entity == "bank" and bank_ci is not None and ci >= bank_ci:
                            return ci
    return None


def extract_note_breakdown_from_rows(
    raw_rows: list[list[str]],
    note_ref: str,
    title_hint: str,
    year: int,
    entity_column: str = "group",
) -> dict[str, float]:
    """
    Extract label->value from a note sub-table embedded in a notes page.

    Finds the block headed by e.g. ``13.1 Interest income`` with GROUP/BANK
    columns, then reads data rows until the next note heading.
    """
    ref = note_ref.strip()
    hint = (title_hint or "").lower()
    year_s = str(year)
    entity = entity_column.upper()

    best_index: dict[str, float] = {}

    for i, row in enumerate(raw_rows):
        joined_low = " ".join(str(c) for c in row).lower()
        if ref not in joined_low:
            continue
        if hint and hint not in joined_low:
            continue

        header_block: list[list[str]] = []
        data_start: int | None = None
        value_col: int | None = None

        for j in range(i, min(i + 12, len(raw_rows))):
            hdr = raw_rows[j]
            row_up = [str(c).strip().upper() for c in hdr]
            if "GROUP" not in row_up or "BANK" not in row_up:
                continue
            group_ci = next((ci for ci, c in enumerate(row_up) if c == "GROUP"), None)
            bank_ci = next((ci for ci, c in enumerate(row_up) if c == "BANK"), None)
            header_block = raw_rows[j : j + 4]
            for hr in header_block[1:]:
                for ci, cell in enumerate(hr):
                    if str(cell).strip() != year_s:
                        continue
                    if entity == "GROUP" and (bank_ci is None or ci < bank_ci):
                        value_col = ci
                        break
                    if entity == "BANK" and bank_ci is not None and ci >= bank_ci - 1:
                        value_col = ci
                        break
                if value_col is not None:
                    break
            if value_col is None:
                if entity == "GROUP" and group_ci is not None:
                    value_col = group_ci
                elif entity == "BANK" and bank_ci is not None:
                    value_col = bank_ci
            hdr_end = j + 1
            for k in range(j + 1, min(j + 6, len(raw_rows))):
                if any("000" in str(c) for c in raw_rows[k]):
                    hdr_end = k + 1
                    break
            data_start = hdr_end
            break

        if value_col is None or data_start is None:
            continue

        index: dict[str, float] = {}
        for drow in raw_rows[data_start:]:
            label = (drow[0] if drow else "").strip()
            if not label:
                joined = " ".join(str(c) for c in drow)
                if re.search(r"\d{1,2}\.\d{1,2}\s+\w", joined):
                    break
                continue
            if re.match(r"^\d{1,2}(\.\d{1,2})?\s", label):
                break
            if label.upper() in {"TOTAL", "ACCOUNTING POLICY"}:
                if index:
                    break
                continue
            if value_col >= len(drow):
                continue
            val = parse_number(drow[value_col])
            if val is None:
                continue
            nl = norm_label(label)
            if nl and nl not in index:
                index[nl] = val

        if len(index) > len(best_index):
            best_index = index

    return best_index


def _entity_year_col(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
    entity_column: str = "group",
) -> int | None:
    """Pick the data column for entity (GROUP or BANK) + year."""
    year_s = str(year)
    entity = entity_column.lower()
    n_cols = max((len(r) for r in header_rows + body_rows), default=0)

    col = _year_col_for_entity(header_rows, year, entity_column)
    if col is not None:
        return col

    # Pass 1: entity label columns with adjacent year
    entity_cols: list[int] = []
    for hrow in header_rows:
        for ci, cell in enumerate(hrow):
            cu = str(cell).strip().upper()
            if entity == "group" and cu == "GROUP":
                entity_cols.append(ci)
            elif entity == "bank" and cu == "BANK":
                entity_cols.append(ci)

    year_by_col: dict[int, str] = {}
    for hrow in header_rows:
        for ci in range(1, len(hrow)):
            cell = (hrow[ci] or "").strip()
            m = re.search(r"\b(20\d{2})\b", cell)
            if m:
                year_by_col[ci] = m.group(1)

    group_ci, bank_ci = _entity_header_positions(header_rows)
    for ci in sorted(entity_cols):
        if _is_change_column(header_rows, ci):
            continue
        if year_by_col.get(ci) == year_s:
            return header_col_to_body_col(ci, header_rows, body_rows)
        for offset in (0, 1, -1):
            target = ci + offset
            if _is_change_column(header_rows, target):
                continue
            if year_by_col.get(target) == year_s:
                return header_col_to_body_col(target, header_rows, body_rows)

    # Pass 2: flat year row "2022 2021 2022 2021" — GROUP is first pair
    flat_years: list[tuple[int, str]] = []
    for hrow in header_rows:
        for ci in range(1, len(hrow)):
            cell = (hrow[ci] or "").strip()
            if _is_change_column(header_rows, ci):
                continue
            if re.fullmatch(r"20\d{2}", cell):
                flat_years.append((ci, cell))
    if flat_years:
        matches = [c for c, y in flat_years if y == year_s]
        if matches:
            if entity == "group":
                return header_col_to_body_col(matches[0], header_rows, body_rows)
            pick = matches[1] if len(matches) > 1 else matches[0]
            return header_col_to_body_col(pick, header_rows, body_rows)

    # Pass 3: magnitude heuristic
    best_ci: int | None = None
    best_score = -1.0
    for ci in range(1, n_cols):
        mags: list[float] = []
        for row in body_rows[:30]:
            if ci >= len(row):
                continue
            v = parse_number(row[ci])
            if v is not None:
                mags.append(abs(v))
        if not mags:
            continue
        median = sorted(mags)[len(mags) // 2]
        score = sum(1 for m in mags if m >= 1000) * 10 + (20 if median >= 10000 else 0)
        if entity == "group" and bank_ci is not None and ci >= bank_ci:
            score *= 0.3
        if entity == "bank" and group_ci is not None and ci < bank_ci:
            score *= 0.3
        if score > best_score:
            best_score = score
            best_ci = ci
    return best_ci


FS_TOPIC_LABELS = frozenset(
    norm_label(x)
    for x in (
        "INCOME STATEMENT",
        "OCI",
        "BALANCE SHEET",
        "CASH FLOW STATEMENT",
        "Assets",
        "Liabilities",
        "Equity",
        "Memorandum information",
        "Cash flows from operating activities",
        "Cash flows from investing activities",
        "Cash flows from financing activities",
        "Adjustments for:",
        "Less: Expenses",
    )
)


def _is_fs_topic_label(label: str) -> bool:
    nl = norm_label(label)
    if nl in FS_TOPIC_LABELS:
        return True
    return bool(re.match(r"^cash flows from \w+ activities$", nl))


def _is_annual_fs_value_row(row: list[str], value_col: int) -> bool:
    """True when the row carries statement amounts in the target column."""
    if value_col >= len(row):
        return False
    val = parse_number(row[value_col])
    if val is None:
        return False
    value_band = [
        parse_number(row[ci])
        for ci in range(value_col, min(len(row), value_col + 6))
        if parse_number(row[ci]) is not None
    ]
    if len(value_band) >= 2:
        return True
    return abs(val) >= 500


def index_label_values_from_rows(
    body_rows: list[list[str]],
    value_col: int | None,
    *,
    header_rows: list[list[str]] | None = None,
    year: int | None = None,
    entity_column: str = "group",
    unit_scale: float = 1.0,
) -> dict[str, float]:
    """
    Pair each line-item label with its GROUP/BANK amount for the target year.

    When header_rows and year are given, each row picks its own value column
  (skipping note refs and page numbers that vary row-to-row).
    """
    index: dict[str, float] = {}
    pending_label = ""

    for row in body_rows:
        if not row:
            continue
        col0 = (row[0] or "").strip()

        if col0:
            if _is_fs_topic_label(col0):
                pending_label = ""
                continue
            pending_label = col0

        row_col = value_col
        if header_rows and year is not None:
            picked = row_entity_year_col(row, header_rows, year, entity_column)
            if picked is not None:
                row_col = picked
        if row_col is None:
            continue

        if not _is_annual_fs_value_row(row, row_col):
            continue

        val = parse_number(row[row_col])
        if val is None or not pending_label:
            continue
        if _is_likely_page_no_value(str(row[row_col]), val):
            continue
        if year is not None and _is_likely_year_header_value(val, year):
            continue

        nl = norm_label(pending_label)
        if nl and nl not in index:
            index[nl] = val * unit_scale
        pending_label = ""

    return index


def _index_table(
    header_rows: list[list[str]],
    body_rows: list[list[str]],
    year: int,
    entity_column: str,
) -> dict[str, float]:
    # Prefer explicit GROUP/BANK + year header mapping — more reliable than the
    # generic entity-year heuristic on multi-column bank annual statements.
    group_ci, bank_ci = _entity_header_positions(header_rows)
    if entity_column.lower() == "bank":
        col = find_annual_bank_body_col(header_rows, body_rows, year)
        if col is None:
            col = _entity_year_col(header_rows, body_rows, year, entity_column)
    else:
        col = find_annual_group_body_col(header_rows, body_rows, year, entity_column)
        if col is None:
            col = _entity_year_col(header_rows, body_rows, year, entity_column)
    if col is None:
        return {}
    # When GROUP/BANK headers are present, keep the fixed column — per-row
    # re-detection often restarts at the first amount (GROUP) even for BANK.
    use_row_pick = not (group_ci is not None and bank_ci is not None)
    return index_label_values_from_rows(
        body_rows,
        col,
        header_rows=header_rows if use_row_pick else None,
        year=year if use_row_pick else None,
        entity_column=entity_column,
    )


def _label_tokens(s: str) -> set[str]:
    stop = {"the", "and", "of", "on", "a", "an", "to", "from", "less", "net", "gross"}
    return {t for t in norm_label(s).split() if t and t not in stop}


def _label_similarity(a: str, b: str) -> float:
    ta, tb = _label_tokens(a), _label_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def map_labels_to_values(
    index: dict[str, float],
    expected_labels: list[str],
    *,
    min_score: float = 0.45,
) -> dict[str, float | None]:
    out: dict[str, float | None] = {lbl: None for lbl in expected_labels}
    exp_norm = {norm_label(l): l for l in expected_labels}
    used_keys: set[str] = set()

    for nl, val in index.items():
        if nl in exp_norm:
            out[exp_norm[nl]] = val
            used_keys.add(nl)

    for en, orig in exp_norm.items():
        if out[orig] is not None:
            continue
        best_key: str | None = None
        best_score = 0.0
        for nl, v in index.items():
            if nl in used_keys:
                continue
            if nl in en or en in nl:
                score = 0.9
            else:
                score = _label_similarity(en, nl)
            if score > best_score:
                best_score = score
                best_key = nl
        if best_key and best_score >= min_score:
            out[orig] = index[best_key]
            used_keys.add(best_key)
    return out


def extract_tables_from_page(pdf_path: Path, page_num: int) -> list[dict[str, Any]]:
    """Return parsed tables from one PDF page."""
    if pdfplumber is None:
        return []
    tables: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if page_num < 1 or page_num > len(pdf.pages):
                return []
            page = pdf.pages[page_num - 1]
            rows = _words_table_rows(page)
            if not rows:
                return []
            header_rows, body_rows = _split_header_body(rows)
            tables.append(
                {
                    "page": page_num,
                    "header_rows": header_rows,
                    "body_rows": body_rows,
                    "raw_rows": rows,
                }
            )
    except Exception:
        return []
    return tables


def _note_ref_from_page(pdf_path: Path, page_num: int) -> str | None:
    if fitz is None:
        return None
    try:
        with fitz.open(str(pdf_path)) as doc:
            text = doc[page_num - 1].get_text() or ""
        m = re.search(r"\b(\d{1,2}\.\d{1,2})\b", text[:800])
        return m.group(1) if m else None
    except Exception:
        return None


def extract_note_group_from_pdf(
    pdf_path: Path,
    parent_label: str,
    child_labels: list[str],
    year: int,
    *,
    entity_column: str = "bank",
    note_hint: str | None = None,
    max_pages: int = 6,
) -> dict[str, Any]:
    """Extract parent + children from PDF note pages (local, no API)."""
    all_labels = [parent_label] + list(child_labels)
    pages = find_note_pages(pdf_path, parent_label, note_hint=note_hint, max_pages=max_pages)
    if not pages:
        return {
            "ok": False,
            "values": {lbl: None for lbl in all_labels},
            "attempts": 0,
            "reason": "no_pages_found",
            "method": "pdf_local",
        }

    best: dict[str, float | None] = {lbl: None for lbl in all_labels}
    best_page: int | None = None
    note_ref: str | None = None

    for page_num in pages:
        for table in extract_tables_from_page(pdf_path, page_num):
            index = _index_table(
                table["header_rows"],
                table["body_rows"],
                year,
                entity_column,
            )
            if not index:
                continue
            mapped = map_labels_to_values(index, all_labels)
            for lbl, val in mapped.items():
                if val is not None:
                    best[lbl] = val
            filled = sum(1 for v in best.values() if v is not None)
            if filled > 0:
                best_page = page_num
                note_ref = _note_ref_from_page(pdf_path, page_num)
            if filled >= max(2, len(child_labels) // 2 + 1):
                return {
                    "ok": True,
                    "values": best,
                    "attempts": 1,
                    "page": page_num,
                    "note_ref": note_ref,
                    "pages_tried": pages,
                    "method": "pdf_local",
                }

    filled = sum(1 for v in best.values() if v is not None)
    return {
        "ok": filled >= max(1, len(child_labels) // 3),
        "values": best,
        "attempts": len(pages),
        "page": best_page,
        "note_ref": note_ref,
        "reason": "partial" if filled else "incomplete_extraction",
        "pages_tried": pages,
        "method": "pdf_local",
    }


def extract_note_group_from_db(
    db,
    company_slug: str,
    year: int,
    parent_label: str,
    child_labels: list[str],
    *,
    entity_column: str = "bank",
) -> dict[str, float | None]:
    """Scan all annual financial_tables for note breakdown labels."""
    from generate_comb_model import find_value_col, norm_label as g_norm

    all_labels = [parent_label] + list(child_labels)
    merged: dict[str, float | None] = {lbl: None for lbl in all_labels}
    exp_norm = {norm_label(l): l for l in all_labels}

    docs = list(
        db.financial_tables.find(
            {
                "company_slug": company_slug,
                "year": year,
                "report_type": "annual",
            }
        )
    )

    for doc in docs:
        col = find_value_col(doc, year)
        if col is None:
            continue
        for row in doc.get("rows") or []:
            cells = row.get("cells") if isinstance(row, dict) else []
            if not cells or not cells[0] or col >= len(cells):
                continue
            label = str(cells[0]).strip()
            val = parse_number(cells[col])
            if val is None:
                continue
            nl = g_norm(label)
            if nl in exp_norm:
                merged[exp_norm[nl]] = val
            else:
                for en, orig in exp_norm.items():
                    if nl in en or en in nl:
                        if merged[orig] is None:
                            merged[orig] = val
                        break
    return merged


def extract_note_group_with_retries(
    pdf_path: Path | None,
    parent_label: str,
    child_labels: list[str],
    year: int,
    *,
    db=None,
    company_slug: str | None = None,
    entity_column: str = "bank",
    note_hint: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """
    Multi-source note extraction: financial_tables first, then PDF pages.
    Compatible return shape with the former OpenAI-based helper.
    """
    all_labels = [parent_label] + list(child_labels)
    best: dict[str, float | None] = {lbl: None for lbl in all_labels}

    if db is not None and company_slug:
        db_vals = extract_note_group_from_db(
            db,
            company_slug,
            year,
            parent_label,
            child_labels,
            entity_column=entity_column,
        )
        for lbl, val in db_vals.items():
            if val is not None:
                best[lbl] = val

    filled = sum(1 for v in best.values() if v is not None)
    if filled >= max(2, len(child_labels) // 2 + 1):
        return {
            "ok": True,
            "values": best,
            "attempts": 1,
            "method": "financial_tables",
            "source_collection": "financial_tables",
        }

    if pdf_path and pdf_path.exists():
        pdf_meta = extract_note_group_from_pdf(
            pdf_path,
            parent_label,
            child_labels,
            year,
            entity_column=entity_column,
            note_hint=note_hint,
        )
        for lbl, val in (pdf_meta.get("values") or {}).items():
            if val is not None:
                best[lbl] = val
        filled = sum(1 for v in best.values() if v is not None)
        if filled > 0:
            pdf_meta["values"] = best
            pdf_meta["ok"] = filled >= max(1, len(child_labels) // 3)
            pdf_meta["source_collection"] = "financial_tables+pdf_notes"
            return pdf_meta

    return {
        "ok": False,
        "values": best,
        "attempts": max_attempts,
        "reason": "no_extraction",
        "method": "local",
        "pages_tried": [],
    }


def confirm_label_absent(
    pdf_path: Path | None,
    label: str,
    year: int,
    *,
    pages: list[int] | None = None,
    parent_label: str | None = None,
) -> bool:
    """
    Return True when the label cannot be found on note pages (confirmed absent).
    Uses PDF text search only — no API calls.
    """
    if not pdf_path or not pdf_path.exists() or fitz is None:
        return False

    if not pages and parent_label:
        pages = find_note_pages(pdf_path, parent_label)[:4]
    if not pages:
        pages = find_note_pages(pdf_path, label)[:4]
    if not pages:
        return False

    needle = norm_label(label)
    year_s = str(year)

    with fitz.open(str(pdf_path)) as doc:
        for page_num in pages:
            if page_num < 1 or page_num > len(doc):
                continue
            text = norm_label(doc[page_num - 1].get_text() or "")
            if needle not in text:
                # try shortened needle (first 4 words)
                parts = needle.split()
                short = " ".join(parts[:4]) if len(parts) > 4 else needle
                if short not in text:
                    continue
            # label text exists on page — check for a nearby year + number pattern
            if year_s in text:
                return False
            # label found but no year column context — treat as not confirmed absent
            return False
    return True
