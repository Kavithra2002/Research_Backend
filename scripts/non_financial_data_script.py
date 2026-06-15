"""
non_financial_data_script.py
============================
Extract the STRUCTURED **non-financial data points** that companies report in
their annual reports (employee counts, gender diversity, GHG emissions, branch
network, subsidiaries, awards / certifications, ESG metrics, CSR spend, board
& governance facts, materiality, …) and return them as a clean JSON document
that can be stored in MongoDB company-wise.

This is the data-capture sibling of ``non_financial_script.py`` (which produces
a qualitative narrative briefing).  Here we instead pull the *measurable /
listable facts* organised under a fixed taxonomy so they can be persisted as
tables and compared across companies and years.

Taxonomy (10 groups → ~45 data points) mirrors the "Common non-financial data
in annual reports" checklist:

    1.  Company identity & overview
    2.  Group & corporate structure
    3.  People & workforce
    4.  Network & distribution
    5.  Awards, recognition & certifications
    6.  Environment & sustainability
    7.  Community & social responsibility
    8.  Leadership & governance
    9.  Stakeholder engagement & materiality
    10. Non-financial performance highlights

Progress and the final result are emitted to STDOUT as one JSON object per line
(NDJSON) so a parent process (the Next.js API route) can stream live status to
the UI.

Usage
-----
    python non_financial_data_script.py --company "AMBEON CAPITAL PLC"
    python non_financial_data_script.py --company "ACL PLASTICS PLC" \
        --pdf "reports/ACL PLASTICS PLC/Annual/xxx.pdf" --upload

Output (when not a dry-run):
    Extracted_json/<company>/non_financial/non_financial_data.json
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

# Reuse the PDF / env / discovery helpers already proven in the narrative
# non-financial script so the two stay consistent.
import non_financial_script as nfs

try:
    from openai import OpenAI  # type: ignore
except ImportError:
    OpenAI = None  # type: ignore[assignment]


SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEFAULT_OUTPUT_ROOT = BACKEND_DIR / "Extracted_json"


# ─────────────────────────────────────────────────────────────────────────────
# Non-financial data taxonomy
# ─────────────────────────────────────────────────────────────────────────────
#
# Each category lists the discrete data points (metrics) we try to capture.
# A metric tuple is (key, human title, short hint of what to capture).
# ─────────────────────────────────────────────────────────────────────────────

NF_TAXONOMY: list[dict[str, Any]] = [
    {
        "key": "company_identity",
        "title": "Company identity & overview",
        "metrics": [
            ("about_company", "About the company / who we are",
             "Business description, history, founding year"),
            ("vision_mission", "Vision, mission & purpose",
             "Strategic direction statements"),
            ("core_values", "Core values",
             "e.g. honesty, integrity, accountability"),
            ("company_theme", "Company theme / annual report theme",
             "The annual report tagline / theme for the year"),
            ("company_milestones", "Our story / company milestones",
             "Historical journey or key anniversaries"),
        ],
    },
    {
        "key": "group_structure",
        "title": "Group & corporate structure",
        "metrics": [
            ("group_structure_diagram", "Group structure",
             "Parent, subsidiaries, associates, investees"),
            ("subsidiaries", "List of subsidiaries",
             "Names with ownership percentage and segment"),
            ("business_segments", "Business segments / divisions",
             "e.g. financial services, technology, manufacturing"),
            ("overseas_presence", "Overseas / foreign presence",
             "Countries, representative offices, foreign subsidiaries"),
            ("new_branch_openings", "New branch / subsidiary openings",
             "Expansions during the year"),
        ],
    },
    {
        "key": "people_workforce",
        "title": "People & workforce",
        "metrics": [
            ("total_employees", "Total employee count",
             "Headcount as at year end"),
            ("gender_diversity", "Gender diversity (% male / female)",
             "Board, management and overall workforce breakdown"),
            ("employee_turnover", "Employee turnover rate",
             "Voluntary and involuntary"),
            ("training_hours", "Training hours per employee",
             "Total and average training investment"),
            ("employee_satisfaction", "Employee satisfaction / engagement score",
             "Survey results or index"),
            ("workplace_safety", "Workplace safety record",
             "Injuries, lost-time incidents, zero-harm targets"),
            ("employee_benefits", "Employee benefits & schemes",
             "Share options, welfare programmes"),
        ],
    },
    {
        "key": "network_distribution",
        "title": "Network & distribution",
        "metrics": [
            ("branches_outlets", "Number of branches / outlets",
             "Total domestic network as at year end"),
            ("atms", "ATMs / self-service points",
             "Count and geographic spread (banking)"),
            ("digital_users", "Digital / mobile platform users",
             "App downloads, active users, digital transactions"),
            ("network_coverage", "Network coverage / towers / base stations",
             "Telecom-specific reach metrics"),
            ("customer_count", "Customer / subscriber count",
             "Total served customers or subscribers"),
        ],
    },
    {
        "key": "awards_certifications",
        "title": "Awards, recognition & certifications",
        "metrics": [
            ("awards", "External awards received",
             "Industry, employer, sustainability, branding awards"),
            ("certifications", "ISO / international certifications",
             "ISO 9001, 27001, 14001, AWS, Top Employer, etc."),
            ("credit_ratings", "Credit / brand ratings",
             "Brand Finance, Fitch, Moody's, S&P ratings"),
            ("industry_rankings", "Industry rankings & market position",
             "Most valuable brand, best employer, etc."),
        ],
    },
    {
        "key": "environment_sustainability",
        "title": "Environment & sustainability",
        "metrics": [
            ("ghg_emissions", "GHG emissions (Scope 1, 2 & 3)",
             "Carbon footprint and reduction targets"),
            ("energy_consumption", "Energy consumption & renewable %",
             "Total usage and green energy transition"),
            ("water_usage", "Water usage & conservation",
             "Volume consumed and reduction initiatives"),
            ("waste_management", "Waste management & circularity",
             "Recycling rates, zero-waste targets"),
            ("carbon_neutrality", "Carbon neutrality / net-zero progress",
             "Milestones achieved toward climate goals"),
            ("esg_strategy", "Sustainability framework / ESG strategy",
             "GRI, SASB, SLFRS S1/S2, GSMA alignment"),
        ],
    },
    {
        "key": "community_social",
        "title": "Community & social responsibility",
        "metrics": [
            ("csr_spend", "CSR / community investment spend",
             "Total amount and key projects"),
            ("beneficiaries", "Beneficiaries reached",
             "Number of people / communities impacted"),
            ("financial_inclusion", "Financial inclusion / literacy initiatives",
             "Rural outreach, SME support"),
            ("dei", "DEI (Diversity, Equity & Inclusion) initiatives",
             "Awards, programmes, policies"),
        ],
    },
    {
        "key": "leadership_governance",
        "title": "Leadership & governance",
        "metrics": [
            ("board_profiles", "Board of directors profiles",
             "Number of directors, designations, independence, tenure"),
            ("board_committees", "Board committee reports",
             "Audit, remuneration, nominations, risk, RPT"),
            ("senior_management", "Senior management team",
             "C-suite and business unit heads"),
            ("chairman_message", "Chairman's message",
             "Strategic overview from board leadership"),
            ("ceo_review", "CEO / MD review",
             "Operational and performance commentary"),
            ("governance_framework", "Corporate governance framework",
             "Codes adhered to, compliance declarations"),
        ],
    },
    {
        "key": "stakeholder_materiality",
        "title": "Stakeholder engagement & materiality",
        "metrics": [
            ("stakeholder_engagement", "Stakeholder engagement summary",
             "How each group was engaged and key concerns"),
            ("material_matters", "Material matters / materiality matrix",
             "Issues deemed most important to the business"),
            ("value_creation", "Value creation model / capitals",
             "How resources are converted to outcomes (IR framework)"),
            ("operating_environment", "Operating environment / macro review",
             "Economic context, industry landscape"),
        ],
    },
    {
        "key": "nonfin_performance",
        "title": "Non-financial performance highlights",
        "metrics": [
            ("nonfin_snapshot", "Non-financial highlights snapshot",
             "Key KPIs excluding financial figures"),
            ("strategy_in_action", "Strategy in action / strategic navigator",
             "Progress against strategic objectives"),
            ("risk_framework", "Risk management framework",
             "Key risks, appetite, mitigation measures"),
            ("digital_transformation", "Digital transformation progress",
             "Technology investments, new digital products launched"),
        ],
    },
]


def taxonomy_lookup() -> dict[str, dict[str, Any]]:
    """category_key -> {title, metrics: {metric_key -> (title, hint)}}."""
    out: dict[str, dict[str, Any]] = {}
    for cat in NF_TAXONOMY:
        metrics = {m[0]: (m[1], m[2]) for m in cat["metrics"]}
        out[cat["key"]] = {"title": cat["title"], "metrics": metrics}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Corpus building
# ─────────────────────────────────────────────────────────────────────────────


def build_corpus(pages: list[tuple[int, str]], max_chars: int) -> str:
    """Concatenate page text (with page markers) up to ``max_chars``.

    Annual reports are long; gpt-4o-mini accepts ~128k tokens so a generous
    character budget keeps most of the narrative/disclosure content while still
    fitting comfortably in context.
    """
    parts: list[str] = []
    total = 0
    for page_num, text in pages:
        if not text or not text.strip():
            continue
        block = f"--- Page {page_num} ---\n{text.strip()}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                parts.append(block[:remaining] + "\n…[truncated]")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI prompt + call
# ─────────────────────────────────────────────────────────────────────────────


def _taxonomy_prompt_block() -> str:
    lines: list[str] = []
    for cat in NF_TAXONOMY:
        lines.append(f"\n[{cat['key']}] {cat['title']}")
        for key, title, hint in cat["metrics"]:
            lines.append(f"  - {key}: {title} — {hint}")
    return "\n".join(lines)


SYSTEM_PROMPT = """\
You are a meticulous ESG / annual-report data analyst. You are given the
extracted text of a single company's annual report. Your job is to capture the
company's NON-FINANCIAL data points and organise them under a fixed taxonomy.

