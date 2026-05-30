"""
non_financial_script.py
=======================
Read a company's annual report PDF, identify the NON-FINANCIAL sections
(About Us, Chairman's Message, Management Discussion & Analysis, Corporate
Governance, Risk Management, ...) and send a curated digest to OpenAI to
produce:

  1. A concise description of the company built from the non-financial
     content of the report.
  2. A breakdown of each non-financial section that was found, with key
     highlights.
  3. A "current real-world scenarios / news" analysis that maps the
     company's outlook and operations against macro and sector trends the
     model is aware of, and explains how those trends may affect the
     company's financial performance.

Progress and the final result are emitted to STDOUT as one JSON object per
line (NDJSON) so a parent process (the Next.js API route) can stream live
status to the UI.

Usage
-----
    python non_financial_script.py \
        --company "COLOMBO LAND AND DEVELOPMENT COMPANY PLC" \
        --pdf "newly_uploaded_report/COLOMBO LAND AND DEVELOPMENT COMPANY PLC/Annual/51341_2025.pdf"

    # The PDF can also be discovered automatically from the company name
    python non_financial_script.py --company "COLOMBO LAND AND DEVELOPMENT COMPANY PLC"

    # Skip the OpenAI call (just emit section detection)
    python non_financial_script.py --company "..." --dry-run

Output layout (when not a dry-run):
    Extracted_json/<company>/non_financial/non_financial_result.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import pdfplumber  # type: ignore
except ImportError:
    print(
        json.dumps(
            {
                "type": "error",
                "message": "pdfplumber is required. Run: pip install pdfplumber",
            }
        ),
        flush=True,
    )
    sys.exit(1)

try:
    from openai import OpenAI  # type: ignore
except ImportError:
    OpenAI = None  # type: ignore[assignment]


SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
BACKEND_ENV = BACKEND_DIR / ".env"
DEFAULT_OUTPUT_ROOT = BACKEND_DIR / "Extracted_json"
DEFAULT_REPORT_DIRS = [
    BACKEND_DIR / "newly_uploaded_report",
    BACKEND_DIR / "reports",
]


# ─────────────────────────────────────────────────────────────────────────────
# Non-financial section definitions
# ─────────────────────────────────────────────────────────────────────────────
#
# Each tuple is (key, human title, heading_regex, [stop_keywords]).
# The heading_regex is used to identify the START of a section on a page;
# we match against the first few lines of every page.
# ─────────────────────────────────────────────────────────────────────────────

NON_FINANCIAL_SECTIONS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "about_us",
        "About Us / Who We Are / About the Group",
        re.compile(
            r"\b(?:about\s+us|who\s+we\s+are|about\s+the\s+group|"
            r"company\s+(?:overview|profile)|our\s+story|"
            r"about\s+(?:this\s+report|the\s+report|the\s+company))\b",
            re.I,
        ),
    ),
    (
        "financial_highlights",
        "Financial Highlights",
        re.compile(
            r"\b(?:financial|performance)\s+highlights?\b|"
            r"\bgroup\s+at\s+a\s+glance\b",
            re.I,
        ),
    ),
    (
        "chairmans_message",
        "Chairman's Message / Letter / Review",
        re.compile(
            r"\bchairman(?:'s|s|\u2019s)?\s+"
            r"(?:message|letter|review|statement|report)\b",
            re.I,
        ),
    ),
    (
        "ceo_message",
        "CEO / Managing Director's Message",
        re.compile(
            r"\b(?:chief\s+executive\s+officer|ceo|managing\s+director|md)(?:'s|s|\u2019s)?\s+"
            r"(?:message|letter|review|statement|report)\b",
            re.I,
        ),
    ),
    (
        "mda",
        "Management Discussion and Analysis",
        re.compile(
            r"management\s+discussion\s+(?:and|&)\s+analysis|"
            r"\bmd&a\b|\bmda\b",
            re.I,
        ),
    ),
    (
        "board_of_directors",
        "Board of Directors / Board Profiles",
        re.compile(
            r"\b(?:board\s+of\s+directors|board\s+profiles?|"
            r"directors[''\u2019]?\s+profiles?|profiles?\s+of\s+(?:the\s+)?directors?)\b",
            re.I,
        ),
    ),
    (
        "corporate_governance",
        "Corporate Governance Report",
        re.compile(
            r"corporate\s+governance(?:\s+(?:report|statement|review))?",
            re.I,
        ),
    ),
    (
        "annual_report_board",
        "Annual Report of the Board on Affairs of the Company",
        re.compile(
            r"(?:annual\s+report|report)\s+of\s+the\s+board(?:\s+of\s+directors)?\s+"
            r"on\s+(?:the\s+)?affairs\s+of\s+the\s+company",
            re.I,
        ),
    ),
    (
        "directors_responsibilities",
        "Statement of Directors' Responsibilities",
        re.compile(
            r"statement\s+of\s+(?:the\s+)?directors[''\u2019s]*\s+responsibilit",
            re.I,
        ),
    ),
    (
        "audit_committee",
        "Audit Committee Report",
        re.compile(r"\baudit\s+committee\s+report\b", re.I),
    ),
    (
        "remuneration_committee",
        "Remuneration / HR Committee Report",
        re.compile(
            r"(?:remuneration|human\s+resources?|hr)(?:\s+(?:and|&)\s+nominations?)?"
            r"\s+committee\s+report",
            re.I,
        ),
    ),
    (
        "nominations_committee",
        "Nominations & Governance Committee Report",
        re.compile(
            r"nominations?\s*(?:&|and)?\s*(?:governance\s+)?committee\s+report",
            re.I,
        ),
    ),
    (
        "related_party_committee",
        "Related Party Transactions Review Committee Report",
        re.compile(
            r"related\s+party\s+transactions?\s+review\s+committee\s+report",
            re.I,
        ),
    ),
    (
        "risk_management",
        "Risk Management Report",
        re.compile(r"\brisk\s+management(?:\s+(?:report|review|framework))?\b", re.I),
    ),
    (
        "investor_information",
        "Investor Information / Investor Relations / Share Information",
        re.compile(
            r"(?:investor\s+(?:information|relations)|share(?:holder)?s?\s+information)",
            re.I,
        ),
    ),
    (
        "statistical_summary",
        "Ten Year / Five Year Statistical Summary",
        re.compile(
            r"(?:ten[\s\-]year|10[\s\-]year|five[\s\-]year|5[\s\-]year)\s+"
            r"(?:summary|highlights?|statistical|achievements?|financial)|"
            r"\bdecade\s+at\s+a\s+glance\b",
            re.I,
        ),
    ),
    (
        "notice_agm",
        "Notice of Annual General Meeting",
        re.compile(r"notice\s+of\s+(?:the\s+)?annual\s+general\s+meeting", re.I),
    ),
    (
        "form_of_proxy",
        "Form of Proxy",
        re.compile(r"\bform\s+of\s+proxy\b", re.I),
    ),
    (
        "corporate_information",
        "Corporate Information",
        re.compile(r"\bcorporate\s+information\b", re.I),
    ),
]

# Only these sections are extracted and sent to OpenAI. Everything else
# (chairman / CEO letters, board profiles, committee reports, corporate
# directory, AGM notices, investor contact blocks, etc.) is recognised only
# so page boundaries stay accurate.
ANALYSIS_SECTION_KEYS: frozenset[str] = frozenset(
    {
        "about_us",
        "financial_highlights",
        "mda",
        "risk_management",
        "statistical_summary",
    }
)

# Headings that, when found at the top of a page, mark the END of the
# previous non-financial section so we stop accumulating its text.
TERMINATOR_REGEX = re.compile(
    r"^\s*("
    r"income\s+statement|"
    r"statement\s+of\s+(?:profit|comprehensive|financial\s+position|"
    r"changes\s+in\s+equity|cash\s+flows?)|"
    r"balance\s+sheet|"
    r"notes\s+to\s+the\s+(?:consolidated\s+)?financial\s+statements?|"
    r"independent\s+auditor[''\u2019s]*\s+report|"
    r"auditors[''\u2019s]*\s+report"
    r")",
    re.I,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def emit(obj: dict[str, Any]) -> None:
    """Write a single NDJSON record to stdout and flush immediately."""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def emit_log(level: str, message: str) -> None:
    emit({"type": "log", "level": level, "message": message})


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env[k] = v
    except Exception:
        pass
    return env


def resolve_api_key(cli_key: str | None) -> str | None:
    if cli_key:
        return cli_key.strip() or None
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"].strip() or None
    env = _load_env_file(BACKEND_ENV)
    val = env.get("OPENAI_API_KEY", "").strip()
    return val or None


def resolve_model(cli_model: str | None) -> str:
    if cli_model and cli_model.strip():
        return cli_model.strip()
    if os.environ.get("OPENAI_CHAT_MODEL"):
        return os.environ["OPENAI_CHAT_MODEL"].strip()
    env = _load_env_file(BACKEND_ENV)
    val = env.get("OPENAI_CHAT_MODEL", "").strip()
    return val or "gpt-4o-mini"


def safe_company_slug(name: str) -> str:
    """Folder-safe name for the output directory."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\- ]+", "_", name)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._-")
    return cleaned or "company"


