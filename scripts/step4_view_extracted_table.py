"""
step4_view_extracted_table.py
=============================
STEP 4 - View / compare extracted JSON tables in a browser.

What it does
------------
  1. Scans   <backend>/Extracted_json/
     for company subfolders that contain  <company>_results.json
     (created by step3_send_to_openai.py).
  2. For every company, renders each financial statement as a real HTML
     TABLE (not just a JSON dump) so you can compare visually with the PDF.
  3. Beside every extracted table it embeds the original captured PNG page(s)
     from  <backend>/json_logs/<company>_captures/<statement>/
     so you can eyeball the AI output vs the original page.
  4. Builds:
        Extracted_json\\index.html                  <- list of all companies
        Extracted_json\\<company>\\<company>_view.html   <- detailed view
  5. Optional:  --serve   starts a local web server so images load reliably.

Typical usage
-------------
    # Build the HTML viewer files
    python step4_view_extracted_table.py

    # Build + open a local web server on http://localhost:8000/
    python step4_view_extracted_table.py --serve

    # Only rebuild one company
    python step4_view_extracted_table.py --only company1

    # Use a different extracted-json folder
    python step4_view_extracted_table.py --extracted D:\\some\\where\\Extracted_json
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import socketserver
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_SCRIPT_DIR           = Path(__file__).resolve().parent
_BACKEND_DIR          = _SCRIPT_DIR.parent
DEFAULT_EXTRACTED_DIR = _BACKEND_DIR / "Extracted_json"
DEFAULT_CAPTURES_BASE = _BACKEND_DIR / "json_logs"
DEFAULT_SERVE_ROOT    = _BACKEND_DIR   # parent of BOTH Extracted_json AND json_logs so images load

STMT_ORDER = [
    "income_statement", "oci", "sofp", "equity", "cash_flows",
    "shareholder_info", "investor_info", "ten_year_summary",
    "five_year_summary", "notes",
]

STMT_LABELS = {
    "income_statement":  "Income Statement",
    "oci":               "Other Comprehensive Income (OCI)",
    "sofp":              "Statement of Financial Position",
    "equity":            "Statement of Changes in Equity",
    "cash_flows":        "Statement of Cash Flows",
    "shareholder_info":  "Shareholder Information",
    "investor_info":     "Investor Information",
    "ten_year_summary":  "Ten Year Summary",
    "five_year_summary": "Five Year Summary",
    "notes":             "Notes to the Financial Statements",
}

STMT_COLORS = {
    "income_statement":  "#1a6b3c",
    "oci":               "#2e7d8a",
    "sofp":              "#1e3a8a",
    "equity":            "#6b21a8",
    "cash_flows":        "#92400e",
    "shareholder_info":  "#065f46",
    "investor_info":     "#0c4a6e",
    "ten_year_summary":  "#78350f",
    "five_year_summary": "#831843",
    "notes":             "#374151",
}


# ─────────────────────────────────────────────────────────────────────────────
# Small formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def esc(x) -> str:
    if x is None:
        return '<span class="muted">-</span>'
    if isinstance(x, (int, float)):
        return fmt_number(x)
    return html.escape(str(x))


def esc_cell(x) -> str:
    """Like esc(), but preserves newlines printed in the PDF as <br/>.

    GPT is instructed to insert a literal "\\n" wherever a printed cell wraps
    onto a second visual line in the PDF (e.g. long OCI section banners).
    We render that as <br/> so the on-screen layout matches the PDF.
    Also handles real CR/LF if any slip through.
    """
    if x is None:
        return '<span class="muted">-</span>'
    if isinstance(x, (int, float)):
        return fmt_number(x)
    s = str(x)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    parts = s.split("\n")
    escaped = [html.escape(p) for p in parts]
    return "<br/>".join(escaped)


def fmt_number(v) -> str:
    if v is None or v == "":
        return '<span class="muted">-</span>'
    try:
        n = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    if n == int(n):
        s = f"{int(n):,}"
    else:
        s = f"{n:,.2f}"
    if n < 0:
        return f'<span class="neg">({s.lstrip("-")})</span>'
    return s


def render_kv(d: dict) -> str:
    if not isinstance(d, dict) or not d:
        return ""
    rows = "".join(
        f"<tr><td class='lbl'>{esc(k)}</td><td class='val'>{esc(v)}</td></tr>"
        for k, v in d.items()
    )
    return f"<table class='kv'>{rows}</table>"


def render_json_pre(obj) -> str:
    return (
        '<details class="rawjson"><summary>Show raw JSON</summary>'
        f'<pre>{html.escape(json.dumps(obj, indent=2, ensure_ascii=False))}</pre>'
        '</details>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Verbatim raw-table renderer  (NEW format produced by step3)
#
# Each statement's "data" looks like:
#   {
#     "statement_title": "INCOME STATEMENT",
#     "preamble":  "For the year ended 31st March",
#     "footnotes": "Figures in brackets indicate ...",
#     "tables": [
#       {
#         "caption":     "ASSETS" | null,
#         "header_rows": [["", "Note", "2025", "2024"], ["", "", "Rs.", "Rs."]],
#         "rows": [
#           {"cells": ["Income", "4", "4,727,585,483", "4,868,237,960"],
#            "style": "data"},
#           {"cells": ["EXPENSES", "", "", ""], "style": "section"},
#           ...
#         ]
#       }
#     ]
#   }
# ─────────────────────────────────────────────────────────────────────────────

def _is_raw_tables_format(data) -> bool:
    if not isinstance(data, dict):
        return False
    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        return False
    first = tables[0]
    if not isinstance(first, dict):
        return False
    return "header_rows" in first or (
        "rows" in first and isinstance(first.get("rows"), list)
        and first["rows"] and isinstance(first["rows"][0], dict)
        and "cells" in first["rows"][0]
    )


def _detect_note_col(header_rows: list, body_rows: list, width: int) -> int:
    """Return the index of the 'Note' column, or -1 if there isn't one.

    Looks at every header row for a cell whose text == 'Note' (case-insensitive).
    Falls back to checking if column index 1 looks like a note column
    (short ref-ish strings like '4', '17', '13.1', '(Note 34.1)').
    """
    for hr in header_rows or []:
        if not isinstance(hr, list):
            continue
        for i, c in enumerate(hr):
            if isinstance(c, str) and c.strip().lower() == "note":
                return i
    # Heuristic: if col 1 mostly contains short ref-like strings, treat it
    # as a note column.
    if width >= 3:
        col_vals = []
        for r in body_rows or []:
            cells = (r or {}).get("cells") or []
            if len(cells) > 1:
                col_vals.append(str(cells[1]).strip())
        non_empty = [v for v in col_vals if v]
        if non_empty:
            short_refs = sum(
                1 for v in non_empty
                if len(v) <= 10 and (
                    v.replace(".", "").isdigit() or
                    v.lower().startswith("(note")
                )
            )
            if short_refs / max(1, len(non_empty)) >= 0.6:
                return 1
    return -1


def _render_header_row_with_spans(hr: list, width: int, note_col: int) -> str:
    """Render a single printed header line.

    GPT is instructed to encode "grouped" headers (e.g. "As at 31st March 2025"
    spanning three sub-columns) by placing the spanning label in its leftmost
    column and leaving the other spanned columns empty ("").

    We detect that pattern: a non-empty cell followed by one or more empty
    cells gets merged into a single <th colspan="..."> so the rendered table
    visually matches the PDF.

    The Note column (if any) is NEVER merged into another span - it always
    renders on its own so the column alignment stays stable.
    """
    cells = list(hr) + [""] * max(0, width - len(hr))
    cells = cells[:width]

    out = ""
    i = 0
    while i < width:
        val = cells[i]
        span = 1
        if str(val).strip():
            j = i + 1
            while j < width and not str(cells[j]).strip():
                if j == note_col:
                    break
                span += 1
                j += 1
        classes = ["lbl"] if i == 0 else ["num"]
        if i == note_col and span == 1:
            classes = ["notecol"]
        colspan_attr = f" colspan='{span}'" if span > 1 else ""
        cls_attr = " ".join(classes)
        out += f"<th class='{cls_attr}'{colspan_attr}>{esc_cell(val)}</th>"
        i += span
    return f"<tr>{out}</tr>"


def _normalize_row_width(cells: list, width: int) -> list:
    """Pad a row out to width N. Padding goes at the END.

    The prompt now instructs GPT to keep every row at exactly width N already,
    inserting "" for missing middle columns. This is purely a defensive
    fallback so that under-wide rows don't visually shift the table.
    """
    out = list(cells)
    while len(out) < width:
        out.append("")
    return out[:width]


def _render_one_raw_table(tbl: dict) -> str:
    if not isinstance(tbl, dict):
        return ""

    caption     = tbl.get("caption")
    header_rows = tbl.get("header_rows") or []
    body_rows   = tbl.get("rows") or []

    # Width = max number of cells across all header & body rows
    width = 0
    for hr in header_rows:
        if isinstance(hr, list):
            width = max(width, len(hr))
    for r in body_rows:
        cells = (r or {}).get("cells") or []
        if isinstance(cells, list):
            width = max(width, len(cells))
    if width == 0:
        return ""

    note_col = _detect_note_col(header_rows, body_rows, width)

    # THEAD - one row per printed header line, with colspan grouping
    thead_html = ""
    for hr in header_rows:
        if not isinstance(hr, list):
            continue
        thead_html += _render_header_row_with_spans(hr, width, note_col)

    # TBODY - keep rows in exact order with the style class GPT assigned
    tbody_html = ""
    for r in body_rows:
        if not isinstance(r, dict):
            continue
        style = (r.get("style") or "data").lower()
        cls = {
            "section":  "section",
            "subtotal": "subtotal",
            "total":    "total",
            "blank":    "blank",
        }.get(style, "")
        cells_list = _normalize_row_width(r.get("cells") or [], width)

        # Section banners often span the whole row in the PDF -
        # if only the first cell is non-empty, span it across all columns.
        non_empty = [c for c in cells_list if str(c).strip()]
        if style == "section" and len(non_empty) == 1 and str(cells_list[0]).strip():
            tbody_html += (
                f"<tr class='{cls}'><td class='lbl section-banner' "
                f"colspan='{width}'>{esc_cell(cells_list[0])}</td></tr>"
            )
            continue
        if style == "blank":
            tbody_html += (
                f"<tr class='blank'><td colspan='{width}'>&nbsp;</td></tr>"
            )
            continue

        def _cell_class(i: int) -> str:
            if i == 0:
                return "lbl"
            if i == note_col:
                return "notecol"
            return "num"

        cells_html = "".join(
            f"<td class='{_cell_class(i)}'>{esc_cell(c)}</td>"
            for i, c in enumerate(cells_list)
        )
        tbody_html += f"<tr class='{cls}'>{cells_html}</tr>"

    caption_html = f"<h4 class='tbl-caption'>{esc_cell(caption)}</h4>" if caption else ""
    return (
        f"{caption_html}"
        f"<table class='fin verbatim'>"
        f"<thead>{thead_html}</thead>"
        f"<tbody>{tbody_html}</tbody>"
        f"</table>"
    )


def render_raw_tables(data: dict) -> str:
    out = ""
    title = data.get("statement_title")
    if title:
        out += f"<p class='unit stmt-title'>{esc_cell(title)}</p>"
    preamble = data.get("preamble")
    if preamble:
        out += f"<p class='preamble'>{esc_cell(preamble)}</p>"
    for tbl in (data.get("tables") or []):
        out += _render_one_raw_table(tbl)
    footnotes = data.get("footnotes")
    if footnotes:
        out += f"<p class='footnotes'>{esc_cell(footnotes)}</p>"
    return out or "<p class='muted'>No tables transcribed.</p>"


# ─────────────────────────────────────────────────────────────────────────────
# Per-statement table renderers
# ─────────────────────────────────────────────────────────────────────────────

def render_line_items_table(periods: list, sections: list, label_col="Item") -> str:
    """Render a table with rows=line_items grouped by section, cols=periods."""
    if not periods:
        return "<p class='muted'>No periods.</p>"
    head = "<th class='lbl'>" + html.escape(label_col) + "</th>"
    head += "<th class='ref'>Note</th>"
    head += "".join(f"<th>{esc(p)}</th>" for p in periods)
    body = ""
    for sec in (sections or []):
        sec_name = sec.get("section_name") or sec.get("name") or ""
        if sec_name:
            body += (
                f"<tr class='section'><td colspan='{len(periods)+2}'>"
                f"{esc(sec_name)}</td></tr>"
            )
        for li in (sec.get("line_items") or []):
            label  = li.get("label", "")
            note   = li.get("note_ref") or ""
            vals   = li.get("values") or {}
            cells  = "".join(f"<td class='num'>{fmt_number(vals.get(p))}</td>"
                             for p in periods)
            body += (
                f"<tr><td class='lbl'>{esc(label)}</td>"
                f"<td class='ref'>{esc(note)}</td>{cells}</tr>"
            )
    return f"<table class='fin'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_income_statement(data: dict) -> str:
    periods  = data.get("periods", [])
    sections = data.get("sections", [])
    unit     = data.get("currency_unit", "")
    out  = f"<p class='unit'>Unit: {esc(unit)}</p>" if unit else ""
    out += render_line_items_table(periods, sections, label_col="Income Statement")
    pbt = data.get("profit_before_tax")
    pat = data.get("profit_after_tax")
    summary = []
    if pbt: summary.append(("Profit before tax", pbt))
    if pat: summary.append(("Profit after tax",  pat))
    if summary:
        head = "<th class='lbl'>Summary</th>" + "".join(
            f"<th>{esc(p)}</th>" for p in periods)
        rows = ""
        for label, vals in summary:
            cells = "".join(
                f"<td class='num bold'>{fmt_number((vals or {}).get(p))}</td>"
                for p in periods)
            rows += f"<tr><td class='lbl bold'>{esc(label)}</td>{cells}</tr>"
        out += (f"<table class='fin summary'><thead><tr>{head}</tr></thead>"
                f"<tbody>{rows}</tbody></table>")
    return out


def render_oci(data: dict) -> str:
    periods = data.get("periods", [])
    unit    = data.get("currency_unit", "")
    out = f"<p class='unit'>Unit: {esc(unit)}</p>" if unit else ""

    rows = ""
    pfy = data.get("profit_for_year") or {}
    if any(pfy.values() if isinstance(pfy, dict) else []):
        cells = "".join(f"<td class='num bold'>{fmt_number(pfy.get(p))}</td>"
                        for p in periods)
        rows += f"<tr><td class='lbl bold'>Profit for the year</td><td class='ref'></td>{cells}</tr>"

    for li in (data.get("oci_items") or []):
        label = li.get("label", "")
        note  = li.get("note_ref") or ""
        vals  = li.get("values") or {}
        cells = "".join(f"<td class='num'>{fmt_number(vals.get(p))}</td>"
                        for p in periods)
        rows += (
            f"<tr><td class='lbl'>{esc(label)}</td>"
            f"<td class='ref'>{esc(note)}</td>{cells}</tr>"
        )

    for k, lbl in (("total_oci", "Total OCI"),
                   ("total_comprehensive_income", "Total Comprehensive Income")):
        v = data.get(k)
        if v:
            cells = "".join(
                f"<td class='num bold'>{fmt_number((v or {}).get(p))}</td>"
                for p in periods)
            rows += (f"<tr class='total'><td class='lbl bold'>{esc(lbl)}</td>"
                     f"<td class='ref'></td>{cells}</tr>")

    head = ("<th class='lbl'>OCI</th><th class='ref'>Note</th>" +
            "".join(f"<th>{esc(p)}</th>" for p in periods))
    out += f"<table class='fin'><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    return out


def _render_sofp_subsection(name: str, line_items: list, dates: list) -> str:
    if not line_items:
        return ""
    head = (f"<th class='lbl'>{esc(name)}</th><th class='ref'>Note</th>" +
            "".join(f"<th>{esc(d)}</th>" for d in dates))
    body = ""
    for li in line_items:
        vals = li.get("values") or {}
        cells = "".join(f"<td class='num'>{fmt_number(vals.get(d))}</td>"
                        for d in dates)
        body += (f"<tr><td class='lbl'>{esc(li.get('label',''))}</td>"
                 f"<td class='ref'>{esc(li.get('note_ref') or '')}</td>{cells}</tr>")
    return f"<table class='fin'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_sofp(data: dict) -> str:
    dates = data.get("dates") or data.get("periods", [])
    unit  = data.get("currency_unit", "")
    out = f"<p class='unit'>Unit: {esc(unit)}</p>" if unit else ""

    assets = data.get("assets") or {}
    liabs  = data.get("liabilities") or {}
    eq     = data.get("equity") or {}

    out += "<h4>Assets</h4>"
    out += _render_sofp_subsection("Non-current assets",
                                   assets.get("non_current_assets", []), dates)
    out += _render_sofp_subsection("Current assets",
                                   assets.get("current_assets", []), dates)
    if assets.get("total_assets"):
        out += render_total_row("Total Assets", assets["total_assets"], dates)

    out += "<h4>Liabilities</h4>"
    out += _render_sofp_subsection("Non-current liabilities",
                                   liabs.get("non_current_liabilities", []), dates)
    out += _render_sofp_subsection("Current liabilities",
                                   liabs.get("current_liabilities", []), dates)
    if liabs.get("total_liabilities"):
        out += render_total_row("Total Liabilities", liabs["total_liabilities"], dates)

    out += "<h4>Equity</h4>"
    out += _render_sofp_subsection("Components of equity",
                                   eq.get("components", []), dates)
    if eq.get("total_equity"):
        out += render_total_row("Total Equity", eq["total_equity"], dates)

    if data.get("total_equity_and_liabilities"):
        out += render_total_row("Total Equity & Liabilities",
                                data["total_equity_and_liabilities"], dates)
    return out


def render_total_row(label: str, vals: dict, periods: list) -> str:
    cells = "".join(f"<td class='num bold'>{fmt_number((vals or {}).get(p))}</td>"
                    for p in periods)
    head  = f"<th class='lbl'>{esc(label)}</th><th class='ref'></th>" + \
            "".join(f"<th>{esc(p)}</th>" for p in periods)
    return (f"<table class='fin summary'><thead><tr>{head}</tr></thead>"
            f"<tbody><tr><td class='lbl bold'>{esc(label)}</td>"
            f"<td class='ref'></td>{cells}</tr></tbody></table>")


def render_equity(data: dict) -> str:
    cols    = data.get("columns") or []
    periods = data.get("periods") or []
    unit    = data.get("currency_unit", "")
    out = f"<p class='unit'>Unit: {esc(unit)}</p>" if unit else ""
    for prd in periods:
        out += f"<h4>{esc(prd.get('period', ''))}</h4>"
        head = "<th class='lbl'>Movement</th>" + "".join(
            f"<th>{esc(c)}</th>" for c in cols)
        body = ""
        for r in (prd.get("rows") or []):
            vals = r.get("values") or {}
            cells = "".join(f"<td class='num'>{fmt_number(vals.get(c))}</td>"
                            for c in cols)
            body += f"<tr><td class='lbl'>{esc(r.get('label',''))}</td>{cells}</tr>"
        out += (f"<table class='fin'><thead><tr>{head}</tr></thead>"
                f"<tbody>{body}</tbody></table>")
    return out


def _render_cf_group(group_label: str, group_data: dict, periods: list) -> str:
    if not group_data:
        return ""
    items = group_data.get("line_items") or []
    head = (f"<th class='lbl'>{esc(group_label)}</th><th class='ref'>Note</th>" +
            "".join(f"<th>{esc(p)}</th>" for p in periods))
    body = ""
    for li in items:
        vals = li.get("values") or {}
        cells = "".join(f"<td class='num'>{fmt_number(vals.get(p))}</td>"
                        for p in periods)
        body += (f"<tr><td class='lbl'>{esc(li.get('label',''))}</td>"
                 f"<td class='ref'>{esc(li.get('note_ref') or '')}</td>{cells}</tr>")
    net = group_data.get("net_cash") or {}
    if net:
        cells = "".join(f"<td class='num bold'>{fmt_number(net.get(p))}</td>"
                        for p in periods)
        body += (f"<tr class='total'><td class='lbl bold'>Net cash</td>"
                 f"<td class='ref'></td>{cells}</tr>")
    return f"<table class='fin'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_cash_flows(data: dict) -> str:
    periods = data.get("periods", [])
    unit    = data.get("currency_unit", "")
    out = f"<p class='unit'>Unit: {esc(unit)}</p>" if unit else ""
    out += _render_cf_group("Operating activities",
                            data.get("operating_activities"), periods)
    out += _render_cf_group("Investing activities",
                            data.get("investing_activities"), periods)
    out += _render_cf_group("Financing activities",
                            data.get("financing_activities"), periods)
    for k, lbl in (("net_increase_in_cash", "Net increase in cash"),
                   ("opening_cash",         "Opening cash"),
                   ("closing_cash",         "Closing cash")):
        v = data.get(k)
        if v:
            out += render_total_row(lbl, v, periods)
    return out


def render_shareholder_info(data: dict) -> str:
    out = ""
    if data.get("as_at_date"):
        out += f"<p class='unit'>As at {esc(data['as_at_date'])}</p>"
    if data.get("total_shares") is not None:
        out += f"<p class='unit'>Total shares: {fmt_number(data['total_shares'])}</p>"

    top = data.get("top_shareholders") or []
    if top:
        out += "<h4>Top shareholders</h4>"
        rows = "".join(
            f"<tr><td class='num'>{esc(s.get('rank'))}</td>"
            f"<td class='lbl'>{esc(s.get('name',''))}</td>"
            f"<td class='num'>{fmt_number(s.get('shares'))}</td>"
            f"<td class='num'>{fmt_number(s.get('percentage'))}</td></tr>"
            for s in top
        )
        out += ("<table class='fin'><thead><tr><th>#</th><th>Name</th>"
                "<th>Shares</th><th>%</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    dist = data.get("share_distribution") or []
    if dist:
        out += "<h4>Share distribution</h4>"
        rows = "".join(
            f"<tr><td class='lbl'>{esc(d.get('range',''))}</td>"
            f"<td class='num'>{fmt_number(d.get('holders'))}</td>"
            f"<td class='num'>{fmt_number(d.get('shares'))}</td>"
            f"<td class='num'>{fmt_number(d.get('percentage'))}</td></tr>"
            for d in dist
        )
        out += ("<table class='fin'><thead><tr><th>Range</th><th>Holders</th>"
                "<th>Shares</th><th>%</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    mp = data.get("market_price") or {}
    if any(mp.values()) if isinstance(mp, dict) else False:
        out += "<h4>Market price</h4>" + render_kv(mp)

    other = data.get("other_info") or {}
    if isinstance(other, dict) and other:
        out += "<h4>Other</h4>" + render_kv(other)

    if data.get("public_holding_percentage") is not None:
        out += (f"<p class='unit'>Public holding: "
                f"{fmt_number(data['public_holding_percentage'])}%</p>")
    return out


def render_investor_info(data: dict) -> str:
    periods = data.get("periods", [])
    metrics = data.get("metrics") or []
    if not metrics:
        return "<p class='muted'>No metrics.</p>"
    head = "<th class='lbl'>Metric</th>" + "".join(f"<th>{esc(p)}</th>" for p in periods)
    body = ""
    for m in metrics:
        vals = m.get("values") or {}
        cells = "".join(f"<td class='num'>{fmt_number(vals.get(p))}</td>"
                        for p in periods)
        body += f"<tr><td class='lbl'>{esc(m.get('label',''))}</td>{cells}</tr>"
    return f"<table class='fin'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_year_summary(data: dict) -> str:
    years    = data.get("years", [])
    sections = data.get("sections", [])
    unit     = data.get("currency_unit", "")
    out  = f"<p class='unit'>Unit: {esc(unit)}</p>" if unit else ""
    out += render_line_items_table(years, sections, label_col="Metric")
    return out


def render_notes(data: dict) -> str:
    notes = data.get("notes") or []
    if not notes:
        return "<p class='muted'>No notes parsed.</p>"
    out = ""
    for n in notes:
        title = f"Note {n.get('number','')} - {n.get('title','')}".strip(" -")
        out  += f"<details class='note'><summary>{esc(title)}</summary>"
        if n.get("summary"):
            out += f"<p>{esc(n['summary'])}</p>"
        ka = n.get("key_amounts") or []
        if ka:
            rows = "".join(
                f"<tr><td class='lbl'>{esc(a.get('label',''))}</td>"
                f"<td class='num'>{fmt_number(a.get('value'))}</td>"
                f"<td class='ref'>{esc(a.get('period',''))}</td></tr>"
                for a in ka
            )
            out += ("<table class='fin'><thead><tr><th>Item</th><th>Value</th>"
                    "<th>Period</th></tr></thead>"
                    f"<tbody>{rows}</tbody></table>")
        for t in (n.get("tables") or []):
            cols = t.get("columns") or []
            head = "<th class='lbl'>Row</th>" + "".join(f"<th>{esc(c)}</th>" for c in cols)
            rows = ""
            for r in (t.get("rows") or []):
                vals = r.get("values") or {}
                cells = "".join(f"<td class='num'>{fmt_number(vals.get(c))}</td>"
                                for c in cols)
                rows += f"<tr><td class='lbl'>{esc(r.get('label',''))}</td>{cells}</tr>"
            if t.get("table_title"):
                out += f"<h5>{esc(t['table_title'])}</h5>"
            out += (f"<table class='fin'><thead><tr>{head}</tr></thead>"
                    f"<tbody>{rows}</tbody></table>")
        out += "</details>"
    return out


RENDERERS = {
    "income_statement":  render_income_statement,
    "oci":               render_oci,
    "sofp":              render_sofp,
    "equity":            render_equity,
    "cash_flows":        render_cash_flows,
    "shareholder_info":  render_shareholder_info,
    "investor_info":     render_investor_info,
    "ten_year_summary":  render_year_summary,
    "five_year_summary": render_year_summary,
    "notes":             render_notes,
}


def render_statement_body(key: str, body: dict) -> str:
    status = body.get("status", "")
    data   = body.get("data")

    if status == "dry_run":
        return ('<p class="warn">Dry-run - not actually sent to OpenAI.</p>'
                + render_json_pre(body))
    if status == "no_images":
        return '<p class="warn">No PNG images were available for this statement.</p>'
    if status == "api_error":
        return (f'<p class="err">API error: {esc(body.get("error",""))}</p>'
                + render_json_pre(body))

    if data is None or not isinstance(data, (dict, list)):
        raw = body.get("raw_response", "")
        return (f'<p class="err">JSON parse failed - showing raw response.</p>'
                f'<pre class="raw">{html.escape(str(raw)[:6000])}</pre>')

    if isinstance(data, dict) and data.get("status") == "multi_batch":
        out = '<p class="warn">Multi-batch result. Showing each batch.</p>'
        for i, b in enumerate(data.get("batches", []), 1):
            out += f"<h4>Batch {i}</h4>"
            try:
                if _is_raw_tables_format(b):
                    out += render_raw_tables(b)
                else:
                    out += RENDERERS.get(key, render_json_pre)(b)
            except Exception:
                out += render_json_pre(b)
        return out

    # Prefer verbatim renderer when GPT returned the new "raw tables" format.
    if _is_raw_tables_format(data):
        try:
            rendered = render_raw_tables(data)
        except Exception as e:
            rendered = (f'<p class="err">Verbatim renderer error: {esc(e)}</p>'
                        + render_json_pre(data))
        return rendered + render_json_pre(data)

    try:
        rendered = RENDERERS.get(key, lambda d: render_json_pre(d))(data)
    except Exception as e:
        rendered = (f'<p class="err">Renderer error: {esc(e)}</p>'
                    + render_json_pre(data))
    return rendered + render_json_pre(data)


# ─────────────────────────────────────────────────────────────────────────────
# Image helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_capture_images(captures_base: Path, company: str, stmt: str) -> list[Path]:
    folder = captures_base / f"{company}_captures" / stmt
    if not folder.exists():
        return []
    return sorted(folder.glob("page_*.png"))


def relpath_from(view_html: Path, target: Path) -> str:
    """Forward-slash relative path so browsers resolve images correctly."""
    try:
        rel = Path("/".join(target.resolve().parts)).relative_to(
            Path("/".join(view_html.parent.resolve().parts))
        )
        return str(rel).replace("\\", "/")
    except Exception:
        # Fall back to absolute file URL
        return target.resolve().as_uri()


def _try_relative(view_html: Path, target: Path) -> str:
    """Compute a clean forward-slash relative path; fall back to file:// URI."""
    try:
        import os
        rel = os.path.relpath(target.resolve(), view_html.parent.resolve())
        return rel.replace("\\", "/")
    except Exception:
        return target.resolve().as_uri()