NON-FINANCIAL means: facts about the company that are NOT line items from the
financial statements (no revenue / profit / asset numbers). Capture descriptive
facts, counts, percentages, lists, targets and qualitative statements such as
employee headcount, gender split, branch / ATM counts, subsidiaries, awards,
certifications, GHG emissions, energy / water / waste metrics, CSR spend,
governance facts, materiality topics, strategy and digital-transformation
progress.

RULES
- Use ONLY information present in the provided report text. Do NOT invent data.
- For every metric in the taxonomy, decide whether the report contains it.
- If found, set "found": true and fill "value" with the concrete figure / list
  / short factual statement (keep numbers and units exactly as printed), and
  "detail" with a one or two sentence supporting note. Put page numbers you
  relied on (from the "--- Page N ---" markers) in "pages" as an array of ints.
- If NOT found in the report, set "found": false, "value": "", "detail": "",
  "pages": [].
- Be concise. Prefer the most recent reporting year when several are shown.
- Do not include any individual's personal data beyond their role/designation.

OUTPUT
Return ONLY a JSON object (no markdown fences, no commentary) with this shape:

{
  "company_overview": "<2-3 sentence factual description of what the company does>",
  "reporting_year": "<the financial year the report covers, e.g. 2024/25 or null>",
  "categories": [
    {
      "key": "<category key from the taxonomy>",
      "metrics": [
        {
          "key": "<metric key from the taxonomy>",
          "found": true,
          "value": "<concrete value / list / statement>",
          "detail": "<short supporting note>",
          "pages": [<int>, ...]
        }
      ]
    }
  ]
}