def find_pdf_for_company(company: str, override: Path | None = None) -> Path | None:
    """Find the most-recently-modified annual PDF for a company name."""
    if override:
        if override.exists() and override.is_file():
            return override
        return None

    candidates: list[Path] = []
    company_lower = company.lower().strip()

    for root in DEFAULT_REPORT_DIRS:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.lower() == company_lower:
                # Prefer Annual subfolder, but fall back to anything.
                for sub_name in ("Annual", "annual"):
                    annual = entry / sub_name
                    if annual.exists():
                        candidates.extend(p for p in annual.glob("*.pdf"))
                if not candidates:
                    for sub in entry.iterdir():
                        if sub.is_dir():
                            candidates.extend(p for p in sub.glob("*.pdf"))
                        elif sub.is_file() and sub.suffix.lower() == ".pdf":
                            candidates.append(sub)
                break

    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# ─────────────────────────────────────────────────────────────────────────────
# PDF parsing + section detection
# ─────────────────────────────────────────────────────────────────────────────


def extract_pdf_pages(pdf_path: Path) -> list[tuple[int, str]]:
    """Return (page_number_1_indexed, plain_text) for every page in the PDF."""
    pages: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                emit_log("warning", f"Failed to extract page {i}: {exc}")
                text = ""
            pages.append((i, text))
            if i == 1 or i == total or i % 20 == 0:
                emit(
                    {
                        "type": "progress",
                        "stage": "extract_text",
                        "current": i,
                        "total": total,
                        "message": f"Extracted text from page {i}/{total}",
                    }
                )
    return pages