# ─────────────────────────────────────────────────────────────────────────────
# HTML page builders
# ─────────────────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#0b0d12;--panel:#141820;--border:#1f2535;--text:#dde3f0;
  --muted:#5a6480;--accent:#22d3ee;--neg:#fca5a5;--ok:#34d399;
  --warn:#fbbf24;--err:#f87171;}
*{box-sizing:border-box;}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:var(--bg);color:var(--text);}
header{background:linear-gradient(90deg,#0c1220,#0b0d12);padding:1.2rem 2rem;
  border-bottom:1px solid var(--border);}
header h1{margin:0;font-size:1.4rem;color:#fff;}
header .meta{color:var(--muted);font-size:0.78rem;margin-top:0.25rem;}
main{padding:1.5rem 2rem;max-width:1500px;margin:0 auto;}
.nav{display:flex;flex-wrap:wrap;gap:0.4rem;margin-bottom:1.4rem;
  background:var(--panel);padding:0.6rem;border:1px solid var(--border);
  border-radius:8px;}
.nav a{padding:0.35rem 0.7rem;background:#1d2231;color:#cbd5e1;
  text-decoration:none;border-radius:5px;font-size:0.78rem;}
.nav a:hover{background:#2a3145;color:#fff;}
section.stmt{margin-bottom:2rem;background:var(--panel);
  border:1px solid var(--border);border-radius:10px;overflow:hidden;}
section.stmt > .hd{padding:0.75rem 1.1rem;display:flex;justify-content:space-between;
  align-items:center;color:#fff;font-weight:700;}
section.stmt > .hd .badge{font-size:0.65rem;text-transform:uppercase;
  background:rgba(0,0,0,0.25);padding:0.15rem 0.5rem;border-radius:4px;
  letter-spacing:0.04em;}
.split{display:grid;grid-template-columns:1fr 1fr;gap:1rem;padding:1rem;}
@media (max-width:1100px){.split{grid-template-columns:1fr;}}
.split > div{background:#0d1119;border:1px solid #1c2233;border-radius:6px;
  padding:0.9rem;overflow:auto;max-height:780px;}
.split h3{margin:0 0 0.6rem;color:#9ca3af;font-size:0.78rem;
  text-transform:uppercase;letter-spacing:0.06em;}
.split img{width:100%;border-radius:4px;border:1px solid #1c2233;
  margin-bottom:0.5rem;display:block;}
.split .pageno{color:var(--muted);font-size:0.7rem;margin:-0.3rem 0 0.5rem;
  text-align:center;}
table.fin{width:100%;border-collapse:collapse;font-size:0.78rem;
  margin:0.5rem 0 1rem;table-layout:auto;}
table.fin th,table.fin td{border-bottom:1px solid #1c2233;padding:0.42rem 0.55rem;
  vertical-align:top;}
table.fin thead th{background:#161b27;color:#9ca3af;text-align:right;
  font-weight:600;font-size:0.72rem;text-transform:uppercase;
  letter-spacing:0.04em;}
table.fin thead th.lbl,table.fin thead th.ref{text-align:left;}
table.fin td.num{text-align:right;font-variant-numeric:tabular-nums;
  white-space:nowrap;}
table.fin td.lbl{color:#e2e8f0;white-space:normal;word-break:break-word;
  hyphens:auto;}
table.fin td.ref{color:var(--muted);font-size:0.7rem;text-align:left;width:42px;}
table.fin tr.section td{background:#1a2030;color:#67e8f9;font-weight:700;
  text-transform:uppercase;letter-spacing:0.04em;font-size:0.72rem;}
table.fin tr.total td{background:#162132;font-weight:700;color:#fff;}
table.fin tr.subtotal td{background:#10182a;font-weight:600;color:#e2e8f0;}
table.fin tr.blank td{background:transparent;height:0.5rem;border-bottom:0;}
table.fin .bold{font-weight:700;color:#fff;}

/* Verbatim renderer - preserves multi-row headers exactly */
table.fin.verbatim{table-layout:auto;}
table.fin.verbatim thead th{text-align:right;font-weight:600;
  text-transform:none;letter-spacing:0;font-size:0.74rem;color:#cbd5e1;
  background:#161b27;border-bottom:1px solid #243046;
  white-space:normal;word-break:break-word;vertical-align:bottom;}
table.fin.verbatim thead th.lbl{text-align:left;}
table.fin.verbatim thead th[colspan]{text-align:center;
  border-bottom:1px solid #2a3245;color:#e2e8f0;font-weight:700;}
table.fin.verbatim thead tr:last-child th{border-bottom:1px solid #67e8f9;}
table.fin.verbatim td{font-size:0.78rem;}
table.fin.verbatim td.num{white-space:nowrap;text-align:right;}
table.fin.verbatim td.lbl{white-space:normal;word-break:normal;
  overflow-wrap:break-word;max-width:340px;line-height:1.35;}
/* Section banners can be long; let them wrap naturally onto multiple lines */
table.fin.verbatim td.section-banner{white-space:normal;line-height:1.4;
  text-transform:uppercase;letter-spacing:0.04em;}
/* Note column (only added when the table actually has one) */
table.fin.verbatim td.notecol,
table.fin.verbatim th.notecol{text-align:left;color:var(--muted);
  font-size:0.72rem;white-space:nowrap;width:1%;padding-right:0.9rem;}
.stmt-title{color:#fff;font-size:0.95rem;font-weight:700;
  letter-spacing:0.02em;margin:0 0 0.4rem;}
.preamble{color:#9ca3af;font-size:0.78rem;margin:0 0 0.6rem;font-style:italic;}
.footnotes{color:var(--muted);font-size:0.72rem;margin:0.7rem 0 0;
  font-style:italic;line-height:1.4;}
h4.tbl-caption{color:#67e8f9;margin:1.1rem 0 0.4rem;font-size:0.82rem;
  text-transform:uppercase;letter-spacing:0.05em;}
.neg{color:var(--neg);}
.muted{color:var(--muted);}
.unit{color:#67e8f9;font-size:0.72rem;margin:0 0 0.5rem;}
.warn{color:var(--warn);}.err{color:var(--err);}
pre.raw,details.rawjson pre{background:#0a0c12;color:#a5f3fc;
  padding:0.8rem;font-size:0.7rem;border-radius:6px;border:1px solid #1c2233;
  overflow:auto;max-height:380px;white-space:pre-wrap;word-break:break-word;}
details.rawjson{margin-top:0.6rem;}
details.rawjson summary,details.note summary{cursor:pointer;color:#9ca3af;
  font-size:0.72rem;padding:0.3rem 0;}
details.note{border:1px solid #1c2233;border-radius:6px;padding:0.4rem 0.8rem;
  margin:0.4rem 0;background:#0d1119;}
h4{color:#cbd5e1;margin:1rem 0 0.4rem;font-size:0.85rem;}
h5{color:#94a3b8;margin:0.6rem 0 0.3rem;font-size:0.78rem;}
table.kv{font-size:0.75rem;border-collapse:collapse;}
table.kv td{padding:0.25rem 0.6rem 0.25rem 0;}
table.kv td.lbl{color:var(--muted);}
.toplink{display:inline-block;margin-bottom:0.6rem;color:var(--accent);
  text-decoration:none;font-size:0.78rem;}
.toplink:hover{text-decoration:underline;}
"""


def build_company_view(company: str, results: dict, view_html: Path,
                       captures_base: Path) -> str:
    nav_links = "".join(
        f"<a href='#{k}'>{esc(STMT_LABELS.get(k, k))}</a>"
        for k in STMT_ORDER if k in results
    )

    body = ""
    for key in STMT_ORDER:
        if key not in results:
            continue
        node   = results[key]
        title  = STMT_LABELS.get(key, key.replace("_", " ").title())
        color  = STMT_COLORS.get(key, "#374151")
        status = node.get("status", "")

        imgs   = find_capture_images(captures_base, company, key)
        img_html = ""
        for ip in imgs:
            src = _try_relative(view_html, ip)
            img_html += (f'<img src="{html.escape(src)}" alt="{esc(ip.name)}"/>'
                         f'<div class="pageno">{esc(ip.name)}</div>')
        if not img_html:
            img_html = '<p class="muted">No captured page images found.</p>'

        try:
            rendered = render_statement_body(key, node)
        except Exception as e:
            rendered = (f'<p class="err">Renderer crashed: {esc(e)}</p>'
                        + render_json_pre(node))

        body += f"""
        <section class="stmt" id="{key}">
          <div class="hd" style="background:{color};">
            <span>{esc(title)}</span>
            <span class="badge">{esc(status or '-')}</span>
          </div>
          <div class="split">
            <div>
              <h3>Original captured page(s)</h3>
              {img_html}
            </div>
            <div>
              <h3>Extracted table</h3>
              <a class="toplink" href="#top">^ back to top</a>
              {rendered}
            </div>
          </div>
        </section>"""

    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'/>
<title>Extracted Tables - {esc(company)}</title>
<style>{CSS}</style></head><body>
<header id='top'>
  <h1>{esc(company)} - Extracted Financial Tables</h1>
  <div class='meta'>Generated: {datetime.now().isoformat(timespec='seconds')}
    &nbsp;|&nbsp; <a href='../index.html' style='color:var(--accent);'>back to index</a></div>
</header>
<main>
  <div class='nav'>{nav_links}</div>
  {body}
</main>
</body></html>"""


def build_index(extracted_dir: Path, companies: list[tuple[str, Path, dict]]) -> str:
    rows = ""
    for company, view_path, meta in companies:
        rel = _try_relative(extracted_dir / "index.html", view_path)
        stmts = meta.get("statements", [])
        gen   = meta.get("generated_at", "")
        model = meta.get("model", "")
        rows += (
            f"<tr><td><a href='{html.escape(rel)}'>{esc(company)}</a></td>"
            f"<td class='num'>{len(stmts)}</td>"
            f"<td class='lbl'>{esc(', '.join(stmts))}</td>"
            f"<td class='ref'>{esc(model)}</td>"
            f"<td class='ref'>{esc(gen)}</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='5' class='muted'>No companies found.</td></tr>"

    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'/>
<title>Extracted JSON - Index</title>
<style>{CSS}</style></head><body>
<header><h1>Extracted JSON - Index</h1>
<div class='meta'>Folder: {esc(extracted_dir)}
&nbsp;|&nbsp; {len(companies)} compan{'y' if len(companies)==1 else 'ies'}
&nbsp;|&nbsp; Generated {datetime.now().isoformat(timespec='seconds')}</div>
</header><main>
<table class='fin'>
<thead><tr><th class='lbl'>Company</th><th>#stmts</th>
  <th class='lbl'>Statements</th><th class='lbl'>Model</th>
  <th class='lbl'>Generated</th></tr></thead>
<tbody>{rows}</tbody></table>
</main></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def discover_companies(extracted_dir: Path) -> list[tuple[str, Path]]:
    """Find every <company>/<company>_results.json under extracted_dir."""
    if not extracted_dir.exists():
        return []
    found = []
    for child in sorted(extracted_dir.iterdir()):
        if not child.is_dir():
            continue
        candidates = list(child.glob("*_results.json"))
        if not candidates:
            continue
        found.append((child.name, candidates[0]))
    return found


def run(extracted_dir: Path, captures_base: Path, only: str | None) -> Path:
    extracted_dir.mkdir(parents=True, exist_ok=True)

    companies_found = discover_companies(extracted_dir)
    if only:
        companies_found = [c for c in companies_found if c[0] == only]
        if not companies_found:
            print(f"[error] No results found for company '{only}' in {extracted_dir}")
            sys.exit(2)

    print(f"\n{'='*60}")
    print(f"  STEP 4 - Build viewer HTML")
    print(f"  Extracted dir : {extracted_dir}")
    print(f"  Captures base : {captures_base}")
    print(f"  Companies     : {len(companies_found)}")
    print(f"{'='*60}\n")

    index_rows: list[tuple[str, Path, dict]] = []

    for company, results_path in companies_found:
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [warn] {company} - cannot read results JSON ({e})")
            continue

        view_path = results_path.parent / f"{company}_view.html"
        html_str  = build_company_view(company, results, view_path, captures_base)
        view_path.write_text(html_str, encoding="utf-8")

        meta_path = results_path.parent / "extraction_meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        meta.setdefault("statements", sorted(results.keys()))

        index_rows.append((company, view_path, meta))
        print(f"  [ok]   {company:<14}-> {view_path.name}")

    index_path = extracted_dir / "index.html"
    index_path.write_text(build_index(extracted_dir, index_rows), encoding="utf-8")
    print(f"\n  [save] Index page -> {index_path}")
    return index_path


def serve(serve_root: Path, port: int, open_path: str) -> None:
    serve_root = serve_root.resolve()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(serve_root), **kw
    )
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/{open_path}"
        print(f"\n  Serving {serve_root} on  http://127.0.0.1:{port}/")
        print(f"  Opening {url}\n  (Ctrl+C to stop)\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="STEP 4 - View / compare extracted JSON tables in a browser."
    )
    ap.add_argument("--extracted", type=Path, default=DEFAULT_EXTRACTED_DIR,
                    help=f"Folder with company subfolders  "
                         f"(default: {DEFAULT_EXTRACTED_DIR})")
    ap.add_argument("--captures",  type=Path, default=DEFAULT_CAPTURES_BASE,
                    help=f"Folder where <company>_captures live  "
                         f"(default: {DEFAULT_CAPTURES_BASE})")
    ap.add_argument("--only",      default=None,
                    help="Only rebuild this single company (e.g. company1)")
    ap.add_argument("--serve",     action="store_true",
                    help="Start a local web server after building and open the index")
    ap.add_argument("--port",      type=int, default=8000,
                    help="Port for --serve (default: 8000)")
    ap.add_argument("--serve-root", type=Path, default=DEFAULT_SERVE_ROOT,
                    help=f"Web server root  (default: {DEFAULT_SERVE_ROOT}). "
                         f"Must be a parent of BOTH Extracted_json and json_logs "
                         f"so images load.")
    args = ap.parse_args()

    index_path = run(args.extracted, args.captures, args.only)

    if args.serve:
        try:
            rel_index = index_path.resolve().relative_to(args.serve_root.resolve())
            url_path  = str(rel_index).replace("\\", "/")
        except Exception:
            url_path = "Extracted_json/index.html"
        serve(args.serve_root, args.port, url_path)
    else:
        print(f"\n  Open in browser:  {index_path}")
        print(f"  Or run with --serve for a local web server.\n")
