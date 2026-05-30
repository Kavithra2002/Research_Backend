"""
step2_capture_pages.py  (v6)
============================
STEP 2 of 3 - Render each detected statement page as a FULL-PAGE PNG image,
but only when (a) the page truly belongs to the right statement AND
(b) it actually contains a financial table.

Captured statements (the nine the user wants)
---------------------------------------------
  1. Income Statement / Statement of Profit or Loss
  2. Statement of Other Comprehensive Income (OCI)
  3. Statement of Financial Position (Balance Sheet / SoFP)
  4. Statement of Changes in Equity
  5. Statement of Cash Flows
  6. Shareholder Information
  7. Investor Information
  8. Ten Year Summary / Decade at a Glance
  9. Five Year Summary / Achievements

Notes are NEVER captured.

What changed in v6 (vs v5)
--------------------------
1. STRICT topic + table CONFIRMATION before capture.
   A page is captured only if EITHER:
     - its OWN statement heading appears as a prominent (short, top)
       heading line ("Topic confirmed"), OR
     - it's the very next page after a confirmed page AND has no
       competing section heading at its top AND contains a real table
       ("Continuation confirmed").
   Otherwise the page is skipped.

2. AGGRESSIVE non-target heading list.
   Pages whose top contains "Report of the Board of Directors", "Risk
   Management", "Material Accounting Policy", "Corporate Governance",
   "Chairman's Message", "Independent Auditor", etc. are skipped even if
   step 1 mistakenly included them in the page range.

3. Per-statement "previous page accepted" flag.
   Without this, a narrative gap between two real tables in the same
   section would let a later wrong page in.

Usage
-----
    python step2_capture_pages.py json_logs/company1_pages.json
    python step2_capture_pages.py json_logs/company1_pages.json --dpi 200
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

try:
    import fitz          # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF is required.  Run:  pip install PyMuPDF")
    sys.exit(1)

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber is required.  Run:  pip install pdfplumber")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

STMT_ORDER = [
    "income_statement",
    "oci",
    "sofp",
    "equity",
    "cash_flows",
    "shareholder_info",
    "investor_info",
    "ten_year_summary",
    "five_year_summary",
]
ALLOWED_KEYS = set(STMT_ORDER)

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
}


# ─────────────────────────────────────────────────────────────────────────────
# Heading patterns
# ─────────────────────────────────────────────────────────────────────────────

_OWN_HEADING: dict[str, re.Pattern] = {
    "income_statement":  re.compile(
        r"(?:income\s+statement|statement\s+of\s+profit(?:\s+or\s+loss)?)",
        re.I),
    "oci":               re.compile(
        r"statement\s+of\s+(?:other\s+)?comprehensive\s+income", re.I),
    "sofp":              re.compile(
        r"(?:statement\s+of\s+financial\s+position|balance\s+sheet)", re.I),
    "equity":            re.compile(
        r"statement\s+of\s+changes\s+in\s+equity", re.I),
    "cash_flows":        re.compile(
        r"(?:statement\s+of\s+cash\s+flows?|cash\s+flow\s+statement)", re.I),
    "shareholder_info":  re.compile(
        r"share(?:holder|s)?\s+information", re.I),
    "investor_info":     re.compile(
        r"investor\s+(?:information|relations)", re.I),
    "ten_year_summary":  re.compile(
        r"(?:ten[\s\-]year|10[\s\-]year)\s+"
        r"(?:summary|achievements?|highlights?|financial)|"
        r"decade\s+at\s+a\s+glance",
        re.I),
    "five_year_summary": re.compile(
        r"(?:five[\s\-]year|5[\s\-]year)\s+"
        r"(?:summary|achievements?|highlights?|financial)",
        re.I),
}

# Pages whose TOP shows ANY of these headings are NOT a statement page —
# regardless of what step 1 said.
# Share Performance is a narrative/chart section — not Investor Information.
_SHARE_PERF_RE = re.compile(r"\bshare\s+performance\b", re.I)

_BLOCKING_HEADINGS_RE = re.compile(
    r"\bshare\s+performance\b|"
    r"\boperating\s+environment\b|"
    r"\bthe\s+group[\u2019']?s?\s+strategy\b|"
    r"\bgroup[\u2019']?s?\s+strategy\b|"
    r"\bmaterial\s+accounting\s+polic|"
    r"\bsignificant\s+accounting\s+polic|"
    r"\baccounting\s+policies\b|"
    r"\breport\s+of\s+the\s+board\s+of\s+directors\b|"
    r"\bdirector(?:s)?'?s?\s+report\b|"
    r"\brisk\s+management\b|"
    r"\bcorporate\s+governance\b|"
    r"\bchairman[\u2019']?s?\s+message\b|"
    r"\bchief\s+executive\s+officer|"
    r"\bceo[\u2019']?s?\s+review\b|"
    r"\bindependent\s+auditor|"
    r"\bglossary\b|"
    r"\bgri\s+content\s+index\b|"
    r"\bsasb\s+disclosures\b|"
    r"\bnotice\s+of\s+(?:annual\s+)?(?:general\s+)?meeting\b|"
    r"\bform\s+of\s+proxy\b|"
    r"\bannexure\b|"
    r"\bcorporate\s+information\b|"
    r"\bvalue\s+creation\s+model\b|"
    r"\bstakeholder\s+engagement\b|"
    r"\bmateriality\b|"
    r"\bbranch\s+network\b|"
    r"\baudit\s+committee\b|"
    r"\bremuneration\s+committee\b|"
    r"\bnomination\s+(?:and|&)\s+governance\b|"
    r"\brelated\s+party\s+transactions\s+review\b|"
    r"\bdirector(?:s)?[\u2019']?\s+responsibilit|"
    r"\bstatement\s+of\s+value\s+added\b|"
    r"\bsegment(?:al)?\s+(?:information|review)\b|"
    r"\bnotes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements?\b|"
    r"\bfinancial\s+calendar\b|"
    r"\bquarterly\s+statistics\b|"
    r"\bus\s+dollar\s+financial\s+statements\b",
    re.I,
)

# Other known financial-statement headings — if the top of the page shows
# one of these and it isn't the current statement, skip the page.
_ANY_STMT_HEADING_RE = re.compile(
    r"\bincome\s+statement\b|"
    r"\bstatement\s+of\s+(?:other\s+)?comprehensive\s+income\b|"
    r"\bstatement\s+of\s+financial\s+position\b|"
    r"\bstatement\s+of\s+changes\s+in\s+equity\b|"
    r"\bstatement\s+of\s+cash\s+flows?\b|"
    r"\bbalance\s+sheet\b|"
    r"\bshare(?:holder)?s?\s+information\b|"
    r"\binvestor\s+(?:information|relations)\b|"
    r"\b(?:ten|five|10|5)[\s\-]year\s+(?:summary|achievements?|highlights?)\b|"
    r"\bdecade\s+at\s+a\s+glance\b",
    re.I,
)


# ─────────────────────────────────────────────────────────────────────────────
# Heading-prominence helper
# ─────────────────────────────────────────────────────────────────────────────

_LEADING_PG_NUM_RE = re.compile(r"^\s*\d{1,4}\s+")


def _has_prominent_heading(text: str, title_re: re.Pattern,
                           top_n: int = 14, max_len: int = 75) -> bool:
    """
    True only if `title_re` matches a STANDALONE heading line near the top
    of `text`.  The title must start at the beginning of a short line
    (a leading "170 " page-number prefix is tolerated) and must dominate
    that line (>= 50% of its remaining length or fill it).
    """
    if not text:
        return False

    seen = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        seen += 1
        if seen > top_n:
            break

        if len(line) > max_len:
            continue

        body = _LEADING_PG_NUM_RE.sub("", line, count=1)

        m = title_re.match(body)
        if not m:
            continue

        match_len = m.end() - m.start()
        if match_len >= len(body) - 4:
            return True
        if match_len / max(len(body), 1) >= 0.50:
            return True

    return False


def _top_block(text: str, n: int = 6) -> str:
    return "\n".join((text or "").split("\n")[:n])


# ─────────────────────────────────────────────────────────────────────────────
# Topic + table confirmation
# ─────────────────────────────────────────────────────────────────────────────

def _topic_confirmed(text: str, key: str) -> bool:
    """The page's TOPIC matches `key` (heading is at the top of the page)."""
    if not text:
        return False

    top = _top_block(text, 6)

    # If a clearly-different section heading dominates the top, reject.
    if _BLOCKING_HEADINGS_RE.search(top):
        return False

    own_re = _OWN_HEADING[key]
    if _ANY_STMT_HEADING_RE.search(top) and not own_re.search(top):
        return False

    # Investor Information must say "investor" — not "Share Performance" alone.
    if key == "investor_info":
        if _SHARE_PERF_RE.search(top) and not own_re.search(top):
            return False
        if not _has_prominent_heading(text, own_re):
            return False
        return True

    return _has_prominent_heading(text, own_re)


