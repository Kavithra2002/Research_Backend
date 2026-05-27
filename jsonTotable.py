"""
view_sofp.py
============
View an extracted SoFP JSON file as a clean HTML table in your browser.

Reads the JSON produced by sofp_extractor.py and renders:
  - Metadata header (company, source PDF, page, extraction date)
  - A financial table with:
      • Description column
      • Note column  (shown when has_note_col = true)
      • One column per amount (with comma-formatted numbers)
  - Section headers rendered as full-width shaded rows
  - Totals rows rendered in bold

Usage:
    python view_sofp.py path/to/company_SoFP.json
    python view_sofp.py path/to/company_SoFP.json --no-browser
"""

from __future__ import annotations

import argparse
import html
import json
import re
import tempfile
import webbrowser
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _esc(val: Any) -> str:
    if val is None:
        return ""
    return html.escape(str(val), quote=True)


def _fmt_amount(val: Any) -> str:
    """Format a number with comma thousands separator, or return raw string."""
    if val is None or val == "":
        return ""
    if isinstance(val, bool):
        return _esc(val)
    # Already a number
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if isinstance(val, float) and val == int(val) and abs(val) < 1e16:
            val = int(val)
        if isinstance(val, int):
            return f"{val:,}" if val >= 0 else f"({abs(val):,})"
        return f"{val:,.4f}".rstrip("0").rstrip(".")
    # String: try to reformat
    s = str(val).strip()
    if not s or s in ("-", "–", "—"):
        return s if s else "-"
    neg = s.startswith("(") and s.endswith(")")
    clean = re.sub(r"[^\d.]", "", s)
    if not clean:
        return _esc(s)
    try:
        if "." in clean:
            n: int | float = float(clean)
            if n == int(n) and abs(n) < 1e16:
                n = int(n)
        else:
            n = int(clean)
        if isinstance(n, int):
            return f"({abs(n):,})" if neg else f"{n:,}"
        return f"({abs(n):,.4f})".rstrip("0").rstrip(".") if neg else f"{n:,.4f}".rstrip("0").rstrip(".")
    except (ValueError, OverflowError):
        return _esc(s)


def _is_total_row(label: str) -> bool:
    """True for subtotal / total rows that should be bolded."""
    lab = (label or "").strip().lower()
    return lab.startswith("total") or lab.startswith("net asset")


# ─────────────────────────────────────────────────────────────────────────────
# HTML builder
# ─────────────────────────────────────────────────────────────────────────────