def detect_sections(
    pages: list[tuple[int, str]],
    max_pages_per_section: int = 6,
    max_chars_per_section: int = 6000,
) -> list[dict[str, Any]]:
    """Detect non-financial sections by scanning the first lines of each page."""

    page_assignments: list[str | None] = [None] * len(pages)

    def heading_from(text: str) -> str:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        return " | ".join(lines[:5])[:600]

    section_starts: list[tuple[int, str, str]] = []  # (page_index, key, title)
    for idx, (_, text) in enumerate(pages):
        head = heading_from(text)
        if not head:
            continue
        for key, title, regex in NON_FINANCIAL_SECTIONS:
            if regex.search(head):
                section_starts.append((idx, key, title))
                break

    found_keys: set[str] = set()
    sections: list[dict[str, Any]] = []

    for order, (start_idx, key, title) in enumerate(section_starts):
        if key not in ANALYSIS_SECTION_KEYS:
            continue
        if key in found_keys:
            continue
        found_keys.add(key)

        next_start_idx = len(pages)
        for other_idx, other_key, _ in section_starts:
            if other_key == key:
                continue
            if other_idx > start_idx and other_idx < next_start_idx:
                next_start_idx = other_idx

        end_idx = min(
            start_idx + max_pages_per_section, next_start_idx, len(pages)
        )

        collected: list[str] = []
        captured_pages: list[int] = []
        char_count = 0
        for idx in range(start_idx, end_idx):
            page_num, text = pages[idx]
            if idx > start_idx:
                head = heading_from(text)
                if head and TERMINATOR_REGEX.search(head):
                    break
                if head:
                    matched_other = False
                    for other_key, _, other_regex in NON_FINANCIAL_SECTIONS:
                        if other_key == key:
                            continue
                        if other_regex.search(head):
                            matched_other = True
                            break
                    if matched_other:
                        break

            if not text.strip():
                continue
            collected.append(f"--- Page {page_num} ---\n{text.strip()}")
            captured_pages.append(page_num)
            page_assignments[idx] = key
            char_count += len(text)
            if char_count >= max_chars_per_section:
                break

        if not collected:
            continue

        joined = "\n\n".join(collected)
        joined = scrub_section_text(joined)
        if not joined.strip():
            continue
        if len(joined) > max_chars_per_section:
            joined = joined[:max_chars_per_section] + "\n…[truncated]"

        sections.append(
            {
                "key": key,
                "title": title,
                "pages": captured_pages,
                "start_page": pages[start_idx][0],
                "text": joined,
            }
        )

    sections.sort(key=lambda s: s["start_page"])
    return sections