def _continuation_topic_ok(text: str, key: str) -> bool:
    """
    For continuation pages: only require that no DIFFERENT section heading
    appears at the top.  The page itself may not repeat the statement title.
    """
    if not text:
        return False

    top = _top_block(text, 6)

    if _BLOCKING_HEADINGS_RE.search(top):
        return False

    own_re = _OWN_HEADING[key]
    if _ANY_STMT_HEADING_RE.search(top) and not own_re.search(top):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Real-table presence check
#
# Keeps narrative or text-only pages out of the capture, even when topic
# would otherwise look OK.
# ─────────────────────────────────────────────────────────────────────────────

_NUM_RE     = re.compile(r"\d{3,}|\d+[.,]\d{2,}")
_BIG_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\(\d{1,3}(?:,\d{3})+\)")


def _cell_is_numeric(cell) -> bool:
    if not cell:
        return False
    return bool(_NUM_RE.search(str(cell).strip()))


def _cell_is_big_number(cell) -> bool:
    if not cell:
        return False
    return bool(_BIG_NUM_RE.search(str(cell)))


def _table_looks_real(data, bbox, page_width) -> bool:
    if not data:
        return False
    if len(data) < 3:
        return False
    if max((len(r) for r in data), default=0) < 2:
        return False

    numeric_cells = 0
    big_num_cells = 0
    numeric_rows  = 0
    for row in data:
        row_nums = 0
        for cell in row:
            if _cell_is_numeric(cell):
                numeric_cells += 1
                row_nums      += 1
            if _cell_is_big_number(cell):
                big_num_cells += 1
        if row_nums >= 1:
            numeric_rows += 1

    if numeric_cells < 4:
        return False
    if numeric_rows < 2:
        return False
    if big_num_cells < 2:
        return False

    x0, _, x1, _ = bbox
    if (x1 - x0) < page_width * 0.20:
        return False

    return True


