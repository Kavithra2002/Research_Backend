"""
step3_send_to_openai.py
=======================
STEP 3 of 3 — Send the captured page images to OpenAI GPT-4o Vision
and save a structured JSON + HTML report of the extracted financial data.

What it does
------------
  1. Reads the manifest (from step1) and the captured images (from step2).
  2. For each financial statement sends all its page images + a targeted
     extraction prompt to GPT-4o.
  3. Parses the JSON response from the model.
  4. Saves results to  <company>_results.json
  5. Builds a readable HTML report  <company>_results.html

Usage
-----
    # Option 1 — core statements only (cheaper)
    python step3_send_to_openai.py HemasPLC_pages.json \\
        --apikey sk-...  --captures ./HemasPLC_captures  --option 1

    # Option 2 — core statements + Notes
    python step3_send_to_openai.py HemasPLC_pages.json \\
        --apikey sk-...  --captures ./HemasPLC_captures  --option 2

    # Dry-run — print what would be sent, no API calls
    python step3_send_to_openai.py HemasPLC_pages.json \\
        --captures ./HemasPLC_captures  --dry-run

Notes
-----
  • The script sends ONE API call per statement (all pages of that statement
    in a single message). This keeps context together for multi-page tables.
  • GPT-4o supports up to 20 images per request; large Notes sections are
    chunked into batches of 15 pages.
  • Set --model gpt-4o-mini for a cheaper (but less accurate) run.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package required.  Run:  pip install openai")
    sys.exit(1)


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "Extracted_json"


# ─────────────────────────────────────────────────────────────────────────────
# Verbatim transcription prompt
# ─────────────────────────────────────────────────────────────────────────────
#
# All statements now use the SAME generic schema so GPT-4o transcribes the
# table EXACTLY as printed, without summarising / renaming / re-shaping it.
#
# Output JSON (every statement):
#
# {
#   "statement_title": "<exact title as printed>",
#   "preamble":  "<text printed above the table, or null>",
#   "footnotes": "<text printed below the table, or null>",
#   "tables": [
#     {
#       "caption":     "<sub-table caption (e.g. 'ASSETS') or null>",
#       "header_rows": [["", "Note", "2025", "2024"], ["", "", "Rs.", "Rs."]],
#       "rows": [
#         {"cells": ["Income", "4", "4,727,585,483", "4,868,237,960"],
#          "style": "data"},
#         {"cells": ["EXPENSES", "", "", ""], "style": "section"},
#         {"cells": ["Net interest income", "", "2,146,394,968",
#                    "1,794,407,747"], "style": "subtotal"}
#       ]
#     }
#   ]
# }
#
# Allowed values for "style":
#   "data"      - ordinary line item rows
#   "section"   - section header rows (e.g. "REVENUE", "ASSETS")
#   "subtotal"  - intermediate subtotal rows
#   "total"     - grand-total rows (e.g. "Total Assets")
#   "blank"     - visually blank row in the original PDF
# ─────────────────────────────────────────────────────────────────────────────

_VERBATIM_RULES = """
CRITICAL RULES - read VERY carefully. Follow EVERY rule.

============================================================
A. GENERAL TRANSCRIPTION
============================================================
1. Transcribe the table(s) EXACTLY as printed. Do NOT summarise, paraphrase,
   re-order, translate, or rename ANY row, column, or value.
2. Keep EVERY visible row, including section banners ("REVENUE", "ASSETS",
   "EXPENSES", "EQUITY", "LIABILITIES"), sub-totals, totals, and totals at
   the very bottom of the table.
3. Output every cell as a STRING - keep the original formatting verbatim:
   commas (e.g. "4,727,585,483"), decimal points, parentheses around
   negatives (e.g. "(2,317,453,659)"), blank cells as "", dashes as "-".
   Do NOT convert numbers, do NOT remove formatting, do NOT change signs.

============================================================
B. COLUMNS AND ROW-WIDTH (THE MOST IMPORTANT RULE)
============================================================
4. FIRST, look at the WIDEST visible row of the table (usually one of the
   "Balance as at ..." rows in an Equity table, or the value rows of a
   side-by-side comparative table) and count how many distinct PRINTED
   columns there are. That number is N.