# ─────────────────────────────────────────────────────────────────────────────
# Text scrubbing — strip generic / boilerplate lines before sending to OpenAI
# ─────────────────────────────────────────────────────────────────────────────

# Lines matching ANY of these patterns are removed from a section's text.
# We strip anything that names individual people, counts staff/branches, or
# repeats governance / contact / vision-mission boilerplate.
_SCRUB_LINE_PATTERNS: list[re.Pattern[str]] = [
    # Personal salutations and director-style honorifics
    re.compile(
        r"\b(?:mr\.?|mrs\.?|ms\.?|miss|dr\.?|prof\.?|hon\.?|deshabandu|"
        r"deshamanya|justice|rev\.?|fr\.?|sir|dato|tan\s+sri|"
        r"chairman|chairperson|chairwoman|managing\s+director|"
        r"chief\s+executive(?:\s+officer)?|ceo|coo|cfo|cmo|cto|"
        r"executive\s+director|non[\s\-]?executive\s+director|"
        r"independent\s+director|alternate\s+director|"
        r"director(?:'s|s|\u2019s)?|"
        r"company\s+secretary|secretary\s+to\s+the\s+board)\b",
        re.I,
    ),
    # Employee / staff / headcount references
    re.compile(
        r"\b(?:employees?|staff|workforce|team\s+members|head[\s\-]?count|"
        r"associates|cadre)\b.{0,40}\b\d[\d,]*\b",
        re.I,
    ),
    re.compile(
        r"\b\d[\d,]*\s*(?:\+|plus)?\s*"
        r"(?:employees?|staff|workforce|team\s+members|associates|cadre)\b",
        re.I,
    ),
    # Branch / outlet / agent counts
    re.compile(
        r"\b\d[\d,]*\s*(?:\+|plus)?\s*"
        r"(?:branches|branch\s+network|outlets|service\s+centres?|"
        r"service\s+centers?|agents|agencies|atms|"
        r"customer\s+centres?|customer\s+centers?|"
        r"touch\s*points|points\s+of\s+presence)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:branches|branch\s+network|outlets|service\s+centres?|"
        r"service\s+centers?|agents|agencies|atms)\b.{0,40}\b\d[\d,]*\b",
        re.I,
    ),
    # Registered office, contact, AGM, auditor boilerplate
    re.compile(
        r"\b(?:registered\s+office|head\s+office|principal\s+place\s+of\s+business|"
        r"registrars?|auditors?|bankers?|legal\s+advisors?|"
        r"stock\s+exchange\s+listing|stock\s+code|ticker\s+symbol|"
        r"company\s+registration|tax\s+payer\s+identification|vat\s+registration|"
        r"telephone|fax|website|e[\s\-]?mail|email\s+address)\b",
        re.I,
    ),
    # Vision / mission / values slogans (often a single line)
    re.compile(
        r"^\s*(?:our\s+)?(?:vision|mission|values|purpose|tagline|motto)\s*[:\-]\s*",
        re.I,
    ),
    # Board / committee / governance roll-calls
    re.compile(
        r"\b(?:board\s+(?:meeting|composition|attendance)|"
        r"audit\s+committee|remuneration\s+committee|nominations?\s+committee|"
        r"related\s+party\s+transactions?\s+review\s+committee|"
        r"governance\s+structure|attendance\s+at\s+meetings|"
        r"directors[''\u2019s]*\s+interests?\s+in\s+contracts)\b",
        re.I,
    ),
]