_TABLE_SETTINGS = [
    {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    {"vertical_strategy": "lines", "horizontal_strategy": "text",
     "intersection_tolerance": 6},
    {"vertical_strategy": "text",  "horizontal_strategy": "text",
     "snap_tolerance": 4, "join_tolerance": 4, "intersection_tolerance": 5,
     "min_words_vertical": 2, "min_words_horizontal": 2},
]


def page_has_real_table(pdf_path: str, page_idx: int,
                        page_width: float) -> bool:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_idx >= len(pdf.pages):
                return False
            page = pdf.pages[page_idx]

            for settings in _TABLE_SETTINGS:
                try:
                    tables = page.find_tables(table_settings=settings)
                except Exception:
                    continue
                for t in tables:
                    try:
                        data = t.extract()
                    except Exception:
                        continue
                    if _table_looks_real(data, t.bbox, page_width):
                        return True
    except Exception:
        return False

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Full-page rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_full_page(pdf_path: str, page_idx: int, dpi: int = 150) -> bytes | None:
    try:
        doc  = fitz.open(pdf_path)
        page = doc[page_idx]
        mat  = fitz.Matrix(dpi / 72, dpi / 72)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        png  = pix.tobytes("png")
        doc.close()
        return png
    except Exception as e:
        print(f"    !! render error (page {page_idx + 1}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# HTML preview builder
# ─────────────────────────────────────────────────────────────────────────────

def _img_tag(png_path: Path) -> str:
    data = png_path.read_bytes()
    b64  = base64.b64encode(data).decode()
    return (
        f'<img src="data:image/png;base64,{b64}" '
        f'style="max-width:100%;border:1px solid #ccc;border-radius:4px;'
        f'display:block;margin:4px 0;" />'
    )


def build_preview(manifest, img_map, out_path: Path) -> None:
    company  = manifest.get("company", "")
    pdf_name = Path(manifest.get("source_pdf", "")).name
    stmts    = manifest.get("statements", {})

    nav = "".join(
        f'<a href="#{k}" style="display:block;padding:6px 12px;color:#d1d5db;'
        f'text-decoration:none;font-size:0.75rem;border-left:3px solid '
        f'{STMT_COLORS.get(k,"#4b5563")};">'
        f'{stmts[k]["title"] if k in stmts else k}</a>'
        for k in STMT_ORDER if k in img_map
    )

    sections = ""
    for k in STMT_ORDER:
        if k not in img_map:
            continue
        color   = STMT_COLORS.get(k, "#374151")
        info    = stmts.get(k, {})
        title   = info.get("title", k.replace("_", " ").title())
        pgs     = info.get("pdf_pages_1based", [])
        images  = img_map[k]

        pgs_str = (f"pg {pgs[0]}" if len(pgs) == 1
                   else f"pg {pgs[0]}-{pgs[-1]}") if pgs else ""
        imgs_html = "".join(_img_tag(p) for p in images)
        if not imgs_html:
            imgs_html = ('<p style="color:#9ca3af;font-style:italic;'
                         'padding:1rem;">No confirmed pages found.</p>')

        sections += f"""
        <section id="{k}" style="margin-bottom:2.5rem;background:#fff;
          border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
          <div style="background:{color};padding:0.65rem 1.1rem;
            display:flex;align-items:center;justify-content:space-between;">
            <span style="color:#fff;font-weight:700;font-size:0.9rem;">{title}</span>
            <span style="color:rgba(255,255,255,0.8);font-size:0.68rem;">
              {len(images)} image(s) &middot; {pgs_str}</span>
          </div>
          <details open style="padding:0.6rem 0.9rem 0.9rem;">
            <summary style="cursor:pointer;font-size:0.78rem;color:#374151;
              font-weight:600;margin-bottom:0.6rem;">
              {len(images)} captured image(s)
            </summary>
            {imgs_html}
          </details>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Captures - {company}</title>
  <style>
    body{{font-family:system-ui,sans-serif;background:#f3f4f6;margin:0;display:flex;}}
    nav{{position:fixed;top:0;left:0;width:195px;height:100vh;overflow-y:auto;
      background:#1f2937;padding:1rem 0 2rem;}}
    nav::before{{content:"Statements";display:block;color:#6b7280;font-size:0.6rem;
      letter-spacing:0.12em;text-transform:uppercase;padding:0 12px 10px;}}
    main{{margin-left:195px;flex:1;padding:1.5rem 2rem 4rem;max-width:960px;}}
    h1{{font-size:1.3rem;font-weight:800;color:#111827;margin-bottom:0.2rem;}}
    .meta{{color:#6b7280;font-size:0.72rem;margin-bottom:1.5rem;}}
  </style>
</head><body>
  <nav>{nav}</nav>
  <main>
    <h1>Page Captures - {company}</h1>
    <div class="meta">PDF: {pdf_name} &nbsp;|&nbsp;
      Total PDF pages: {manifest.get('total_pdf_pages','')}</div>
    {sections}
  </main>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run(manifest_path: str | Path,
        out_dir:       str | Path | None = None,
        dpi:           int  = 150) -> dict:

    manifest_path = Path(manifest_path).resolve()
    manifest      = json.loads(manifest_path.read_text(encoding="utf-8"))

    company  = manifest.get("company", "company")
    pdf_path = manifest.get("source_pdf", "")
    stmts    = manifest.get("statements", {})

    if not Path(pdf_path).exists():
        print(f"\n  ERROR [{company}]: PDF not found -> {pdf_path}")
        return {}

    if out_dir is None:
        out_dir = manifest_path.parent / f"{company}_captures"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  [{company}]  capturing ...", end="", flush=True)

    # Pre-read page dims & full text for cheap filtering.
    page_dims:  dict[int, tuple[float, float]] = {}
    page_texts: dict[int, str]                 = {}
    try:
        with pdfplumber.open(pdf_path) as _pdf:
            for i, pg in enumerate(_pdf.pages):
                page_dims[i] = (float(pg.width), float(pg.height))
                try:
                    txt = pg.extract_text() or ""
                except Exception:
                    txt = ""
                # We need more than 8 lines now — heading might sit a bit
                # lower on some report templates.
                page_texts[i] = "\n".join(txt.split("\n")[:18])
    except Exception:
        pass

    img_map:           dict[str, list[Path]] = {}
    saved_total        = 0
    skipped_topic      = 0
    skipped_no_table   = 0
    skipped_notes      = 0

    ordered_keys  = [k for k in STMT_ORDER if k in stmts]
    skipped_notes = sum(stmts[k].get("page_count", 0)
                        for k in stmts if k not in ALLOWED_KEYS)

    for key in ordered_keys:
        info    = stmts[key]
        indices = info["pdf_indices_0based"]

        stmt_dir = out_dir / key
        stmt_dir.mkdir(exist_ok=True)
        saved: list[Path] = []
        prev_confirmed   = False

        for pos, idx_0 in enumerate(indices):
            is_first = (pos == 0)
            page_1   = idx_0 + 1
            pg_txt   = page_texts.get(idx_0, "")

            # ── 1. Topic confirmation (the critical fix) ──────────────────
            if is_first:
                if not _topic_confirmed(pg_txt, key):
                    # The very first page doesn't actually have the right
                    # heading — that means step 1 picked a wrong page.  We
                    # skip the entire statement to avoid mis-captures.
                    skipped_topic += len(indices)
                    break
                topic_ok = True
            else:
                # Continuation: either the same statement's heading repeats
                # (rare "Contd." pages) OR no different-section heading at
                # the top AND the previous page was already confirmed.
                same_heading_now = _has_prominent_heading(
                    pg_txt, _OWN_HEADING[key])
                if same_heading_now:
                    topic_ok = True
                elif prev_confirmed and _continuation_topic_ok(pg_txt, key):
                    topic_ok = True
                else:
                    topic_ok = False

            if not topic_ok:
                skipped_topic += 1
                # Stop extending — once a wrong page is hit, the rest of
                # the range almost always belongs to a different section.
                break

            # ── 2. Real-table check ──────────────────────────────────────
            pw, _ = page_dims.get(idx_0, (595.0, 842.0))
            if not page_has_real_table(pdf_path, idx_0, pw):
                skipped_no_table += 1
                # A blank / narrative page in the middle doesn't break the
                # run; the next page can still be a continuation.
                prev_confirmed = True
                continue

            # ── 3. Render the full page ──────────────────────────────────
            img_path = stmt_dir / f"page_{page_1:04d}.png"
            if not img_path.exists():
                png = render_full_page(pdf_path, idx_0, dpi=dpi)
                if not png:
                    prev_confirmed = True
                    continue
                img_path.write_bytes(png)

            saved.append(img_path)
            saved_total += 1
            prev_confirmed = True

        img_map[key] = saved

    preview_path = out_dir / f"{company}_preview.html"
    build_preview(manifest, img_map, preview_path)

    extras = []
    if skipped_no_table: extras.append(f"{skipped_no_table} no-table page(s) skipped")
    if skipped_topic:    extras.append(f"{skipped_topic} wrong-topic page(s) skipped")
    if skipped_notes:    extras.append(f"{skipped_notes} notes/other page(s) ignored")
    extra_str = "  (" + "; ".join(extras) + ")" if extras else ""

    print(f"  done.  {saved_total} image(s) saved{extra_str}  ->  {out_dir.name}/")

    return {"img_map": img_map, "preview_path": preview_path}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=(
            "STEP 2 - Capture FULL-PAGE images of the nine financial statements "
            "with strict topic + table confirmation."
        )
    )
    ap.add_argument("manifest",      type=Path,
                    help="JSON manifest from step1_find_pages.py")
    ap.add_argument("--dpi",         type=int, default=150,
                    help="Render DPI (default 150, use 200 for higher quality)")
    ap.add_argument("--out",         type=Path, default=None,
                    help="Output folder (default: <company>_captures/ beside manifest)")
    args = ap.parse_args()

    run(args.manifest, out_dir=args.out, dpi=args.dpi)