5. EVERY single header_row and EVERY single body row in the output MUST
   contain EXACTLY N cells. No row may be shorter or longer than N cells.
   - If a printed cell is visually empty for that row, output "" (or "-"
     if the PDF prints a dash). NEVER omit the cell.
   - Do NOT "collapse" empty middle columns by shifting later values left.
     If a row has "2,431,879,039 | 375,736,747 | 141,120,773 | (blank) |
     332,527,607 | 3,281,006,166", the cell list MUST contain an empty ""
     in the 4th value-position, NOT a shifted list of 5 numbers.
6. The Note column (when present) is ALWAYS the 2nd cell (index 1), right
   after the label. If a particular row has no note number, output "" for
   that cell - never put a value into the Note slot. Never drop the Note
   slot just because one row is missing the note.
   - Note refs look like: "4", "17", "23.1", "(Note 34.1)" - keep verbatim.

============================================================
C. HEADERS (multi-line and grouped/span headers)
============================================================
7. Keep the column headers EXACTLY as printed.
   - If the header is printed on MULTIPLE lines (e.g. "For the year ended"
     above, then "31st March" below, then "2025" / "2024" below that,
     then "Rs." / "Rs." below that) output EACH printed line as a SEPARATE
     entry inside "header_rows", in top-to-bottom order.
   - Do NOT merge / collapse / abbreviate header lines.
   - Every header_row MUST have N cells (same as data rows). Use "" for
     empty positions.
8. If a header label visually SPANS multiple sub-columns (typical for
   comparative tables: "As at 31st March 2025" sits above three sub-columns
   "No. of Shareholders", "No. of Shares", "% of Shares"), then:
   - Put the spanning label in the LEFTMOST of those positions in the
     header row, and leave the other positions EMPTY ("") so the renderer
     can detect the span.
   - Example for Share Information with 8 total columns:
       header_rows[0] = ["", "", "As at 31st March 2025", "", "",
                         "As at 31st March 2024", "", ""]
       header_rows[1] = ["", "", "No. of Shareholders", "No. of Shares",
                         "% of Shares", "No. of Shareholders",
                         "No. of Shares", "% of Shares"]
9. Do NOT invent a "Note" column if the PDF doesn't have one. Do NOT drop
   the "Note" column if it IS there.

============================================================
D. WIDE TABLES (especially Statement of Changes in Equity)
============================================================
10. Statement of Changes in Equity is a WIDE table. Each row is a movement
    (Balance b/f, Profit for the year, Transfer to reserve, ...) and the
    COLUMNS are equity components (Stated capital, Statutory reserve fund,
    Revaluation reserve, Non-distributable regulatory loss allowance
    reserve, Retained earnings, Total - exact list varies by company).
    - You MUST capture EVERY equity-component column, even if many rows
      are blank or "-" in that column.
    - You MUST keep the column count consistent across ALL rows.
    - Re-count columns by looking at the very first "Balance as at ..."
      row - that row almost always has values in EVERY column.

============================================================
E. SIDE-BY-SIDE COMPARATIVE TABLES (Share Information, etc.)
============================================================
11. Many disclosure tables print BOTH reporting periods side-by-side
    (e.g. Share Information shows "2025" group of 3 columns AND "2024"
    group of 3 columns next to each other). You MUST capture ALL columns
    for BOTH periods. NEVER drop the older period's columns.

============================================================
F. SECTION BANNERS AND LINE WRAPS
============================================================
12. If a row contains a section banner that spans across all columns (e.g.
    "ASSETS", "OPERATING EXPENSES", "OTHER COMPREHENSIVE INCOME"), put
    that text in the FIRST cell and leave the other cells "" (still N
    cells total), and set style to "section".
13. PRESERVE PRINTED LINE BREAKS inside long cells.
    - If a row label is printed on TWO visual lines in the PDF (e.g.
      "Other comprehensive income to be re-classified" on line 1, then
      "to profit or loss in subsequent periods" on line 2), output the
      cell value with a literal "\\n" between the two lines:
        "Other comprehensive income to be re-classified\\nto profit or loss in subsequent periods"
    - Do the same for any other multi-line printed cell (long headers,
      long disclosure-table labels, etc.).
    - This applies to header cells AND body cells.