You MUST include every category and every metric key from the taxonomy below,
in the same order, even when "found" is false.

TAXONOMY
""".strip() + "\n" + _taxonomy_prompt_block()


def build_user_prompt(company: str, reporting_hint: str, corpus: str) -> str:
    return (
        f"Company: {company}\n"
        f"{('Report context: ' + reporting_hint) if reporting_hint else ''}\n\n"
        "Annual report text (verbatim, extracted from PDF — may be noisy):\n"
        "================================================================\n"
        f"{corpus}\n"
        "================================================================\n\n"
        "Now produce the JSON exactly as specified in the system instructions, "
        "covering every taxonomy category and metric."
    )


def call_openai(
    company: str,
    corpus: str,
    api_key: str,
    model: str,
    reporting_hint: str = "",
) -> dict[str, Any]:
    if OpenAI is None:
        raise RuntimeError(
            "The openai python package is not installed. Run: pip install openai"
        )

    client = OpenAI(api_key=api_key)
    user_prompt = build_user_prompt(company, reporting_hint, corpus)

    nfs.emit(
        {
            "type": "progress",
            "stage": "openai_call",
            "message": f"Sending report corpus ({len(corpus)} chars) to {model}…",
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
        temperature=0.1,
        max_tokens=4096,
    )
    elapsed = time.time() - started
    nfs.emit_log("info", f"OpenAI response received in {elapsed:.1f}s")

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
# Normalisation — coerce the model output into the canonical taxonomy shape
# ─────────────────────────────────────────────────────────────────────────────


def normalise_payload(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Return (categories, found_count, total_count) in canonical order.

    Guarantees every taxonomy category + metric is present even if the model
    omitted some, so the stored document has a stable, comparable shape.
    """
    lookup = taxonomy_lookup()

    # Index the model's output by (category_key, metric_key).
    model_metrics: dict[tuple[str, str], dict[str, Any]] = {}
    for cat in raw.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        ckey = str(cat.get("key") or "").strip()
        for m in cat.get("metrics") or []:
            if not isinstance(m, dict):
                continue
            mkey = str(m.get("key") or "").strip()
            if ckey and mkey:
                model_metrics[(ckey, mkey)] = m

    categories: list[dict[str, Any]] = []
    found_count = 0
    total_count = 0

    for cat in NF_TAXONOMY:
        ckey = cat["key"]
        metrics_out: list[dict[str, Any]] = []
        for mkey, mtitle, mhint in [(m[0], m[1], m[2]) for m in cat["metrics"]]:
            total_count += 1
            src = model_metrics.get((ckey, mkey), {})
            value = str(src.get("value") or "").strip()
            found = bool(src.get("found")) and bool(value)
            if found:
                found_count += 1
            pages = src.get("pages")
            if not isinstance(pages, list):
                pages = []
            pages = [int(p) for p in pages if isinstance(p, (int, float))]
            metrics_out.append(
                {
                    "key": mkey,
                    "title": mtitle,
                    "hint": mhint,
                    "found": found,
                    "value": value if found else "",
                    "detail": str(src.get("detail") or "").strip() if found else "",
                    "pages": pages if found else [],
                }
            )
        categories.append(
            {
                "key": ckey,
                "title": lookup[ckey]["title"],
                "metrics": metrics_out,
            }
        )

    return categories, found_count, total_count


