"""
financial_agent.py — "Robin", the financial-data analysis agent
================================================================
Robin is a conversational AI agent for **financial data analysis** over the
data that the extraction pipeline has already pulled out of company annual /
quarterly reports and stored in MongoDB (``Research_Project`` →
``financial_tables`` + ``companies``).

How it works
------------
1. The user asks a question in natural language ("What was Janashakthi
   Finance's net interest income in 2025?", "Compare total assets 2024 vs
   2025", or even a normal small-talk question).
2. Robin sends the question to OpenAI (using the SAME ``OPENAI_API_KEY`` from
   ``backend/.env`` that the rest of the project uses).
3. The model decides — via tool/function calling — what data it needs and
   asks Robin to query MongoDB (list companies, see what data exists, pull a
   statement, search line items, …).
4. Robin runs those queries against the DB and feeds the *real* numbers back
   to the model, which then writes a grounded, accurate answer.
5. For ordinary / non-financial questions Robin just answers normally.

Grounding rule
--------------
Robin must never invent financial figures. If the requested data is not in the
database, it says so and OFFERS to look it up in the company's full report (see
the "FUTURE FEATURE" section near the bottom — the hook is already wired in but
the heavy report-reading implementation is intentionally left as a stub for us
to fill in later).

Usage
-----
    # Interactive chat (recommended)
    python scripts/financial_agent.py

    # One-shot question
    python scripts/financial_agent.py --question "What is Janashakthi Finance's total income in 2025?"

    # Show the DB tool calls Robin makes (debugging)
    python scripts/financial_agent.py --verbose

    # Override key / model / mongo
    python scripts/financial_agent.py --apikey sk-... --model gpt-4o
    python scripts/financial_agent.py --mongo-uri mongodb://localhost:27017 --db-name Research_Project

The OpenAI key is read from (in priority order): --apikey flag, OPENAI_API_KEY
env var, then backend/.env.  Mongo config is resolved the same way the rest of
the pipeline does it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Paths / constants
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
BACKEND_ENV = BACKEND_DIR / ".env"

COMPANIES_COLLECTION = "companies"
TABLES_COLLECTION    = "financial_tables"

DEFAULT_MODEL   = "gpt-4o-mini"
MAX_TOOL_ROUNDS = 8          # safety cap on the query/answer loop per turn
MAX_ROWS_RETURNED = 80       # don't blow the context window with huge tables
MAX_MATCHES_RETURNED = 30


# ─────────────────────────────────────────────────────────────────────────────
# .env / config resolution  (same approach as db_uploader.py / Data_retrive.py)
# ─────────────────────────────────────────────────────────────────────────────

def _load_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser (no python-dotenv dependency)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env[k] = v
    except Exception:
        pass
    return env


def resolve_config(
    *,
    api_key: str | None = None,
    model: str | None = None,
    mongo_uri: str | None = None,
    db_name: str | None = None,
) -> dict[str, str | None]:
    """CLI flag -> environment -> backend/.env -> sane default."""
    env = _load_env_file(BACKEND_ENV)
    return {
        "api_key": (
            api_key
            or os.environ.get("OPENAI_API_KEY")
            or env.get("OPENAI_API_KEY")
        ),
        "model": (
            model
            or os.environ.get("OPENAI_CHAT_MODEL")
            or env.get("OPENAI_CHAT_MODEL")
            or DEFAULT_MODEL
        ),
        "mongo_uri": (
            mongo_uri
            or os.environ.get("MONGO_URI")
            or env.get("MONGO_URI")
            or "mongodb://localhost:27017"
        ),
        "db_name": (
            db_name
            or os.environ.get("MONGO_DB_NAME")
            or env.get("MONGO_DB_NAME")
            or "Research_Project"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB data-access layer  (the only place that touches the DB)
# ─────────────────────────────────────────────────────────────────────────────

class FinancialDB:
    """Thin pymongo wrapper exposing the few read queries Robin needs."""

    def __init__(self, uri: str, db_name: str):
        from pymongo import MongoClient  # lazy import so --help works without pymongo

        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.client.admin.command("ping")  # surface connection errors now
        self.db = self.client[db_name]

    # ── company resolution ────────────────────────────────────────────────
    def list_companies(self, query: str | None = None, limit: int = 60) -> list[dict]:
        q: dict[str, Any] = {}
        if query:
            rx = {"$regex": re.escape(query.strip()), "$options": "i"}
            q = {"$or": [{"name": rx}, {"slug": rx}]}
        cur = (
            self.db[COMPANIES_COLLECTION]
            .find(q, {"_id": 0, "slug": 1, "name": 1})
            .sort("name", 1)
            .limit(limit)
        )
        return [{"slug": d.get("slug"), "name": d.get("name")} for d in cur]

    def resolve_company(self, term: str) -> dict:
        """Map a name-or-slug to a single company.

        Returns one of:
          {"slug": ..., "name": ...}                       -> resolved
          {"candidates": [ {slug,name}, ... ]}             -> ambiguous
          {"error": "..."}                                 -> not found
        """
        term = (term or "").strip()
        if not term:
            return {"error": "No company name/slug given."}

        # 1) exact slug
        doc = self.db[COMPANIES_COLLECTION].find_one(
            {"slug": term}, {"_id": 0, "slug": 1, "name": 1}
        )
        if doc:
            return {"slug": doc["slug"], "name": doc.get("name")}

        # 2) exact (case-insensitive) name
        doc = self.db[COMPANIES_COLLECTION].find_one(
            {"name": {"$regex": f"^{re.escape(term)}$", "$options": "i"}},
            {"_id": 0, "slug": 1, "name": 1},
        )
        if doc:
            return {"slug": doc["slug"], "name": doc.get("name")}

        # 3) fuzzy contains
        matches = self.list_companies(term, limit=10)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return {"candidates": matches}

        # 4) fall back to whatever financial_tables knows (data may exist even
        #    if the companies registry is sparse)
        slugs = self.db[TABLES_COLLECTION].distinct(
            "company_slug",
            {"$or": [
                {"company_slug": {"$regex": re.escape(term), "$options": "i"}},
                {"company_name": {"$regex": re.escape(term), "$options": "i"}},
            ]},
        )
        if len(slugs) == 1:
            name = self.db[TABLES_COLLECTION].find_one(
                {"company_slug": slugs[0]}, {"company_name": 1}
            )
            return {"slug": slugs[0], "name": (name or {}).get("company_name")}
        if len(slugs) > 1:
            return {"candidates": [{"slug": s, "name": s} for s in slugs[:10]]}

        return {"error": f"No company found matching '{term}'."}

    # ── what data exists for a company ─────────────────────────────────────
    def company_overview(self, company_slug: str) -> dict:
        pipeline = [
            {"$match": {"company_slug": company_slug}},
            {"$group": {
                "_id": {
                    "report_type": "$report_type",
                    "year": "$year",
                    "quarter": "$quarter",
                },
                "statements": {"$addToSet": "$statement_key"},
            }},
            {"$sort": {"_id.year": -1, "_id.report_type": 1, "_id.quarter": 1}},
        ]
        periods: list[dict] = []
        for g in self.db[TABLES_COLLECTION].aggregate(pipeline):
            key = g["_id"]
            periods.append({
                "report_type": key.get("report_type"),
                "year": key.get("year"),
                "quarter": key.get("quarter"),
                "statements": sorted(s for s in g.get("statements", []) if s),
            })
        name_doc = self.db[TABLES_COLLECTION].find_one(
            {"company_slug": company_slug}, {"company_name": 1}
        )
        return {
            "company_slug": company_slug,
            "company_name": (name_doc or {}).get("company_name", company_slug),
            "period_count": len(periods),
            "periods": periods,
        }

    # ── fetch a specific statement table ───────────────────────────────────
    def get_statement(
        self,
        company_slug: str,
        statement_key: str | None = None,
        year: int | None = None,
        report_type: str | None = None,
        quarter: str | None = None,
    ) -> dict:
        q: dict[str, Any] = {"company_slug": company_slug}
        if statement_key:
            q["$or"] = [
                {"statement_key": {"$regex": re.escape(statement_key), "$options": "i"}},
                {"statement_title": {"$regex": re.escape(statement_key), "$options": "i"}},
            ]
        if year is not None:
            q["year"] = year
        if report_type:
            q["report_type"] = report_type.lower()
        if quarter:
            q["quarter"] = quarter.upper()

        cur = self.db[TABLES_COLLECTION].find(q).sort([("year", -1), ("table_index", 1)])
        tables = [self._slim_table(d) for d in cur]
        if not tables:
            return {
                "found": False,
                "note": "No matching statement/table stored for that company "
                        "with those filters.",
            }
        # Cap so we never dump an enormous payload back into the model.
        return {"found": True, "table_count": len(tables), "tables": tables[:6]}

    # ── keyword search across a company's line items ───────────────────────
    def search_line_items(
        self,
        company_slug: str,
        keyword: str,
        year: int | None = None,
        report_type: str | None = None,
    ) -> dict:
        q: dict[str, Any] = {"company_slug": company_slug}
        if year is not None:
            q["year"] = year
        if report_type:
            q["report_type"] = report_type.lower()

        kw = (keyword or "").strip().lower()
        if not kw:
            return {"error": "keyword is required for search_line_items"}

        matches: list[dict] = []
        for doc in self.db[TABLES_COLLECTION].find(q).sort("year", -1):
            header_rows = doc.get("header_rows") or []
            for row in doc.get("rows") or []:
                cells = row.get("cells") if isinstance(row, dict) else None
                if not isinstance(cells, list) or not cells:
                    continue
                if any(kw in str(c).lower() for c in cells):
                    matches.append({
                        "company_name": doc.get("company_name"),
                        "year": doc.get("year"),
                        "report_type": doc.get("report_type"),
                        "quarter": doc.get("quarter"),
                        "statement_key": doc.get("statement_key"),
                        "statement_title": doc.get("statement_title"),
                        "header_rows": header_rows,
                        "row": cells,
                    })
                    if len(matches) >= MAX_MATCHES_RETURNED:
                        return {
                            "match_count": len(matches),
                            "truncated": True,
                            "matches": matches,
                        }
        return {
            "match_count": len(matches),
            "truncated": False,
            "matches": matches,
        }

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _slim_table(doc: dict) -> dict:
        rows = []
        for r in (doc.get("rows") or [])[:MAX_ROWS_RETURNED]:
            if isinstance(r, dict) and isinstance(r.get("cells"), list):
                rows.append(r["cells"])
        return {
            "company_name": doc.get("company_name"),
            "year": doc.get("year"),
            "report_type": doc.get("report_type"),
            "quarter": doc.get("quarter"),
            "period_label": doc.get("period_label"),
            "statement_key": doc.get("statement_key"),
            "statement_title": doc.get("statement_title"),
            "caption": doc.get("caption"),
            "header_rows": doc.get("header_rows") or [],
            "rows": rows,
            "row_count": doc.get("row_count"),
            "source_pdf": doc.get("source_pdf"),
        }

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema exposed to the model
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_companies",
            "description": (
                "List companies that have financial data in the database. "
                "Use this to discover available companies or to resolve a "
                "company the user named to its exact slug/name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive substring to "
                                       "filter company name or slug.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "company_overview",
            "description": (
                "Show WHAT financial data exists for one company: which years, "
                "report types (annual/quarterly), quarters, and statement keys "
                "are stored. Call this before fetching a statement so you know "
                "what is actually available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Company name or slug.",
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statement",
            "description": (
                "Fetch the actual rows of a financial statement table for a "
                "company. Statement keys look like 'income_statement', 'sofp' "
                "(statement of financial position / balance sheet), 'cash_flow', "
                "'oci', etc. You may also pass a human title fragment. Returns "
                "header rows + data rows verbatim (numbers are kept as printed)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company name or slug."},
                    "statement_key": {
                        "type": "string",
                        "description": "Statement key or title fragment, e.g. "
                                       "'income_statement', 'balance sheet', 'cash flow'.",
                    },
                    "year": {"type": "integer", "description": "Reporting year, e.g. 2025."},
                    "report_type": {
                        "type": "string",
                        "enum": ["annual", "quarterly"],
                        "description": "Filter by report type.",
                    },
                    "quarter": {
                        "type": "string",
                        "description": "Quarter for quarterly reports, e.g. 'Q1'.",
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_line_items",
            "description": (
                "Search inside a company's stored statements for rows whose "
                "cells contain a keyword (e.g. 'net interest income', 'total "
                "assets', 'revenue'). Best tool for pinpointing a single figure. "
                "Returns each matching row with its statement, year and headers "
                "so you can read off the right column."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company name or slug."},
                    "keyword": {
                        "type": "string",
                        "description": "Text to look for inside the statement rows.",
                    },
                    "year": {"type": "integer", "description": "Optional reporting year filter."},
                    "report_type": {
                        "type": "string",
                        "enum": ["annual", "quarterly"],
                        "description": "Optional report type filter.",
                    },
                },
                "required": ["company", "keyword"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refer_to_full_report",
            "description": (
                "Use ONLY when the requested financial data is NOT in the "
                "database AND the user has explicitly agreed to let you look it "
                "up in the company's full report. Do not call this without the "
                "user's confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company name or slug."},
                    "question": {
                        "type": "string",
                        "description": "The user's original financial question.",
                    },
                },
                "required": ["company", "question"],
                "additionalProperties": False,
            },
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# System prompt — Robin's persona + rules
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = "\n".join([
    "You are \"Robin\", a financial data-analysis assistant for the Ambeon Console.",
    "You answer questions about companies' financial data that was extracted from "
    "their annual and quarterly reports and stored in a database.",
    "",
    "How to work:",
    "  - For any question about a company's financial figures, USE THE TOOLS to "
    "    look up real data before answering. Never guess or fabricate numbers.",
    "  - Typical flow: resolve the company (list_companies) -> see what exists "
    "    (company_overview) -> pull the figure (search_line_items or get_statement).",
    "  - Numbers are stored exactly as printed in the report (with commas, and "
    "    brackets for negatives). Report them faithfully; only do arithmetic the "
    "    user asks for, and show your working briefly.",
    "  - Always state which company, year and statement a figure came from.",
    "  - If a company name is ambiguous, ask the user which one they meant.",
    "",
    "When the data is NOT in the database:",
    "  - Do NOT invent it. Say something like: \"Sorry, I don't have that data "
    "    with me right now. Would you like me to answer it by referring to the "
    "    full report of that company?\"",
    "  - Only if the user says yes, call the refer_to_full_report tool.",
    "",
    "Normal / non-financial questions:",
    "  - If the user just chats or asks something general, answer normally and "
    "    conversationally without using the data tools.",
    "",
    "Keep replies clear and concise. Use short tables or bullet points for "
    "multiple figures.",
])


# ─────────────────────────────────────────────────────────────────────────────
# FUTURE FEATURE — "refer to the full report"  (intentional placeholder)
# ─────────────────────────────────────────────────────────────────────────────
#
# IDEA (to implement later):
#   When the user asks a financial question whose answer is NOT in the
#   database, Robin asks for permission and — if granted — reads the company's
#   FULL report to answer, again using this same OpenAI key.
#
# To build this out, fill in `refer_to_full_report()` below. A likely shape:
#   1. Locate the company's source report PDF (see Data_retrive._pick_pdf / the
#      `source_pdf` field already stored on financial_tables, or the
#      reports/<company>/ folder).
#   2. Extract / chunk the relevant text (PyMuPDF is already a dependency; the
#      step1/step2/step3 modules already know how to find + render pages).
#   3. Retrieve the chunks most relevant to `question` (keyword or embeddings).
#   4. Send those chunks + the question to OpenAI and return the answer text.
#   5. Optionally write the new figure back into financial_tables so next time
#      it's served straight from the DB.
#
# Returning a dict with an "answer" string is enough for Robin to relay it.
# Keep the function signature stable so the tool wiring above keeps working.

def refer_to_full_report(
    company_slug: str,
    question: str,
    *,
    config: dict[str, str | None],
) -> dict:
    """Placeholder for the full-report fallback. Not implemented yet.

    Returns a friendly, machine-readable result so Robin degrades gracefully
    until the real report-reading pipeline is built here.
    """
    # TODO: implement the full-report reading pipeline described above.
    return {
        "implemented": False,
        "company_slug": company_slug,
        "question": question,
        "answer": None,
        "note": (
            "The full-report fallback is not built yet. Tell the user this "
            "feature is coming soon and that, for now, you can only answer from "
            "the data already in the database."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Robin agent — the OpenAI tool-calling loop
# ─────────────────────────────────────────────────────────────────────────────

class RobinAgent:
    def __init__(self, db: FinancialDB, config: dict[str, str | None], *, verbose: bool = False):
        from openai import OpenAI  # lazy import so --help works without openai

        self.db = db
        self.config = config
        self.verbose = verbose
        self.model = config.get("model") or DEFAULT_MODEL
        self.client = OpenAI(api_key=config.get("api_key"))

    # ── tool dispatch ──────────────────────────────────────────────────────
    def _dispatch_tool(self, name: str, args: dict) -> Any:
        if name == "list_companies":
            return {"companies": self.db.list_companies(args.get("query"))}

        if name == "company_overview":
            resolved = self.db.resolve_company(str(args.get("company", "")))
            if "slug" not in resolved:
                return resolved
            return self.db.company_overview(resolved["slug"])

        if name == "get_statement":
            resolved = self.db.resolve_company(str(args.get("company", "")))
            if "slug" not in resolved:
                return resolved
            return self.db.get_statement(
                resolved["slug"],
                statement_key=args.get("statement_key"),
                year=args.get("year"),
                report_type=args.get("report_type"),
                quarter=args.get("quarter"),
            )

        if name == "search_line_items":
            resolved = self.db.resolve_company(str(args.get("company", "")))
            if "slug" not in resolved:
                return resolved
            return self.db.search_line_items(
                resolved["slug"],
                keyword=str(args.get("keyword", "")),
                year=args.get("year"),
                report_type=args.get("report_type"),
            )

        if name == "refer_to_full_report":
            resolved = self.db.resolve_company(str(args.get("company", "")))
            slug = resolved.get("slug") if "slug" in resolved else str(args.get("company", ""))
            return refer_to_full_report(
                slug, str(args.get("question", "")), config=self.config
            )

        return {"error": f"Unknown tool: {name}"}

    # ── one conversational turn ────────────────────────────────────────────
    def ask(self, messages: list[dict]) -> dict:
        """Run the model + tool loop for the current `messages` history.

        `messages` should already include the system prompt and the latest
        user turn. It is mutated in place (assistant + tool turns appended) so
        the caller can keep the running conversation.
        Returns {"reply": str, "tool_events": [...]}.
        """
        events: list[dict] = []

        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0.2,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []

            # Record the assistant turn (with any tool calls) in the history.
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ] if tool_calls else None,
            })

            if not tool_calls:
                return {"reply": msg.content or "", "tool_events": events}

            for tc in tool_calls:
                try:
                    parsed = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    parsed = {}

                if self.verbose:
                    print(f"   · tool: {tc.function.name}({parsed})", file=sys.stderr)

                try:
                    result = self._dispatch_tool(tc.function.name, parsed)
                    ok = not (isinstance(result, dict) and result.get("error"))
                except Exception as ex:  # never let a tool crash the turn
                    result = {"error": str(ex)}
                    ok = False

                events.append({
                    "tool": tc.function.name,
                    "arguments": parsed,
                    "ok": ok,
                    "result": result,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        # Ran out of rounds — ask for a plain wrap-up.
        messages.append({
            "role": "user",
            "content": "Please give your best final answer now based on what you found.",
        })
        resp = self.client.chat.completions.create(
            model=self.model, temperature=0.2, messages=messages,
        )
        final = resp.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": final})
        return {"reply": final, "tool_events": events}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _print_banner(model: str, db_name: str) -> None:
    print("=" * 64)
    print("  Robin — financial data analysis agent")
    print(f"  model: {model}   db: {db_name}")
    print("  Ask about any company's financials. Type 'exit' to quit.")
    print("=" * 64)


def chat_repl(agent: RobinAgent, db_name: str) -> None:
    _print_banner(agent.model, db_name)
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    while True:
        try:
            user_input = input("\nYou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", ":q"}:
            print("Bye.")
            return
        messages.append({"role": "user", "content": user_input})
        try:
            out = agent.ask(messages)
        except Exception as ex:
            print(f"\nRobin > [error] {ex}")
            # drop the failed user turn so the history stays consistent
            continue
        print(f"\nRobin > {out['reply']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Robin — conversational financial data analysis agent over "
                    "the extracted financial_tables in MongoDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--question", "-q", default=None,
                    help="Ask a single question and exit (non-interactive).")
    ap.add_argument("--apikey", default=None,
                    help="OpenAI API key (else OPENAI_API_KEY env or backend/.env).")
    ap.add_argument("--model", default=None,
                    help=f"OpenAI chat model (else OPENAI_CHAT_MODEL / {DEFAULT_MODEL}).")
    ap.add_argument("--mongo-uri", default=None,
                    help="MongoDB URI (else MONGO_URI env / backend/.env).")
    ap.add_argument("--db-name", default=None,
                    help="MongoDB database (else MONGO_DB_NAME env / backend/.env).")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print the DB tool calls Robin makes.")
    args = ap.parse_args(argv)

    config = resolve_config(
        api_key=args.apikey,
        model=args.model,
        mongo_uri=args.mongo_uri,
        db_name=args.db_name,
    )

    if not config.get("api_key"):
        print("ERROR: OpenAI API key not found.", file=sys.stderr)
        print("       Pass --apikey, set OPENAI_API_KEY, or add it to backend/.env",
              file=sys.stderr)
        return 1

    # Connect to Mongo.
    try:
        db = FinancialDB(str(config["mongo_uri"]), str(config["db_name"]))
    except Exception as ex:
        print(f"ERROR: cannot connect to MongoDB at {config['mongo_uri']} ({ex})",
              file=sys.stderr)
        return 1

    try:
        agent = RobinAgent(db, config, verbose=args.verbose)
    except ImportError:
        print("ERROR: the 'openai' package is not installed. "
              "Run: pip install -r scripts/requirements.txt", file=sys.stderr)
        db.close()
        return 1

    try:
        if args.question:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": args.question},
            ]
            out = agent.ask(messages)
            print(out["reply"])
        else:
            chat_repl(agent, str(config["db_name"]))
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