============================================================
G. ROWS, STYLES, MULTI-TABLE PAGES
============================================================
14. If a row is visually blank in the PDF, still include it with style
    "blank" so the layout is preserved.
15. If the page contains several SEPARATE tables (e.g. an Assets table, a
    Liabilities table and an Equity table that each have their own column
    headers), output them as separate items in the "tables" array, each
    with its own "caption" and "header_rows".

============================================================
H. OUTPUT FORMAT
============================================================
16. Do NOT include any keys other than the ones shown below. Do NOT wrap
    the JSON in markdown code fences. Do NOT add commentary before or
    after the JSON. Return ONLY the JSON object.

Return ONLY a JSON object with this EXACT shape:

{
  "statement_title": "<exact title as printed, e.g. 'INCOME STATEMENT'>",
  "preamble":  "<text printed above the table (e.g. 'For the year ended 31st March') or null>",
  "footnotes": "<text printed below the table or null>",
  "tables": [
    {
      "caption": "<sub-table caption (e.g. 'ASSETS') or null>",
      "header_rows": [
        ["", "Note", "2025", "2024"],
        ["", "",    "Rs.",  "Rs."]
      ],
      "rows": [
        {"cells": ["Income", "4", "4,727,585,483", "4,868,237,960"], "style": "data"},
        {"cells": ["Net interest income", "", "2,146,394,968", "1,794,407,747"], "style": "subtotal"},
        {"cells": ["EXPENSES", "", "", ""], "style": "section"},
        {"cells": ["Total Assets", "", "27,357,674,016", "20,477,357,040"], "style": "total"}
      ]
    }
  ]
}

EXAMPLE of CORRECT equity row alignment (N = 8 columns):

  header_rows: [
    ["",                              "Note", "Stated capital", "Statutory reserve fund",
     "Revaluation reserve",           "Non-distributable regulatory loss allowance reserve",
     "Retained earnings",             "Total"],
    ["",                              "",     "Rs.",            "Rs.",
     "Rs.",                           "Rs.",
     "Rs.",                           "Rs."]
  ]

  row (Balance b/f - 7 visible values, the 4th column is empty):
    {"cells": ["Balance as at 31st March 2023", "",
               "2,431,879,039", "375,736,747", "141,120,773",
               "",                       // <-- KEEP empty cell for the missing column!
               "332,527,607", "3,281,006,166"], "style": "data"}

  row (Transfer with note reference):
    {"cells": ["Transfer to statutory reserve", "(Note 34.1)",
               "-", "17,426,357", "-", "-", "(17,426,357)", "-"], "style": "data"}