def scrub_section_text(text: str) -> str:
    """Drop lines that look like director names, headcount, branch counts,
    governance / contact boilerplate. Returns text with the same structure
    minus the offending lines.
    """
    if not text:
        return text
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            kept.append(line)
            continue
        # Skip page markers from being filtered
        if line.lstrip().startswith("--- Page"):
            kept.append(line)
            continue
        if any(p.search(line) for p in _SCRUB_LINE_PATTERNS):
            continue
        kept.append(line)
    # Collapse runs of blank lines that the filtering can create
    out_lines: list[str] = []
    prev_blank = False
    for line in kept:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        out_lines.append(line)
        prev_blank = is_blank
    return "\n".join(out_lines).strip()


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI call
# ─────────────────────────────────────────────────────────────────────────────


SYSTEM_PROMPT = """\
You are a senior equity research analyst writing a company-specific
investment briefing. The input has already been filtered to keep only the
substantive business sections (About Us / company profile, Financial
Highlights, MD&A, Risk Management, multi-year statistical summary). All
references to individual people, branch/employee counts and governance
boilerplate have been scrubbed.

================================================================
ABSOLUTE EXCLUSIONS — these MUST NOT appear anywhere in your output
================================================================
You are FORBIDDEN from writing about, naming, counting, or even alluding to:

- ANY individual person (directors, chairman, CEO, MD, executives,
  company secretary, auditors, advisors). Never write a person's name or
  honorific. Never describe "led by", "headed by", "chaired by", etc.
- Board of Directors composition, board profiles, board attendance,
  board committee structure or membership.
- Audit / Remuneration / Nominations / Related Party / HR / any other
  committee, its report, its members, or its activities.
- Corporate Governance compliance, code-of-conduct statements,
  "Statement of Directors' Responsibilities", or any governance checklist.
- Employee headcount, number of staff / workforce / team members /
  associates / cadre — whether absolute or as a ratio.
- Number of branches, outlets, service centres, agents, agencies, ATMs,
  customer centres, or other physical-footprint counts. The fact that the
  company HAS branches is fine; the COUNT is not.
- Registered office, head office, addresses, telephone, fax, e-mail,
  website, registrars, auditors, bankers, legal advisors, stock code.
- Generic vision / mission / values / purpose slogans.
- AGM notices, proxy forms, dividend declarations, shareholder contact
  blocks, annual report production credits.
- "Sustainability award", "ISO certification", and similar self-praise
  unless it directly explains a competitive advantage.

If you find yourself reaching for any of the above, REPHRASE the point to
focus on the underlying business activity instead, or DROP the point.

================================================================
WHAT TO INCLUDE — company-unique business substance ONLY
================================================================
- Core business model: products, services, segments, revenue drivers
- Markets served, customer profile, sectoral/geographic exposure
- Competitive positioning and what differentiates this company
- Strategic initiatives, portfolio shifts, digital transformation,
  new product lines — described as actions, not as people decisions
- Operating performance from MD&A (segment growth, margins, asset
  quality, funding mix) — quantitative where possible
- Company-specific risk exposures (credit, market, liquidity, regulatory,
  concentration, technology, sector-cycle) — NOT generic compliance text
- Distinguishing multi-year trends from the statistical summary
- Forward-looking strategy and outlook

================================================================
TASKS
================================================================
1. company_overview: 2-4 sentences describing what this company DOES and
   how it MAKES MONEY. No people, no counts, no boilerplate.

2. section_summaries: For each section in the input, a short summary
   (2-3 sentences) and 3-5 highlight bullets. Every sentence and every
   bullet must describe a business activity, strategy, performance metric,
   product, segment, market, or risk. Re-read each bullet before
   emitting — if it mentions a person, a headcount, a branch count, a
   governance committee or a generic slogan, REWRITE it or drop it.

3. real_world_analysis: Using your knowledge of macro-economics, sector
   trends, geopolitics and Sri Lankan market conditions (CSE-listed
   issuer), identify 4-6 external factors that are likely to materially
   affect THIS company's near-term financial performance GIVEN ITS ACTUAL
   BUSINESS MODEL above. Each factor: short title, clear explanation
   tied to the company's operations, and an impact tag
   ("positive", "negative", "mixed", or "neutral").

4. overall_conclusion: One paragraph tying the company's distinctive
   business position to the external environment. Same exclusions apply.

Be precise. Do NOT invent facts. Omit a section if nothing substantive
remains after filtering.

Return ONLY a JSON object with this exact shape (no markdown fences, no
commentary):

{
  "company_overview": "<paragraph>",
  "section_summaries": [
    {
      "key": "<section key from the input>",
      "title": "<section title>",
      "summary": "<paragraph>",
      "highlights": ["<bullet 1>", "<bullet 2>", "..."]
    }
  ],
  "real_world_analysis": {
    "summary": "<paragraph>",
    "points": [
      {
        "title": "<short title>",
        "explanation": "<clear explanation>",
        "impact": "positive|negative|mixed|neutral"
      }
    ]
  },
  "overall_conclusion": "<paragraph>"
}
""".strip()


