"""
Q_testing.py
============
Side-by-side HTML viewer for the artefacts produced by Q_data_extraction.py.

For every company folder under <extracted_dir>/<slug>/ it builds:
  · <slug>/<slug>_view.html   — one page per statement:
        LEFT  : rasterised PDF page image (from captures/<stmt_key>/page_NNN.png)
        RIGHT : OpenAI verbatim table (step4 renderer) or local fallback
  · index.html                — master index listing all companies
  · collapsible raw JSON panel at the bottom of each company page

Uses the same table renderer as testing.py / step4_view_extracted_table.py
for OpenAI-extracted data.  Older rule-based JSON (legacy shape) still
renders via the built-in local table renderer.

Usage
-----
    # Build HTML for all companies found under ./extracted/
    python Q_testing.py

    # Specify a different extraction root
    python Q_testing.py --extracted ./my_output

    # Only rebuild one company (use its slug folder name)
    python Q_testing.py --only acl_plastics_plc

    # Build + start a local web server and open the index
    python Q_testing.py --serve

    # Change port (default 8001)
    python Q_testing.py --serve --port 9000
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
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

# Reuse step4's high-quality verbatim-table renderer so the OpenAI
# extracted data displays identically to Data_retrive.py / testing.py.
try:
    import step4_view_extracted_table as _step4
    _STEP4_AVAILABLE = True
except Exception:
    _step4 = None  # type: ignore
    _STEP4_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 – STATEMENT DISPLAY METADATA
# ═══════════════════════════════════════════════════════════════════════════

# Preferred display order for statements in the viewer
STMT_ORDER: list[str] = [
    "consolidated_income_statement",
    "company_income_statement",
    "income_statement",
    "profit_loss_comprehensive",
    "consolidated_comprehensive_income",
    "company_comprehensive_income",
    "comprehensive_income",
    "financial_position",
    "changes_in_equity",
    "cash_flow_statement",
]

STMT_LABELS: dict[str, str] = {
    "consolidated_income_statement"   : "Consolidated Income Statement",
    "company_income_statement"        : "Company Income Statement",
    "income_statement"                : "Income Statement",
    "profit_loss_comprehensive"       : "Profit or Loss & Comprehensive Income",
    "consolidated_comprehensive_income": "Consolidated Comprehensive Income",
    "company_comprehensive_income"    : "Company Comprehensive Income",
    "comprehensive_income"            : "Comprehensive Income",
    "financial_position"              : "Statement of Financial Position",
    "changes_in_equity"               : "Changes in Equity",
    "cash_flow_statement"             : "Cash Flow Statement",
}

STMT_COLORS: dict[str, str] = {
    "consolidated_income_statement"   : "#1e3a5f",
    "company_income_statement"        : "#1e3a5f",
    "income_statement"                : "#1e3a5f",
    "profit_loss_comprehensive"       : "#1e3a5f",
    "consolidated_comprehensive_income": "#1a4731",
    "company_comprehensive_income"    : "#1a4731",
    "comprehensive_income"            : "#1a4731",
    "financial_position"              : "#3d1a5f",
    "changes_in_equity"               : "#5f3d1a",
    "cash_flow_statement"             : "#1a3d5f",
}
_DEFAULT_COLOR = "#374151"


def _stmt_label(key: str, record: dict) -> str:
    if key in STMT_LABELS:
        return STMT_LABELS[key]
    # Use the title stored in the record if available
    return record.get("title") or key.replace("_", " ").title()


def _stmt_color(key: str) -> str:
    # Strip numeric suffix (_2, _3 …) for colour lookup
    base = key.rstrip("_0123456789").rstrip("_")
    return STMT_COLORS.get(base, _DEFAULT_COLOR)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 – CSS
# ═══════════════════════════════════════════════════════════════════════════

CSS = """
/* ── Reset & base ───────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:15px}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0f1117;color:#e2e8f0;line-height:1.55;
}

/* ── Layout ─────────────────────────────────────────────────────── */
header{
  padding:1.2rem 2rem 1rem;
  border-bottom:1px solid #1f2535;
  background:#161b27;
}
header h1{font-size:1.35rem;font-weight:700;color:#f1f5f9;margin-bottom:.3rem}
header .meta{font-size:0.78rem;color:#94a3b8}
header .meta a{color:#60a5fa;text-decoration:none}
header .meta a:hover{text-decoration:underline}

main{padding:1.5rem 2rem 3rem;max-width:1700px;margin:0 auto}

/* ── Pill badges ─────────────────────────────────────────────────── */
.pills{display:flex;gap:.4rem;flex-wrap:wrap;margin:.5rem 0 1.2rem}
.pill{
  border-radius:999px;padding:.18rem .7rem;
  font-size:.7rem;font-weight:600;letter-spacing:.02em;
  border:1px solid #1f2535;color:#cbd5e1;background:#161b27;
}
.pill.green{border-color:#065f46;color:#34d399}
.pill.blue {border-color:#1e3a5f;color:#60a5fa}

/* ── Statement section ───────────────────────────────────────────── */
.stmt{border:1px solid #1f2535;border-radius:8px;margin-bottom:2rem;overflow:hidden}
.stmt-hd{
  display:flex;justify-content:space-between;align-items:center;
  padding:.55rem 1.1rem;cursor:pointer;user-select:none;
}
.stmt-hd span{font-size:.85rem;font-weight:700;color:#f1f5f9;letter-spacing:.03em}
.stmt-hd .badge{
  font-size:.65rem;padding:.15rem .55rem;border-radius:999px;
  background:rgba(255,255,255,.12);color:#e2e8f0;font-weight:600;
}

/* ── Split pane ──────────────────────────────────────────────────── */
.split{display:grid;grid-template-columns:1fr 1fr;gap:0}
.split > .pane{padding:1rem 1.2rem;overflow-y:auto;max-height:900px}
.split > .pane:first-child{
  border-right:1px solid #1f2535;background:#0c1020;
}
.split > .pane:last-child{background:#10151f}
.pane h3{
  font-size:.75rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:#64748b;margin-bottom:.7rem;
}

/* ── PDF page images ─────────────────────────────────────────────── */
.pdf-images img{
  width:100%;border-radius:4px;margin-bottom:.6rem;
  border:1px solid #1f2535;display:block;
}
.pageno{font-size:.65rem;color:#475569;margin-top:-.4rem;margin-bottom:.8rem}
.no-images{color:#475569;font-style:italic;font-size:.85rem;padding:.5rem 0}

/* ── Extracted table ─────────────────────────────────────────────── */
.tbl-wrap{overflow-x:auto}
table.fin{
  width:100%;border-collapse:collapse;font-size:.78rem;
  min-width:360px;
}
table.fin th{
  background:#161b27;color:#94a3b8;
  font-weight:700;font-size:.68rem;text-transform:uppercase;
  letter-spacing:.05em;padding:.45rem .7rem;
  border-bottom:2px solid #1f2535;text-align:left;white-space:nowrap;
}
table.fin th.hdr-multi{
  text-transform:none;font-size:.72rem;line-height:1.35;
  white-space:normal;vertical-align:bottom;
}
table.fin td{
  padding:.38rem .7rem;border-bottom:1px solid #1a2035;
  color:#cbd5e1;vertical-align:top;
}
table.fin td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.fin td.lbl{color:#e2e8f0;max-width:280px}
table.fin tr:last-child td{border-bottom:none}
table.fin tr.subtotal td{color:#f1f5f9;font-weight:600}
table.fin tr.section-hd td{
  color:#60a5fa;font-weight:700;font-size:.72rem;
  text-transform:uppercase;letter-spacing:.05em;
  padding-top:.65rem;border-top:1px solid #1f2535;
}
table.fin tr:hover td{background:rgba(255,255,255,.025)}

/* ── Raw fallback ────────────────────────────────────────────────── */
pre.raw{
  font-size:.7rem;color:#94a3b8;white-space:pre-wrap;word-break:break-all;
  background:#0c1020;padding:.8rem;border-radius:4px;max-height:500px;
  overflow-y:auto;border:1px solid #1f2535;
}

/* ── Raw JSON dump ───────────────────────────────────────────────── */
.json-block{
  border:1px solid #1f2535;border-radius:8px;margin-bottom:2rem;
  background:#0c1020;overflow:hidden;
}
.json-block summary{
  cursor:pointer;user-select:none;list-style:none;
  padding:.6rem 1.1rem;background:#1a2238;color:#f1f5f9;
  font-size:.85rem;font-weight:700;letter-spacing:.03em;
  display:flex;justify-content:space-between;align-items:center;
}
.json-block summary::after{
  content:"▼";font-size:.65rem;color:#94a3b8;transition:transform .15s;
}
.json-block[open] summary::after{transform:rotate(180deg)}
.json-block summary:hover{background:#243049}
.json-block pre.json{
  margin:0;padding:1rem 1.2rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.72rem;line-height:1.5;color:#cbd5e1;
  white-space:pre;overflow:auto;max-height:600px;
  background:#0c1020;border-top:1px solid #1f2535;
}
.json-block pre.json .k{color:#7dd3fc}
.json-block pre.json .s{color:#86efac}
.json-block pre.json .n{color:#fcd34d}
.json-block pre.json .b{color:#f472b6}

/* ── Nav bar ─────────────────────────────────────────────────────── */
.nav{
  display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.4rem;
  padding:.7rem 1rem;background:#161b27;border-radius:6px;
  border:1px solid #1f2535;
}
.nav a{
  font-size:.75rem;font-weight:600;color:#94a3b8;
  text-decoration:none;padding:.2rem .6rem;border-radius:4px;
  border:1px solid transparent;transition:all .15s;
}
.nav a:hover{color:#f1f5f9;border-color:#334155;background:#1e293b}

/* ── Index table ─────────────────────────────────────────────────── */
table.idx{
  width:100%;border-collapse:collapse;font-size:.82rem;margin-top:1rem;
}
table.idx th{
  background:#161b27;color:#64748b;font-size:.7rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.05em;
  padding:.5rem .9rem;border-bottom:2px solid #1f2535;text-align:left;
}
table.idx td{
  padding:.5rem .9rem;border-bottom:1px solid #1a2035;color:#cbd5e1;
}
table.idx td a{color:#60a5fa;text-decoration:none}
table.idx td a:hover{text-decoration:underline}
table.idx tr:hover td{background:rgba(255,255,255,.025)}
table.idx td.num{text-align:right;color:#94a3b8}

/* ── Misc ────────────────────────────────────────────────────────── */
.toplink{
  display:block;text-align:right;font-size:.7rem;color:#475569;
  text-decoration:none;margin-bottom:.5rem;
}
.toplink:hover{color:#94a3b8}
.muted{color:#475569;font-style:italic;font-size:.82rem}
.err{color:#f87171;font-size:.8rem}
@media(max-width:900px){
  .split{grid-template-columns:1fr}
  .split > .pane:first-child{border-right:none;border-bottom:1px solid #1f2535}
}
"""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 – HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def esc(s: object) -> str:
    """HTML-escape a value."""
    import html
    return html.escape(str(s) if s is not None else "")


def _rel(from_file: Path, to_file: Path) -> str:
    """Relative path from one file to another (for HTML href / src)."""
    try:
        return os.path.relpath(to_file, from_file.parent).replace("\\", "/")
    except ValueError:
        return str(to_file).replace("\\", "/")


def _is_numeric_cell(v: str) -> bool:
    import re
    return bool(re.match(r"^\s*[\(\-]?\s*[\d,\.]+\s*[\)]?\s*%?\s*$",
                         v.replace(" ", "")))


def render_json_block(result: dict) -> str:
    """
    Pretty-print the full extraction JSON inside a collapsible <details>
    block with light syntax colouring (keys/strings/numbers/booleans).
    """
    import re
    pretty = json.dumps(result, indent=2, ensure_ascii=False)
    safe   = esc(pretty)

    safe = re.sub(r'(&quot;[^&\n]*?&quot;)(\s*:)',
                  r'<span class="k">\1</span>\2', safe)
    safe = re.sub(r':\s*(&quot;.*?&quot;)',
                  lambda m: m.group(0).replace(
                      m.group(1), f'<span class="s">{m.group(1)}</span>'),
                  safe)
    safe = re.sub(r':\s*(-?\d+(?:\.\d+)?)\b',
                  lambda m: m.group(0).replace(
                      m.group(1), f'<span class="n">{m.group(1)}</span>'),
                  safe)
    safe = re.sub(r':\s*(true|false|null)\b',
                  lambda m: m.group(0).replace(
                      m.group(1), f'<span class="b">{m.group(1)}</span>'),
                  safe)

    n_stmts = len(result.get("statements", {}) or {})
    size_kb = len(pretty.encode("utf-8")) / 1024
    return (
        '<details class="json-block">'
        f'<summary>Extracted JSON  '
        f'<span style="font-weight:400;font-size:.7rem;color:#94a3b8">'
        f'{n_stmts} statement(s) · {size_kb:.1f} KB · click to expand</span>'
        '</summary>'
        f'<pre class="json">{safe}</pre>'
        '</details>'
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 – TABLE RENDERER
# ═══════════════════════════════════════════════════════════════════════════

def render_step3_table(api_record: dict) -> str:
    """
    Render the step3 verbatim shape produced by OpenAI:
      api_record = {"status": "ok", "title": "...", "data": {...}}
    Reuses step4_view_extracted_table.render_raw_tables when available
    so the output looks identical to the testing.py viewer.
    """
    status = api_record.get("status", "")
    data   = api_record.get("data")

    if status == "ok" and isinstance(data, dict) and _STEP4_AVAILABLE:
        try:
            return _step4.render_raw_tables(data)
        except Exception as ex:
            return (
                f"<p class='err'>step4 renderer crashed: {esc(ex)}</p>"
                f"<pre class='raw'>{esc(json.dumps(data, indent=2)[:4000])}</pre>"
            )

    if status == "ok" and isinstance(data, dict):
        # step4 unavailable — render the verbatim JSON in a code block
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        return f"<pre class='raw'>{esc(pretty[:8000])}</pre>"

    if status == "dry_run":
        return (
            "<p class='muted'>Dry-run — images captured but not sent "
            "to OpenAI. Re-run with <code>--apikey</code> (or set "
            "<code>OPENAI_API_KEY</code>) to populate this table.</p>"
        )

    if status == "no_api_key":
        return (
            "<p class='muted'>No OpenAI API key configured. "
            "Re-run extraction with <code>--apikey sk-…</code> or set "
            "<code>OPENAI_API_KEY</code> in <code>backend/.env</code>.</p>"
        )

    if status == "parse_failed":
        raw = api_record.get("raw_response", "")
        return (
            "<p class='err'>OpenAI returned a non-JSON response.</p>"
            f"<pre class='raw'>{esc(raw[:4000])}</pre>"
        )

    if status == "api_error":
        return f"<p class='err'>OpenAI API error: {esc(api_record.get('error', ''))}</p>"

    if status == "no_images":
        return "<p class='muted'>No captured page images for this statement.</p>"

    return (
        f"<p class='muted'>(status: <code>{esc(status)}</code>)</p>"
        f"<pre class='raw'>{esc(json.dumps(api_record, indent=2)[:4000])}</pre>"
    )


def render_local_table(record: dict) -> str:
    """Render the LOCAL rule-based extractor output (legacy shape)."""
    columns  = record.get("columns") or []
    rows     = record.get("rows")    or []
    raw_rows = record.get("raw_rows") or []

    # ── Fallback: render raw_rows when structured extraction failed ────
    if not rows and raw_rows:
        lines = "\n".join(
            " | ".join(str(c) for c in row) for row in raw_rows
        )
        return f"<pre class='raw'>{esc(lines)}</pre>"

    if not rows:
        return "<p class='muted'>No data rows extracted for this statement.</p>"

    # ── Build column list from data when columns metadata is thin ─────
    if not columns or len(columns) < 2:
        # Infer columns from the keys of the first row's values dict
        if rows:
            columns = ["Line Item"] + list(rows[0]["values"].keys())

    # ── Header (multi-line when column name contains " / ") ─────────────
    header_cells = ""
    for c in columns:
        if " / " in c:
            parts = [esc(p.strip()) for p in c.split(" / ") if p.strip()]
            inner = "<br>".join(parts)
            header_cells += f'<th class="hdr-multi">{inner}</th>'
        else:
            header_cells += f"<th>{esc(c)}</th>"
    thead = f"<thead><tr>{header_cells}</tr></thead>"

    # ── Body ──────────────────────────────────────────────────────────
    tbody_rows = []
    val_keys = list(rows[0]["values"].keys()) if rows else []

    for row in rows:
        label  = row.get("label", "")
        values = row.get("values", {})

        # Classify row style
        css_class = ""
        lbl_lower = label.lower()
        if not label.strip() and not any(v.strip() for v in values.values()):
            continue   # skip blank rows
        if any(kw in lbl_lower for kw in
               ("total", "profit", "gross profit", "net", "balance")):
            css_class = "subtotal"
        elif not any(v.strip() for v in values.values()):
            css_class = "section-hd"

        # Label cell
        label_td = f"<td class='lbl'>{esc(label)}</td>"

        # Value cells
        val_tds = ""
        for k in val_keys:
            v = values.get(k, "")
            num_cls = " num" if _is_numeric_cell(v) else ""
            val_tds += f"<td class='val{num_cls}'>{esc(v)}</td>"

        tbody_rows.append(
            f"<tr class='{css_class}'>{label_td}{val_tds}</tr>"
        )

    tbody = "<tbody>" + "\n".join(tbody_rows) + "</tbody>"
    return f"<div class='tbl-wrap'><table class='fin'>{thead}{tbody}</table></div>"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 – CAPTURE IMAGE FINDER
# ═══════════════════════════════════════════════════════════════════════════

def find_captures(company_dir: Path, stmt_key: str) -> list[Path]:
    """
    Return sorted list of page PNG images for a statement.
    Searches:  captures/<stmt_key>/page_*.png
               <stmt_key>/page_*.png          (legacy layout)
    """
    candidates = [
        company_dir / "captures" / stmt_key,
        company_dir / stmt_key,
    ]
    for folder in candidates:
        if folder.is_dir():
            imgs = sorted(folder.glob("page_*.png"))
            if imgs:
                return imgs
    return []


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 – COMPANY VIEW HTML
# ═══════════════════════════════════════════════════════════════════════════

def build_company_view(slug: str, company_dir: Path,
                       result: dict, view_path: Path) -> str:
    """
    Build the full side-by-side HTML page for one company.

    Supports both result shapes:
      • NEW: {"statements": {key: {"status": "ok", "data": {...}}},
              "local_statements": {key: <legacy record>}, ...}
      • OLD: {"statements": {key: <legacy record>}, ...}
    """
    company   = esc(result.get("company", slug))
    period    = esc(result.get("period", ""))
    generated = esc(result.get("generated_at", ""))
    source    = esc(result.get("source_pdf", ""))
    api_status = esc(result.get("api_status", ""))
    model      = esc(result.get("model") or "")

    api_statements   = result.get("statements", {}) or {}
    local_statements = result.get("local_statements", {}) or {}

    # Detect which shape we have. If the first value of `statements` looks
    # like the LEGACY local-extractor shape (has "rows" or "raw_rows"),
    # treat the whole dict as local-only.
    is_legacy_only = bool(api_statements) and all(
        isinstance(v, dict) and ("rows" in v or "raw_rows" in v)
        and "status" not in v
        for v in api_statements.values()
    )
    if is_legacy_only:
        local_statements = api_statements
        api_statements   = {}

    # Display every key that appears in either map
    all_keys = list(dict.fromkeys(
        list(api_statements.keys()) + list(local_statements.keys())
    ))
    ordered_keys = (
        [k for k in STMT_ORDER if k in all_keys]
        + [k for k in all_keys if k not in STMT_ORDER]
    )

    def _label(k: str) -> str:
        return _stmt_label(
            k,
            api_statements.get(k) or local_statements.get(k) or {},
        )

    # ── Nav links ─────────────────────────────────────────────────────
    nav_links = "".join(
        f"<a href='#{esc(k)}'>{esc(_label(k))}</a>"
        for k in ordered_keys
    )

    # ── Pill summary ──────────────────────────────────────────────────
    n_stmts = len(ordered_keys)
    n_imgs  = sum(len(find_captures(company_dir, k)) for k in ordered_keys)
    n_api_ok = sum(
        1 for v in api_statements.values()
        if isinstance(v, dict) and v.get("status") == "ok"
    )
    pills   = (
        f"<span class='pill green'>{n_stmts} statement(s)</span>"
        f"<span class='pill blue'>{n_imgs} captured page(s)</span>"
    )
    if api_statements:
        pills += (
            f"<span class='pill blue'>OpenAI ok: {n_api_ok}/"
            f"{len(api_statements)}</span>"
        )
    if model:
        pills += f"<span class='pill'>{model}</span>"
    if api_status:
        pills += f"<span class='pill'>{api_status}</span>"

    # ── Statement sections ────────────────────────────────────────────
    body_parts: list[str] = []
    for key in ordered_keys:
        api_rec   = api_statements.get(key)
        local_rec = local_statements.get(key) or {}
        title  = esc(_label(key))
        color  = _stmt_color(key)
        pages  = (local_rec or {}).get("pages", [])
        page_label = (
            f"page {pages[0]}" if len(pages) == 1
            else f"pages {', '.join(str(p) for p in pages)}"
            if pages else "—"
        )

        # Images
        imgs = find_captures(company_dir, key)
        if imgs:
            img_tags = "".join(
                f"<img src='{esc(_rel(view_path, img))}' "
                f"alt='{esc(img.name)}' loading='lazy'/>"
                f"<div class='pageno'>{esc(img.name)}</div>"
                for img in imgs
            )
            left_html = f"<div class='pdf-images'>{img_tags}</div>"
        else:
            left_html = "<p class='no-images'>No captured page images found.</p>"

        # Right pane:  OpenAI verbatim table (preferred) or local fallback
        try:
            if api_rec:
                right_html = render_step3_table(api_rec)
                source_tag = api_rec.get("status", "openai")
            elif local_rec:
                right_html = render_local_table(local_rec)
                source_tag = "local"
            else:
                right_html = "<p class='muted'>No extraction available.</p>"
                source_tag = "—"
        except Exception as ex:
            right_html = f"<p class='err'>Renderer error: {esc(ex)}</p>"
            import traceback
            right_html += f"<pre class='raw'>{esc(traceback.format_exc())}</pre>"
            source_tag = "error"

        # If we have BOTH an API result AND a local rule-based record,
        # show the local one underneath in a collapsed details block so
        # the user can compare.
        if api_rec and local_rec and (local_rec.get("rows") or local_rec.get("raw_rows")):
            try:
                local_html = render_local_table(local_rec)
            except Exception as ex:
                local_html = f"<p class='err'>local renderer error: {esc(ex)}</p>"
            right_html += (
                "<details class='local-fallback' style='margin-top:1rem'>"
                "<summary style='cursor:pointer;font-size:.75rem;color:#94a3b8'>"
                "Show local rule-based extraction (for comparison)"
                "</summary>"
                f"<div style='margin-top:.5rem'>{local_html}</div>"
                "</details>"
            )

        body_parts.append(f"""
<section class="stmt" id="{esc(key)}">
  <div class="stmt-hd" style="background:{color}">
    <span>{title}</span>
    <span class="badge">{esc(page_label)} · {esc(source_tag)}</span>
  </div>
  <div class="split">
    <div class="pane">
      <h3>Original PDF page(s) — {len(imgs)} image(s)</h3>
      {left_html}
    </div>
    <div class="pane">
      <a class="toplink" href="#top">↑ back to top</a>
      <h3>Extracted table</h3>
      {right_html}
    </div>
  </div>
</section>""")

    if not body_parts:
        body_html = "<p class='muted'>No statements extracted for this company.</p>"
    else:
        body_html = "\n".join(body_parts)

    json_html = render_json_block(result)

    back_link = _rel(view_path, view_path.parent.parent / "index.html")

    extra_css = _step4.CSS if _STEP4_AVAILABLE else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{company} — Financial Statement Viewer</title>
  <style>{CSS}</style>
  <style>{extra_css}</style>
</head>
<body>
<header id="top">
  <h1>{company}</h1>
  <div class="meta">
    Period: <strong>{period}</strong>
    &nbsp;|&nbsp; Source: {source}
    &nbsp;|&nbsp; Generated: {generated}
    &nbsp;|&nbsp; <a href="{esc(back_link)}">← index</a>
  </div>
  <div class="pills">{pills}</div>
</header>
<main>
  <div class="nav">{nav_links}</div>
  {body_html}
  {json_html}
</main>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 – INDEX HTML
# ═══════════════════════════════════════════════════════════════════════════

def build_index(extracted_dir: Path,
                entries: list[dict]) -> str:
    """Build the master index.html listing all companies."""

    rows_html = ""
    for e in entries:
        rel      = _rel(extracted_dir / "index.html", Path(e["view_path"]))
        company  = esc(e["company"])
        period   = esc(e["period"])
        n_stmts  = e["n_statements"]
        keys     = ", ".join(e["statement_keys"])
        source   = esc(e["source_pdf"])
        gen      = esc(e["generated_at"])

        rows_html += f"""
<tr>
  <td><a href="{esc(rel)}">{company}</a></td>
  <td>{period}</td>
  <td class="num">{n_stmts}</td>
  <td class="muted" style="font-size:.72rem">{esc(keys)}</td>
  <td class="muted" style="font-size:.72rem">{source}</td>
  <td class="muted" style="font-size:.7rem">{gen}</td>
</tr>"""

    if not rows_html:
        rows_html = "<tr><td colspan='6' class='muted'>No companies found.</td></tr>"

    n = len(entries)
    now = datetime.now().isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Financial Statement Viewer — Index</title>
  <style>{CSS}</style>
</head>
<body>
<header>
  <h1>Financial Statement Viewer</h1>
  <div class="meta">
    {n} compan{"y" if n == 1 else "ies"} found in
    <code>{esc(str(extracted_dir.resolve()))}</code>
    &nbsp;|&nbsp; Built: {now}
  </div>
</header>
<main>
  <table class="idx">
    <thead>
      <tr>
        <th>Company</th>
        <th>Period</th>
        <th class="num">#Stmts</th>
        <th>Statement keys</th>
        <th>Source PDF</th>
        <th>Extracted</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</main>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 – DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════

def discover_companies(extracted_dir: Path) -> list[tuple[str, Path]]:
    """
    Return [(slug, results_json_path), …] for every company folder found
    under extracted_dir that contains a *_results.json file.
    """
    if not extracted_dir.exists():
        return []
    found = []
    for child in sorted(extracted_dir.iterdir()):
        if not child.is_dir():
            continue
        # Prefer <slug>_results.json, then any *_results.json
        named = child / f"{child.name}_results.json"
        if named.exists():
            found.append((child.name, named))
            continue
        candidates = sorted(child.glob("*_results.json"))
        if candidates:
            found.append((child.name, candidates[0]))
    return found


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 – RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def run(extracted_dir: Path, only: str | None) -> Path:
    extracted_dir.mkdir(parents=True, exist_ok=True)

    companies = discover_companies(extracted_dir)
    if only:
        companies = [(s, p) for s, p in companies if s == only]
        if not companies:
            print(f"\n  [error] No company slug '{only}' found in {extracted_dir}")
            sys.exit(2)

    print("=" * 70)
    print("  Q_TESTING — build side-by-side viewer (with raw JSON panel)")
    print(f"  Folder    : {extracted_dir}")
    print(f"  Companies : {len(companies)}")
    print("=" * 70 + "\n")

    index_entries: list[dict] = []
    n_done = n_fail = 0

    for idx, (slug, results_path) in enumerate(companies, start=1):
        company_label = slug
        print(f"company {idx} ({company_label}) Q report running")

        try:
            result = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception as ex:
            print(f"    → cannot read results JSON: {ex}")
            print("failed\n")
            n_fail += 1
            continue

        company_label = result.get("company", slug)
        print(f"    → {results_path.name}  (company: {company_label})")

        company_dir = results_path.parent
        view_path   = company_dir / f"{slug}_view.html"

        try:
            html_content = build_company_view(
                slug        = slug,
                company_dir = company_dir,
                result      = result,
                view_path   = view_path,
            )
            view_path.write_text(html_content, encoding="utf-8")
        except Exception as ex:
            print(f"    → view build error: {ex}")
            import traceback; traceback.print_exc()
            print("failed\n")
            n_fail += 1
            continue

        statements = result.get("statements", {})
        index_entries.append({
            "slug"          : slug,
            "company"       : result.get("company", slug),
            "period"        : result.get("period", ""),
            "source_pdf"    : result.get("source_pdf", ""),
            "generated_at"  : result.get("generated_at", ""),
            "n_statements"  : len(statements),
            "statement_keys": list(statements.keys()),
            "view_path"     : str(view_path),
        })
        print(f"done  ({len(statements)} statement(s) → {view_path.name})\n")
        n_done += 1

    index_path = extracted_dir / "index.html"
    index_path.write_text(
        build_index(extracted_dir, index_entries),
        encoding="utf-8",
    )
    print("=" * 70)
    print(f"  ALL COMPANIES PROCESSED")
    print(f"  done   : {n_done}")
    print(f"  failed : {n_fail}")
    print(f"  index  : {index_path}")
    print("=" * 70 + "\n")
    return index_path


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 – LOCAL SERVER
# ═══════════════════════════════════════════════════════════════════════════

def serve(serve_root: Path, port: int, open_path: str) -> None:
    serve_root = serve_root.resolve()
    Handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(serve_root), **kw
    )
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/{open_path}"
        print(f"  Serving  : http://127.0.0.1:{port}/")
        print(f"  Opening  : {url}")
        print("  (Ctrl+C to stop)\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server stopped.")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 11 – CLI
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Build side-by-side HTML viewer from Q_data_extraction output"
    )
    ap.add_argument(
        "--extracted", type=Path, default=Path("extracted"),
        metavar="DIR",
        help="Root folder produced by Q_data_extraction.py  (default: ./extracted)",
    )
    ap.add_argument(
        "--only", default=None, metavar="SLUG",
        help="Only rebuild this single company slug folder",
    )
    ap.add_argument(
        "--serve", action="store_true",
        help="Start a local web server after building and open the index",
    )
    ap.add_argument(
        "--port", type=int, default=8001,
        help="Port for --serve  (default: 8001)",
    )
    args = ap.parse_args(argv)

    extracted_dir = args.extracted.resolve()
    index_path    = run(extracted_dir, args.only)

    if args.serve:
        try:
            url_path = str(
                index_path.resolve().relative_to(extracted_dir)
            ).replace("\\", "/")
        except ValueError:
            url_path = "index.html"
        serve(extracted_dir, args.port, url_path)
    else:
        print(f"  Open in browser : {index_path}")
        print(f"  Tip: run with --serve to start a local web server\n")


if __name__ == "__main__":
    main()