""".strip()


def _verbatim_prompt(statement_label: str, extra_hint: str = "") -> str:
    hint = f"\n\nContext hint: these images show the **{statement_label}**." \
           f"{(' ' + extra_hint) if extra_hint else ''}"
    return ("You are a financial-statement OCR / transcription specialist."
            + hint + "\n\n" + _VERBATIM_RULES)


PROMPTS = {
    "income_statement": _verbatim_prompt(
        "Income Statement / Statement of Profit or Loss",
        "Expect a column for 'Note' and one column per reporting period. "
        "The Note column ALWAYS sits in slot index 1 (right after the label). "
        "If a particular row has no note number, leave its Note cell empty "
        "(\"\") - never push a value into the Note slot."
    ),
    "oci": _verbatim_prompt(
        "Statement of Other Comprehensive Income (OCI)",
        "It usually starts with 'Profit for the year' and ends with "
        "'Total comprehensive income for the year'. Several section banners "
        "have LONG printed text that wraps onto two lines in the PDF (e.g. "
        "'Other comprehensive income to be re-classified to profit or loss "
        "in subsequent periods'). For any such cell, insert a literal \\n "
        "at the exact point where the printed text breaks to the next line."
    ),
    "sofp": _verbatim_prompt(
        "Statement of Financial Position (Balance Sheet)",
        "Expect section banners like ASSETS / LIABILITIES / EQUITY and "
        "sub-totals like 'Total Assets', 'Total Liabilities', "
        "'Total Equity', 'Total Equity and Liabilities'. Keep them all."
    ),
    "equity": _verbatim_prompt(
        "Statement of Changes in Equity",
        "WIDE table - rows are movements (Balance b/f, Profit for the year, "
        "Transfer to reserve, ...) and columns are equity components "
        "(Stated capital, Statutory reserve fund, Revaluation reserve, "
        "Non-distributable regulatory loss allowance reserve, Retained "
        "earnings, Total, etc.). \n\n"
        "VERY IMPORTANT: count the equity-component columns by looking at "
        "the FIRST 'Balance as at ...' row (it almost always has a value in "
        "every column). Then EVERY row in the table - including rows that "
        "look short like 'Profit for the year' (which only has values in "
        "Retained earnings and Total) - MUST contain that same number of "
        "cells. Fill missing positions with \"\" (or \"-\" if the PDF prints "
        "a dash). Do NOT shift values left to skip empty middle columns. "
        "Keep all reporting periods shown (both 2025 and 2024 totals)."
    ),
    "cash_flows": _verbatim_prompt(
        "Statement of Cash Flows",
        "Keep the three section banners (Operating activities, Investing "
        "activities, Financing activities) and every sub-total such as "
        "'Net cash from operating activities'."
    ),
    "shareholder_info": _verbatim_prompt(
        "Shareholder / Share Information",
        "There may be SEVERAL separate tables on these pages (Share "
        "Information distribution, Analysis of Share Holders, Public "
        "Holding, Top 20 shareholders, Market price, Ratios). Output each "
        "as its own item in the 'tables' array.\n\n"
        "Many of these tables print BOTH 2025 AND 2024 SIDE BY SIDE (e.g. "
        "'As at 31st March 2025' header spans the columns "
        "'No. of Shareholders | No. of Shares | % of Shares' AND right "
        "next to it 'As at 31st March 2024' spans the SAME 3 sub-columns "
        "again). You MUST capture ALL 6 (or however many) sub-columns - "
        "do NOT drop the 2024 side. Use the grouped-header pattern: place "
        "the spanning label in its leftmost position and leave the other "
        "spanned positions empty in that header row."
    ),
    "investor_info": _verbatim_prompt(
        "Investor Information / Key Investor Ratios",
        "Each row is a ratio/metric, each column is a reporting period."
    ),
    "ten_year_summary": _verbatim_prompt(
        "Ten Year Summary / Decade at a Glance",
        "Wide table with 10 year columns. Keep EVERY year column even if "
        "some cells are blank."
    ),
    "five_year_summary": _verbatim_prompt(
        "Five Year Summary",
        "Wide table with 5 year columns. Keep EVERY year column."
    ),
    "notes": _verbatim_prompt(
        "Notes to the Financial Statements",
        "Each note may contain prose AND one or more tables. For these "
        "pages, output one entry in 'tables' for EACH note's sub-table you "
        "see, and put the note number + title in 'caption' (e.g. "
        "'Note 4 - Income'). Long prose can go into 'preamble' or "
        "'footnotes' as needed.\n\n"
        "NOTES HEADER EXCEPTION (overrides rule 7 for year/unit lines): "
        "In COMB/bank note tables the amount-column header is usually ONE "
        "printed cell containing the year and the unit on two lines "
        "(e.g. '2020' above \"Rs. '000\" inside the same cell). "
        "Keep that as ONE header cell using a literal \\n between the lines, "
        "e.g. \"2020\\nRs. '000\". Do NOT put the year in one header_rows "
        "entry and \"Rs. '000\" in a separate header_rows entry below it."
    ),
}

CORE_KEYS = [
    "income_statement", "oci", "sofp", "equity", "cash_flows",
    "shareholder_info", "investor_info", "ten_year_summary", "five_year_summary",
]

STMT_ORDER = CORE_KEYS + ["notes"]

MAX_IMGS_PER_CALL = 15   # stay well under GPT-4o's 20-image limit
RETRY_DELAY       = 5    # seconds between retries


# ─────────────────────────────────────────────────────────────────────────────
# Image helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _img_content(b64: str) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
    }


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI call with retry
# ─────────────────────────────────────────────────────────────────────────────

def call_gpt4o(
    client:    "OpenAI",
    images_b64: list[str],
    prompt:    str,
    model:     str = "gpt-4o",
    retries:   int = 3,
    max_tokens: int = 16384,
) -> str:
    content = [_img_content(b) for b in images_b64]
    content.append({"type": "text", "text": prompt.strip()})
    # GPT-5 family uses max_completion_tokens and often rejects temperature.
    is_gpt5 = model.lower().startswith("gpt-5")

    for attempt in range(1, retries + 1):
        try:
            kwargs: dict = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
            }
            if is_gpt5:
                kwargs["max_completion_tokens"] = max_tokens
            else:
                kwargs["max_tokens"] = max_tokens
                kwargs["temperature"] = 0
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as e:
            print(f"      [warn] API error (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# JSON extraction from model response
# ─────────────────────────────────────────────────────────────────────────────

def extract_json(raw: str) -> dict | list | None:
    """Extract the first valid JSON object or array from a model response."""
    # Remove markdown code fences
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    # Try parsing the whole thing
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try finding the first {...} or [...]
    for pat in (r"\{[\s\S]+\}", r"\[[\s\S]+\]"):
        m = re.search(pat, cleaned)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass

    return None   # couldn't parse — return None and store raw


# ─────────────────────────────────────────────────────────────────────────────
# Process one statement
# ─────────────────────────────────────────────────────────────────────────────

def process_statement(
    key:         str,
    img_paths:   list[Path],
    client:      "OpenAI",
    model:       str,
    dry_run:     bool,
) -> dict:
    prompt = PROMPTS.get(key, f"Extract all data from these {key} pages as JSON.")

    print(f"  [{key}]  {len(img_paths)} page image(s)")

    if dry_run:
        print(f"    DRY-RUN — would send {len(img_paths)} images")
        return {"status": "dry_run", "pages": [p.name for p in img_paths]}

    if not img_paths:
        return {"status": "no_images"}

    # Split into batches if needed (Notes can be 50+ pages)
    all_results: list[dict | list | None] = []
    batches = [
        img_paths[i: i + MAX_IMGS_PER_CALL]
        for i in range(0, len(img_paths), MAX_IMGS_PER_CALL)
    ]

    for b_idx, batch in enumerate(batches):
        batch_label = f"batch {b_idx+1}/{len(batches)}" if len(batches) > 1 else ""
        print(f"    Sending {len(batch)} image(s) to {model}  {batch_label} …")

        imgs_b64 = [_load_b64(p) for p in batch]

        # For multi-batch Notes, add a batch hint to the prompt
        batch_prompt = prompt
        if len(batches) > 1:
            batch_prompt = (
                f"[BATCH {b_idx+1} of {len(batches)} — "
                f"pages {b_idx*MAX_IMGS_PER_CALL+1} to "
                f"{min((b_idx+1)*MAX_IMGS_PER_CALL, len(img_paths))}]\n\n"
                + prompt
            )

        try:
            raw = call_gpt4o(client, imgs_b64, batch_prompt, model=model)
        except Exception as e:
            return {"status": "api_error", "error": str(e)}

        parsed = extract_json(raw)
        if parsed is not None:
            print(f"    [ok]   JSON parsed successfully")
            all_results.append(parsed)
        else:
            print(f"    [warn] Could not parse JSON - storing raw response")
            all_results.append({"status": "parse_failed", "raw_response": raw[:2000]})

        time.sleep(1)  # brief pause between calls

    # Merge batch results
    if len(all_results) == 1:
        result = all_results[0]
    else:
        # For Notes: merge the 'notes' arrays from each batch
        result = {"status": "multi_batch", "batches": all_results}
        if key == "notes":
            merged_notes = []
            for r in all_results:
                if isinstance(r, dict) and "notes" in r:
                    merged_notes.extend(r["notes"])
            if merged_notes:
                result = {"title": "Notes to the Financial Statements",
                          "notes": merged_notes}

    return {"status": "ok", "data": result}


# ─────────────────────────────────────────────────────────────────────────────
# HTML results report
# ─────────────────────────────────────────────────────────────────────────────

STMT_COLORS = {
    "income_statement": "#1a6b3c", "oci": "#2e7d8a", "sofp": "#1e3a8a",
    "equity": "#6b21a8", "cash_flows": "#92400e", "shareholder_info": "#065f46",
    "investor_info": "#0c4a6e", "ten_year_summary": "#78350f",
    "five_year_summary": "#831843", "notes": "#374151",
}

def build_results_html(company: str, results: dict) -> str:
    import html as html_lib

    sections = ""
    for key in STMT_ORDER:
        if key not in results:
            continue
        color  = STMT_COLORS.get(key, "#374151")
        title  = results[key].get("title", key.replace("_"," ").title())
        status = results[key].get("status", "")
        data   = results[key].get("data")

        if isinstance(data, (dict, list)):
            content = (
                f'<pre style="background:#0a0b0e;color:#a5f3fc;padding:1rem;'
                f'font-size:0.72rem;overflow-x:auto;max-height:600px;'
                f'overflow-y:auto;white-space:pre-wrap;word-break:break-word;">'
                f'{html_lib.escape(json.dumps(data, indent=2, ensure_ascii=False)[:8000])}'
                f'</pre>'
            )
        elif status == "dry_run":
            content = f'<p style="color:#f59e0b;padding:1rem;">Dry-run — not sent to API</p>'
        else:
            raw = results[key].get("raw_response", str(results[key]))
            content = (
                f'<pre style="padding:1rem;color:#fca5a5;font-size:0.72rem;">'
                f'{html_lib.escape(str(raw)[:3000])}</pre>'
            )

        sections += f"""
        <section style="margin-bottom:2rem;background:#141820;
          border:1px solid #1f2535;border-radius:8px;overflow:hidden;">
          <div style="background:{color};padding:0.7rem 1.1rem;">
            <span style="color:#fff;font-weight:700;font-size:0.9rem;">{title}</span>
            <span style="float:right;color:rgba(255,255,255,0.7);font-size:0.65rem;">{status}</span>
          </div>
          <details open><summary style="padding:0.6rem 1rem;cursor:pointer;
            color:#9ca3af;font-size:0.75rem;">Show / hide extracted data</summary>
            {content}
          </details>
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Extraction Results — {company}</title>
  <style>
    body{{font-family:system-ui,sans-serif;background:#080a10;color:#dde3f0;
      padding:2rem;max-width:1100px;margin:0 auto;}}
    h1{{font-size:1.5rem;font-weight:800;color:#fff;margin-bottom:0.25rem;}}
    .meta{{color:#5a6480;font-size:0.72rem;margin-bottom:2rem;}}
    details summary::-webkit-details-marker{{color:#5a6480;}}
  </style>
</head>
<body>
  <h1>Extraction Results — {company}</h1>
  <div class="meta">Generated: {datetime.now().isoformat(timespec='seconds')}</div>
  {sections}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_statements(
    manifest_path: str | Path,
    captures_dir: str | Path,
    api_key: str | None,
    keys: list[str],
    model: str = "gpt-4o",
    dry_run: bool = False,
    out_dir: str | Path | None = None,
    existing_results: dict[str, dict] | None = None,
    write_outputs: bool = True,
) -> dict[str, dict]:
    """
    Extract ONLY the given statement keys.  Merges into *existing_results*
    when provided.  Returns the combined results dict.
    """
    manifest_path = Path(manifest_path).resolve()
    captures_dir = Path(captures_dir).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    company = manifest.get("company", "company")
    stmts = manifest.get("statements", {})

    if out_dir is None:
        out_dir = DEFAULT_OUTPUT_ROOT / company
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = None
    if not dry_run:
        if not api_key:
            raise ValueError("API key required unless dry_run=True")
        client = OpenAI(api_key=api_key)

    results: dict[str, dict] = dict(existing_results or {})

    for key in keys:
        if key not in stmts:
            print(f"  [{key}]  NOT IN MANIFEST — skipping")
            continue

        stmt_dir = captures_dir / key
        if not stmt_dir.exists():
            print(f"  [{key}]  No capture folder at {stmt_dir} — skipping")
            continue

        img_paths = sorted(stmt_dir.glob("page_*.png"))
        if not img_paths:
            print(f"  [{key}]  No PNG files in {stmt_dir} — skipping")
            continue

        print(f"  [{key}]  re-extracting {len(img_paths)} page image(s) …")
        res = process_statement(key, img_paths, client, model, dry_run)
        res["title"] = stmts[key].get("title", key)
        results[key] = res

    if write_outputs:
        json_out = out_dir / f"{company}_results.json"
        json_out.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        html_out = out_dir / f"{company}_results.html"
        html_out.write_text(build_results_html(company, results), encoding="utf-8")

    return results


def run(
    manifest_path:  str | Path,
    captures_dir:   str | Path,
    api_key:        str | None,
    option:         str  = "1",
    model:          str  = "gpt-4o",
    dry_run:        bool = False,
    out_dir:        str | Path | None = None,
) -> dict[str, dict]:
    manifest_path = Path(manifest_path).resolve()
    captures_dir  = Path(captures_dir).resolve()
    manifest      = json.loads(manifest_path.read_text(encoding="utf-8"))

    company = manifest.get("company", "company")
    stmts   = manifest.get("statements", {})

    if out_dir is None:
        out_dir = DEFAULT_OUTPUT_ROOT / company
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Which keys to process
    keys = CORE_KEYS if option == "1" else STMT_ORDER

    print(f"\n{'='*60}")
    print(f"  STEP 3 — Sending images to OpenAI")
    print(f"  Company : {company}")
    print(f"  Option  : {option}  ({'Core only' if option=='1' else 'Core + Notes'})")
    print(f"  Model   : {model}")
    print(f"  Dry-run : {dry_run}")
    print(f"{'='*60}\n")

    client = None
    if not dry_run:
        if not api_key:
            print("ERROR: --apikey is required unless --dry-run is set.")
            sys.exit(1)
        client = OpenAI(api_key=api_key)

    results: dict[str, dict] = {}

    for key in keys:
        if key not in stmts:
            print(f"  [{key}]  NOT IN MANIFEST — skipping")
            continue

        # Find captured images for this statement
        stmt_dir = captures_dir / key
        if not stmt_dir.exists():
            print(f"  [{key}]  No capture folder found at {stmt_dir} — skipping")
            print(f"           Run step2_capture_pages.py first.")
            continue

        img_paths = sorted(stmt_dir.glob("page_*.png"))
        if not img_paths:
            print(f"  [{key}]  No PNG files in {stmt_dir} — skipping")
            continue

        res = process_statement(key, img_paths, client, model, dry_run)
        # Store title from manifest
        res["title"] = stmts[key].get("title", key)
        results[key] = res

    json_out = out_dir / f"{company}_results.json"
    json_out.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n  [save] Results JSON -> {json_out}")

    html_out = out_dir / f"{company}_results.html"
    html_out.write_text(build_results_html(company, results), encoding="utf-8")
    print(f"  [save] Results HTML -> {html_out}")

    meta_out = out_dir / "extraction_meta.json"
    meta_out.write_text(
        json.dumps({
            "company":       company,
            "manifest_path": str(manifest_path),
            "captures_dir":  str(captures_dir),
            "model":         model,
            "option":        option,
            "dry_run":       dry_run,
            "generated_at":  datetime.now().isoformat(timespec="seconds"),
            "statements":    sorted(results.keys()),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  [save] Meta         -> {meta_out}")
    print(f"\n  Done. Open {html_out} to review extracted data.")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="STEP 3 — Send captured images to OpenAI GPT-4o and save results."
    )
    ap.add_argument("manifest",     type=Path,
                    help="JSON manifest from step1_find_pages.py")
    ap.add_argument("--captures",   type=Path, required=True,
                    help="Folder of captured images from step2_capture_pages.py")
    ap.add_argument("--apikey",     default=None,
                    help="OpenAI API key  (or set OPENAI_API_KEY env variable)")
    ap.add_argument("--option",     choices=["1","2"], default="1",
                    help="1=core statements only  2=core+Notes  (default: 1)")
    ap.add_argument("--model",      default="gpt-4o",
                    help="OpenAI model  (default: gpt-4o)")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Print what would be sent — no API calls")
    ap.add_argument("--out",        type=Path, default=None,
                    help="Output folder for results  "
                         f"(default: {DEFAULT_OUTPUT_ROOT}\\<company>\\)")
    args = ap.parse_args()

    import os
    api_key = args.apikey or os.environ.get("OPENAI_API_KEY")

    run(
        manifest_path = args.manifest,
        captures_dir  = args.captures,
        api_key       = api_key,
        option        = args.option,
        model         = args.model,
        dry_run       = args.dry_run,
        out_dir       = args.out,
    )