def build_user_prompt(company: str, sections: list[dict[str, Any]]) -> str:
    parts: list[str] = [
        f"Company: {company}",
        f"Sections extracted from the annual report (verbatim, may be noisy "
        f"because they come from PDF OCR/text extraction):",
        "",
    ]
    for sec in sections:
        parts.append(
            f"### [{sec['key']}] {sec['title']} (pages "
            f"{', '.join(str(p) for p in sec['pages']) or '-'})"
        )
        parts.append(sec["text"])
        parts.append("")
    parts.append(
        "Now produce the JSON briefing exactly as specified in the system "
        "instructions."
    )
    return "\n".join(parts)


def call_openai(
    company: str,
    sections: list[dict[str, Any]],
    api_key: str,
    model: str,
) -> dict[str, Any]:
    if OpenAI is None:
        raise RuntimeError(
            "The openai python package is not installed. Run: pip install openai"
        )

    client = OpenAI(api_key=api_key)
    user_prompt = build_user_prompt(company, sections)

    emit(
        {
            "type": "progress",
            "stage": "openai_call",
            "message": f"Sending {len(sections)} section(s) to {model}…",
        }
    )

    started = time.time()
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    elapsed = time.time() - started
    emit_log("info", f"OpenAI response received in {elapsed:.1f}s")

    raw = (completion.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"OpenAI returned non-JSON output: {exc}\n--- raw ---\n{raw[:2000]}"
        ) from exc

    usage = getattr(completion, "usage", None)
    if usage is not None:
        data["_usage"] = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    data["_model"] = model
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Non-financial section analyser for CSE annual reports.",
    )
    ap.add_argument(
        "--company",
        required=True,
        help="Company name (used to locate the PDF and label the output).",
    )
    ap.add_argument(
        "--pdf",
        type=Path,
        default=None,
        help=(
            "Explicit path to the company's annual report PDF. If omitted "
            "the script searches newly_uploaded_report/ and reports/ for a "
            "matching folder."
        ),
    )
    ap.add_argument(
        "--model",
        default=None,
        help="OpenAI model id (default: OPENAI_CHAT_MODEL or gpt-4o-mini).",
    )
    ap.add_argument(
        "--apikey",
        default=None,
        help="OpenAI API key (else OPENAI_API_KEY env or backend/.env).",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: Extracted_json/<company>/non_financial).",
    )
    ap.add_argument(
        "--max-pages-per-section",
        type=int,
        default=6,
        help="Cap how many consecutive pages a section can extend over.",
    )
    ap.add_argument(
        "--max-chars-per-section",
        type=int,
        default=6000,
        help="Cap how many characters per section get sent to OpenAI.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect sections and save the digest but skip the OpenAI call.",
    )
    return ap.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    company: str = args.company.strip()

    emit(
        {
            "type": "start",
            "company": company,
            "pdf": str(args.pdf) if args.pdf else None,
            "model": resolve_model(args.model),
            "dry_run": bool(args.dry_run),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    pdf_path = find_pdf_for_company(company, args.pdf)
    if not pdf_path:
        emit(
            {
                "type": "error",
                "message": (
                    f"No PDF found for company '{company}'. "
                    "Provide --pdf or place the report under "
                    "backend/newly_uploaded_report/<COMPANY>/Annual/*.pdf "
                    "or backend/reports/<COMPANY>/Annual/*.pdf."
                ),
            }
        )
        emit({"type": "done", "code": 2})
        return 2

    emit_log("info", f"Using PDF: {pdf_path}")

    pages = extract_pdf_pages(pdf_path)
    emit_log("info", f"Read {len(pages)} pages from the PDF.")

    sections = detect_sections(
        pages,
        max_pages_per_section=args.max_pages_per_section,
        max_chars_per_section=args.max_chars_per_section,
    )

    if not sections:
        emit(
            {
                "type": "warning",
                "message": (
                    "No non-financial sections were detected. The PDF may be "
                    "image-only (scanned) and require OCR before this script "
                    "can identify section headings."
                ),
            }
        )

    for sec in sections:
        emit(
            {
                "type": "section_found",
                "key": sec["key"],
                "title": sec["title"],
                "pages": sec["pages"],
                "char_count": len(sec["text"]),
            }
        )

    emit_log(
        "info",
        f"Detected {len(sections)} company-specific section(s) for analysis "
        "(chairman/CEO letters, board profiles, governance & directory sections excluded).",
    )

    out_dir = (
        args.out_dir
        if args.out_dir is not None
        else DEFAULT_OUTPUT_ROOT / safe_company_slug(company) / "non_financial"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    digest_path = out_dir / "non_financial_digest.json"
    try:
        digest_path.write_text(
            json.dumps(
                {
                    "company": company,
                    "pdf": str(pdf_path),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "sections": [
                        {
                            "key": s["key"],
                            "title": s["title"],
                            "pages": s["pages"],
                            "text": s["text"],
                        }
                        for s in sections
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        emit_log("info", f"Saved section digest -> {digest_path}")
    except Exception as exc:
        emit_log("warning", f"Could not save digest: {exc}")

    result_payload: dict[str, Any] = {
        "company": company,
        "pdf": str(pdf_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": resolve_model(args.model),
        "sections_found": [
            {"key": s["key"], "title": s["title"], "pages": s["pages"]}
            for s in sections
        ],
    }

    if args.dry_run:
        result_payload["company_overview"] = (
            "[dry-run] Skipped OpenAI call. The detected non-financial "
            "sections are listed above."
        )
        result_payload["section_summaries"] = []
        result_payload["real_world_analysis"] = {"summary": "", "points": []}
        result_payload["overall_conclusion"] = ""
        emit({"type": "result", "data": result_payload})
        emit({"type": "done", "code": 0})
        return 0

    if not sections:
        emit(
            {
                "type": "error",
                "message": (
                    "No content to send to OpenAI – aborting before the API "
                    "call. Try a different PDF or pre-OCR the report."
                ),
            }
        )
        emit({"type": "done", "code": 3})
        return 3

    api_key = resolve_api_key(args.apikey)
    if not api_key:
        emit(
            {
                "type": "error",
                "message": (
                    "OpenAI API key not found. Pass --apikey, set "
                    "OPENAI_API_KEY, or add it to backend/.env."
                ),
            }
        )
        emit({"type": "done", "code": 4})
        return 4

    model = resolve_model(args.model)

    try:
        openai_payload = call_openai(company, sections, api_key, model)
    except Exception as exc:
        emit(
            {
                "type": "error",
                "message": f"OpenAI call failed: {exc}",
            }
        )
        emit({"type": "done", "code": 5})
        return 5

    result_payload["company_overview"] = openai_payload.get("company_overview", "")
    result_payload["section_summaries"] = openai_payload.get(
        "section_summaries", []
    )
    result_payload["real_world_analysis"] = openai_payload.get(
        "real_world_analysis", {"summary": "", "points": []}
    )
    result_payload["overall_conclusion"] = openai_payload.get(
        "overall_conclusion", ""
    )
    if "_usage" in openai_payload:
        result_payload["usage"] = openai_payload["_usage"]

    result_path = out_dir / "non_financial_result.json"
    try:
        result_path.write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        emit_log("info", f"Saved result -> {result_path}")
    except Exception as exc:
        emit_log("warning", f"Could not save result file: {exc}")

    emit({"type": "result", "data": result_payload})
    emit({"type": "done", "code": 0})
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        emit({"type": "error", "message": "Interrupted by user."})
        emit({"type": "done", "code": 130})
        rc = 130
    except Exception as exc:
        emit({"type": "error", "message": f"Unhandled exception: {exc}"})
        emit({"type": "done", "code": 1})
        rc = 1
    sys.exit(rc)