# ─────────────────────────────────────────────────────────────────────────────
# Public extraction entry point (used by the orchestrator)
# ─────────────────────────────────────────────────────────────────────────────


def extract_non_financial_data(
    *,
    company: str,
    pdf_path: Path,
    api_key: str,
    model: str,
    max_chars: int = 200_000,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read the PDF, extract structured non-financial data and return a payload.

    Returns a dict shaped for storage:
        {company, pdf, generated_at, model, reporting_year, company_overview,
         categories:[...], found_count, total_count, usage?}
    """
    pages = nfs.extract_pdf_pages(pdf_path)
    nfs.emit_log("info", f"Read {len(pages)} pages from the PDF.")

    corpus = build_corpus(pages, max_chars)
    if not corpus.strip():
        raise RuntimeError(
            "No extractable text in the PDF (it may be a scanned/image-only "
            "report requiring OCR)."
        )

    payload: dict[str, Any] = {
        "company": company,
        "pdf": str(pdf_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
    }

    if dry_run:
        # Emit the empty taxonomy so the UI can preview the shape.
        categories, found_count, total_count = normalise_payload({"categories": []})
        payload.update(
            {
                "reporting_year": None,
                "company_overview": "[dry-run] OpenAI call skipped.",
                "categories": categories,
                "found_count": 0,
                "total_count": total_count,
            }
        )
        return payload

    raw = call_openai(company, corpus, api_key, model)
    categories, found_count, total_count = normalise_payload(raw)

    payload.update(
        {
            "reporting_year": raw.get("reporting_year"),
            "company_overview": str(raw.get("company_overview") or "").strip(),
            "categories": categories,
            "found_count": found_count,
            "total_count": total_count,
        }
    )
    if "_usage" in raw:
        payload["usage"] = raw["_usage"]
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# CLI (single company)
# ─────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Structured non-financial DATA extractor for CSE annual reports.",
    )
    ap.add_argument("--company", required=True,
                    help="Company name (used to locate the PDF and label output).")
    ap.add_argument("--pdf", type=Path, default=None,
                    help="Explicit path to the annual report PDF.")
    ap.add_argument("--model", default=None,
                    help="OpenAI model id (default: OPENAI_CHAT_MODEL or gpt-4o-mini).")
    ap.add_argument("--apikey", default=None,
                    help="OpenAI API key (else OPENAI_API_KEY env or backend/.env).")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory (default: Extracted_json/<company>/non_financial).")
    ap.add_argument("--max-chars", type=int, default=200_000,
                    help="Cap on report characters sent to OpenAI (default 200000).")
    ap.add_argument("--upload", action="store_true",
                    help="Also upload the extracted data into MongoDB.")
    ap.add_argument("--report-group", default=None,
                    help="Originating report group/year label (for the DB key).")
    ap.add_argument("--mongo-uri", default=None, help="Override MongoDB URI.")
    ap.add_argument("--db-name", default=None, help="Override MongoDB database.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Detect pages but skip the OpenAI call and DB upload.")
    return ap.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    company = args.company.strip()
    model = nfs.resolve_model(args.model)

    nfs.emit(
        {
            "type": "start",
            "company": company,
            "pdf": str(args.pdf) if args.pdf else None,
            "model": model,
            "dry_run": bool(args.dry_run),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    pdf_path = nfs.find_pdf_for_company(company, args.pdf)
    if not pdf_path:
        nfs.emit({"type": "error", "message": (
            f"No PDF found for company '{company}'. Provide --pdf or place the "
            "report under backend/reports/<COMPANY>/Annual/*.pdf.")})
        nfs.emit({"type": "done", "code": 2})
        return 2

    nfs.emit_log("info", f"Using PDF: {pdf_path}")

    api_key = nfs.resolve_api_key(args.apikey)
    if not args.dry_run and not api_key:
        nfs.emit({"type": "error", "message": (
            "OpenAI API key not found. Pass --apikey, set OPENAI_API_KEY, or "
            "add it to backend/.env.")})
        nfs.emit({"type": "done", "code": 4})
        return 4

    try:
        payload = extract_non_financial_data(
            company=company,
            pdf_path=pdf_path,
            api_key=api_key or "",
            model=model,
            max_chars=args.max_chars,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:
        nfs.emit({"type": "error", "message": f"Extraction failed: {exc}"})
        nfs.emit({"type": "done", "code": 5})
        return 5

    nfs.emit({
        "type": "data_extracted",
        "company": company,
        "found": payload.get("found_count", 0),
        "total": payload.get("total_count", 0),
    })

    out_dir = (
        args.out_dir if args.out_dir is not None
        else DEFAULT_OUTPUT_ROOT / nfs.safe_company_slug(company) / "non_financial"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "non_financial_data.json"
    try:
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        nfs.emit_log("info", f"Saved data -> {result_path}")
    except Exception as exc:
        nfs.emit_log("warning", f"Could not save result file: {exc}")

    if args.upload and not args.dry_run:
        try:
            import non_financial_db_uploader as nf_db
            summary = nf_db.upload_non_financial_data(
                payload,
                company_slug=nfs.safe_company_slug(company),
                company_name=company,
                report_group=args.report_group,
                source_pdf=str(pdf_path),
                uri=args.mongo_uri,
                db_name=args.db_name,
            )
            nfs.emit({"type": "db-upload", "company": company, **summary})
        except Exception as exc:
            nfs.emit({"type": "db-upload", "company": company,
                      "status": "error", "error": str(exc)})

    nfs.emit({"type": "result", "data": payload})
    nfs.emit({"type": "done", "code": 0})
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        nfs.emit({"type": "error", "message": "Interrupted by user."})
        nfs.emit({"type": "done", "code": 130})
        rc = 130
    except Exception as exc:
        nfs.emit({"type": "error", "message": f"Unhandled exception: {exc}"})
        nfs.emit({"type": "done", "code": 1})
        rc = 1
    sys.exit(rc)