def build_html(data: dict) -> str:
    company       = data.get("company", "")
    source_pdf    = data.get("source_pdf", "")
    extracted_at  = data.get("extracted_at", "")
    page_1based   = data.get("statement_pdf_page_1based", "")
    has_note      = bool(data.get("has_note_col", False))
    col_headers   = data.get("column_headers") or []
    line_items    = data.get("line_items") or []

    # ── Metadata block ───────────────────────────────────────────────────────
    meta_rows = [
        ("Company",       company),
        ("Source PDF",    Path(source_pdf).name if source_pdf else ""),
        ("Statement page", str(page_1based)),
        ("Extracted at",  extracted_at),
    ]
    meta_html = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
        for k, v in meta_rows if v
    )

    # ── Table header ─────────────────────────────────────────────────────────
    # Columns: Description | [Note] | amt1 | amt2 | ...
    th_desc = '<th class="th-desc">Description</th>'
    th_note = '<th class="th-note">Note</th>' if has_note else ""
    th_amts = "".join(
        f'<th class="th-amt">{_esc(h)}</th>' for h in col_headers
    )
    thead = f"<thead><tr>{th_desc}{th_note}{th_amts}</tr></thead>"

    # ── Table body ────────────────────────────────────────────────────────────
    tbody_rows: list[str] = []
    n_amt = len(col_headers)

    for item in line_items:
        label     = item.get("label", "")
        is_sec    = item.get("is_section_header", False)
        note      = item.get("note") or ""
        amounts   = item.get("amounts") or []
        amts_raw  = item.get("amounts_raw") or []

        if is_sec:
            # Section header: spans all columns, shaded background
            n_total_cols = 1 + (1 if has_note else 0) + n_amt
            tbody_rows.append(
                f'<tr class="row-section">'
                f'<td colspan="{n_total_cols}" class="td-section">{_esc(label)}</td>'
                f'</tr>'
            )
            continue

        is_total = _is_total_row(label)
        row_cls  = "row-total" if is_total else "row-data"

        td_desc = f'<td class="td-desc">{_esc(label)}</td>'
        td_note = f'<td class="td-note">{_esc(note)}</td>' if has_note else ""

        td_amts = ""
        for k in range(n_amt):
            # Use parsed amounts when available, fall back to raw strings
            raw = amts_raw[k] if k < len(amts_raw) else ""
            val = amounts[k] if k < len(amounts) else None
            display = _fmt_amount(val if val is not None else raw)
            if not display and raw in ("-", "–", "—"):
                display = "-"
            td_amts += f'<td class="td-amt">{display}</td>'

        tbody_rows.append(f'<tr class="{row_cls}">{td_desc}{td_note}{td_amts}</tr>')

    tbody = "<tbody>" + "".join(tbody_rows) + "</tbody>"

    # ── Assemble ──────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>SoFP – {_esc(company)}</title>
  <style>
    :root {{
      --border:    #d0d0d0;
      --head-bg:   #e6eaf0;
      --sec-bg:    #f0f2f5;
      --stripe:    #fafbfc;
      --total-bg:  #eef2f7;
      --text:      #1a1a1a;
      --note-col:  #5a6a7a;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
      font-size: 13px;
      color: var(--text);
      background: #f3f4f6;
      padding: 1.5rem 2rem 3rem;
    }}
    h1 {{
      font-size: 1.15rem;
      font-weight: 600;
      margin-bottom: 1rem;
      color: #2c3e50;
    }}

    /* ── Metadata ── */
    table.meta {{
      border-collapse: collapse;
      margin-bottom: 1.5rem;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 4px;
      overflow: hidden;
    }}
    table.meta th, table.meta td {{
      padding: 0.35rem 0.8rem;
      border-bottom: 1px solid var(--border);
      text-align: left;
      font-size: 0.8rem;
    }}
    table.meta th {{
      background: var(--head-bg);
      font-weight: 600;
      color: #444;
      width: 140px;
    }}

    /* ── Main financial table ── */
    .scroll {{ overflow-x: auto; }}
    table.report {{
      border-collapse: collapse;
      width: 100%;
      min-width: 480px;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 4px;
      font-variant-numeric: tabular-nums;
    }}

    /* Header */
    table.report thead tr th {{
      background: var(--head-bg);
      border: 1px solid var(--border);
      padding: 0.5rem 0.7rem;
      font-weight: 600;
      white-space: nowrap;
    }}
    th.th-desc {{ text-align: left; min-width: 240px; }}
    th.th-note {{ text-align: center; width: 52px; color: var(--note-col); }}
    th.th-amt  {{ text-align: right; min-width: 110px; }}

    /* Body cells */
    table.report tbody td {{
      border: 1px solid var(--border);
      padding: 0.38rem 0.7rem;
      vertical-align: middle;
      line-height: 1.4;
    }}
    td.td-desc {{ text-align: left; }}
    td.td-note {{
      text-align: center;
      color: var(--note-col);
      font-size: 0.78rem;
      white-space: nowrap;
      width: 52px;
    }}
    td.td-amt {{
      text-align: right;
      white-space: nowrap;
      font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
      font-size: 0.8rem;
    }}

    /* Section header rows */
    tr.row-section td.td-section {{
      background: var(--sec-bg);
      font-weight: 700;
      font-size: 0.78rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: #3a4a5a;
      padding: 0.45rem 0.7rem;
      border-top: 2px solid #b0bac8;
    }}

    /* Total rows */
    tr.row-total td {{
      background: var(--total-bg);
      font-weight: 700;
      border-top: 1.5px solid #aab4c0;
    }}

    /* Alternating stripe on data rows */
    tr.row-data:nth-child(even) td {{ background: var(--stripe); }}
    tr.row-data:hover td {{ background: #e8edf5; }}
  </style>
</head>
<body>
  <h1>Statement of Financial Position — {_esc(company)}</h1>

  <table class="meta">
    {meta_html}
  </table>

  <div class="scroll">
    <table class="report">
      {thead}
      {tbody}
    </table>
  </div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="View a SoFP JSON file as an HTML table in your browser."
    )
    ap.add_argument("json_path", type=Path,
                    help="Path to the JSON file (e.g. json_logs/ACME_SoFP.json)")
    ap.add_argument("--no-browser", action="store_true",
                    help="Write HTML to a temp file but do not open the browser")
    args = ap.parse_args()

    path = args.json_path.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise SystemExit("JSON root must be an object / dict")

    html_out = build_html(data)

    tmp = Path(tempfile.gettempdir()) / "sofp_viewer.html"
    tmp.write_text(html_out, encoding="utf-8")
    print(f"Wrote: {tmp}")

    if not args.no_browser:
        webbrowser.open(tmp.as_uri())
        print("Opened in your default browser.")


if __name__ == "__main__":
    main()
