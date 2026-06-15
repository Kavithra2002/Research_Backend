import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import { FinancialTable } from "../models/FinancialTable";
import { Company } from "../models/Company";
import { SEARCH_REPORT_TEXT_TOOL, searchReportText } from "./rag/ragTool";
import {
  COMMON_TOOLS,
  currentDateContext,
  dispatchCommonTool,
} from "./agentTools";
import {
  NON_FINANCIAL_DB_TOOLS,
  dispatchNonFinancialTool,
} from "./nonFinancialAgentTools";
import {
  WEB_SEARCH_TOOL,
  dispatchWebSearchTool,
} from "./webSearchAgentTool";
import { RESPONSE_STYLE_GUIDE } from "./responseStyle";

/* ────────────────────────────────────────────────────────────────────────── *
 * Robin — financial data-analysis agent (chat, like Sage)
 *
 * Robin answers questions about the financial data extracted from company
 * annual / quarterly reports and stored in MongoDB (`financial_tables` +
 * `companies`). The model drives the work through tool calls; each tool is a
 * real DB query so every number Robin reports is grounded in stored data.
 *
 * This mirrors the Python `scripts/financial_agent.py` agent, ported to the
 * same OpenAI-tool-calling shape the Sage agent already uses.
 * ────────────────────────────────────────────────────────────────────────── */

export type ChatRole = "system" | "user" | "assistant" | "tool";

export interface ChatMessage {
  role: ChatRole;
  content: string | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
}

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ToolEvent {
  tool: string;
  arguments: unknown;
  result: unknown;
  ok: boolean;
  error?: string;
}

export interface RobinChatInput {
  messages: ChatMessage[];
  user: {
    first_name: string;
    last_name: string;
    email: string;
    user_id: string;
    role: string;
  };
}

export interface RobinChatOutput {
  reply: string;
  messages: ChatMessage[];
  tool_events: ToolEvent[];
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Caps so a single tool result never blows the context window
 * ────────────────────────────────────────────────────────────────────────── */

const MAX_TOOL_ROUNDS = 8;
const MAX_ROWS_RETURNED = 80;
const MAX_MATCHES_RETURNED = 30;
const MAX_TABLES_RETURNED = 6;

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool schema exposed to the model
 * ────────────────────────────────────────────────────────────────────────── */

export const FINANCIAL_DB_TOOLS = [
  {
    type: "function",
    function: {
      name: "list_companies",
      description:
        "List companies that have financial data in the database. Use this to discover available companies or to resolve a company the user named to its exact slug/name.",
      parameters: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description:
              "Optional case-insensitive substring to filter company name or slug.",
          },
        },
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "company_overview",
      description:
        "Show WHAT financial data exists for one company: which years, report types (annual/quarterly), quarters, and statement keys are stored. Call this before fetching a statement so you know what is actually available.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
        },
        required: ["company"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_statement",
      description:
        "Fetch the actual rows of a financial statement table for a company. Statement keys look like 'income_statement', 'sofp' (balance sheet / statement of financial position), 'cash_flow', 'oci', etc. You may also pass a human title fragment. Returns header rows + data rows verbatim (numbers kept as printed).",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
          statement_key: {
            type: "string",
            description:
              "Statement key or title fragment, e.g. 'income_statement', 'balance sheet', 'cash flow'.",
          },
          year: { type: "integer", description: "Reporting year, e.g. 2025." },
          report_type: {
            type: "string",
            enum: ["annual", "quarterly"],
            description: "Filter by report type.",
          },
          quarter: {
            type: "string",
            description: "Quarter for quarterly reports, e.g. 'Q1'.",
          },
        },
        required: ["company"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "search_line_items",
      description:
        "Search inside a company's stored statements for rows whose cells contain a keyword (e.g. 'net interest income', 'total assets', 'revenue'). Best tool for pinpointing a single figure. Returns each matching row with its statement, year and headers so you can read off the right column.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
          keyword: {
            type: "string",
            description: "Text to look for inside the statement rows.",
          },
          year: { type: "integer", description: "Optional reporting year filter." },
          report_type: {
            type: "string",
            enum: ["annual", "quarterly"],
            description: "Optional report type filter.",
          },
        },
        required: ["company", "keyword"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "compare_companies",
      description:
        "Fetch the SAME line item across MULTIPLE companies (and optionally multiple years) in one call. Use this for comparison / multi-company report requests like 'compare net profit for these 4 companies from 2020 to 2025'. Keyword matching is synonym-aware (e.g. 'net profit' also finds 'Profit for the Year'). Returns, per company, the matching rows with their headers so you can read off the right column and build a comparison table.",
      parameters: {
        type: "object",
        properties: {
          companies: {
            type: "array",
            items: { type: "string" },
            description: "Company names or slugs to compare.",
          },
          keyword: {
            type: "string",
            description:
              "The line item to compare, e.g. 'net profit', 'total assets', 'revenue'.",
          },
          years: {
            type: "array",
            items: { type: "integer" },
            description:
              "Optional list of reporting years to include, e.g. [2020,2021,2022,2023,2024,2025]. Omit for all available years.",
          },
          report_type: {
            type: "string",
            enum: ["annual", "quarterly"],
            description: "Optional report type filter (defaults to any).",
          },
        },
        required: ["companies", "keyword"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "refer_to_full_report",
      description:
        "Use ONLY when the requested financial data is NOT in the database AND the user has explicitly agreed to let you look it up in the company's full report. Do not call this without the user's confirmation.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
          question: {
            type: "string",
            description: "The user's original financial question.",
          },
        },
        required: ["company", "question"],
        additionalProperties: false,
      },
    },
  },
  SEARCH_REPORT_TEXT_TOOL,
] as const;

const TOOLS = [
  ...FINANCIAL_DB_TOOLS,
  ...NON_FINANCIAL_DB_TOOLS,
  WEB_SEARCH_TOOL,
  ...COMMON_TOOLS,
] as const;

/* ────────────────────────────────────────────────────────────────────────── *
 * DB helpers (the only place that touches MongoDB)
 * ────────────────────────────────────────────────────────────────────────── */

type CompanyRef = { slug: string; name: string | null };
type ResolveResult =
  | { slug: string; name: string | null }
  | { candidates: CompanyRef[] }
  | { error: string };

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Financial term synonyms
 *
 * Reports rarely use the exact phrase a user types (e.g. "net profit" never
 * appears verbatim — the rows say "Profit for the Year" / "Profit After Tax").
 * We expand a user's keyword to the set of label variants actually used in
 * statements so a search doesn't wrongly come back empty.
 * ────────────────────────────────────────────────────────────────────────── */

const SYNONYM_GROUPS: string[][] = [
  [
    "net profit",
    "net income",
    "profit for the year",
    "profit for the period",
    "profit after tax",
    "profit/(loss) for the year",
    "profit attributable",
    "total comprehensive income",
  ],
  ["profit before tax", "pbt", "profit/(loss) before tax", "profit before taxation"],
  ["gross profit", "gross income"],
  ["operating profit", "operating income", "result from operating activities"],
  ["revenue", "turnover", "total income", "gross income", "sales", "total revenue"],
  ["total assets", "total asset"],
  ["total liabilities", "total liability"],
  ["total equity", "shareholders funds", "shareholders' funds", "net assets", "equity"],
  ["earnings per share", "eps", "earning per share", "earnings/(loss) per share"],
  ["net interest income", "interest income net"],
  ["cash and cash equivalents", "cash equivalents", "cash & cash equivalents"],
  ["finance cost", "finance costs", "interest expense"],
  ["dividend", "dividends", "dividend per share", "dps"],
];

/** Expand a keyword to itself + any synonyms it belongs to (all lower-case). */
function expandKeywords(keyword: string): string[] {
  const kw = keyword.trim().toLowerCase();
  if (!kw) return [];
  const out = new Set<string>([kw]);
  for (const group of SYNONYM_GROUPS) {
    if (group.some((term) => term === kw || kw.includes(term) || term.includes(kw))) {
      for (const term of group) out.add(term);
    }
  }
  return [...out];
}

/** Lower-case, strip suffixes/punctuation so "ambon holdigs" ~ "Ambeon Holdings PLC". */
function normalizeName(value: string): string {
  return String(value ?? "")
    .toLowerCase()
    .replace(/\bplc\b|\blimited\b|\bltd\b|\bpvt\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function levenshtein(a: string, b: string): number {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;
  const prev = new Array(b.length + 1);
  for (let j = 0; j <= b.length; j += 1) prev[j] = j;
  for (let i = 1; i <= a.length; i += 1) {
    let prevDiag = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const tmp = prev[j];
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, prevDiag + cost);
      prevDiag = tmp;
    }
  }
  return prev[b.length];
}

/**
 * Similarity in [0,1] combining edit-distance with token overlap, so a typo
 * (misplaced/missing letters) still scores high against the right company.
 */
function similarity(query: string, candidate: string): number {
  const q = normalizeName(query);
  const c = normalizeName(candidate);
  if (!q || !c) return 0;
  if (q === c) return 1;

  const dist = levenshtein(q, c);
  const editScore = 1 - dist / Math.max(q.length, c.length);

  const qTokens = new Set(q.split(" ").filter(Boolean));
  const cTokens = new Set(c.split(" ").filter(Boolean));
  let overlap = 0;
  for (const t of qTokens) {
    if (cTokens.has(t)) overlap += 1;
    else {
      // partial token match (typo within a single word)
      for (const ct of cTokens) {
        if (ct.length > 3 && levenshtein(t, ct) <= 2) {
          overlap += 0.6;
          break;
        }
      }
    }
  }
  const tokenScore = overlap / Math.max(qTokens.size, cTokens.size);
  const contains = c.includes(q) || q.includes(c) ? 0.15 : 0;

  return Math.min(1, 0.6 * editScore + 0.4 * tokenScore + contains);
}

/** Every known company (registry + whatever financial_tables references). */
async function allKnownCompanies(): Promise<CompanyRef[]> {
  const [registry, tableCompanies] = await Promise.all([
    Company.find({}).select({ _id: 0, slug: 1, name: 1 }).lean(),
    FinancialTable.aggregate<{ _id: string; name: string | null }>([
      { $group: { _id: "$company_slug", name: { $first: "$company_name" } } },
    ]),
  ]);

  const bySlug = new Map<string, CompanyRef>();
  for (const c of registry) {
    bySlug.set(c.slug, { slug: c.slug, name: c.name ?? null });
  }
  for (const c of tableCompanies) {
    const existing = bySlug.get(c._id);
    if (existing) {
      if (!existing.name) existing.name = c.name ?? null;
    } else {
      bySlug.set(c._id, { slug: c._id, name: c.name ?? null });
    }
  }
  return [...bySlug.values()];
}

/** Top fuzzy candidates for a typed (possibly misspelled) company term. */
async function fuzzyCandidates(term: string, limit = 3): Promise<CompanyRef[]> {
  const all = await allKnownCompanies();
  const scored = all
    .map((ref) => ({
      ref,
      score: Math.max(
        similarity(term, ref.name ?? ref.slug),
        similarity(term, ref.slug),
      ),
    }))
    .filter((s) => s.score >= 0.45)
    .sort((a, b) => b.score - a.score);
  return scored.slice(0, limit).map((s) => s.ref);
}

/** Contains-only registry lookup (exact substring on name or slug). */
async function containsCompanies(query?: string): Promise<CompanyRef[]> {
  const filter: Record<string, unknown> = {};
  const q = query?.trim();
  if (q) {
    const rx = { $regex: escapeRegex(q), $options: "i" };
    filter.$or = [{ name: rx }, { slug: rx }];
  }
  const docs = await Company.find(filter)
    .select({ _id: 0, slug: 1, name: 1 })
    .sort({ name: 1 })
    .limit(60)
    .lean();
  return docs.map((d) => ({ slug: d.slug, name: d.name ?? null }));
}

async function listCompanies(query?: string): Promise<CompanyRef[]> {
  const q = query?.trim();
  const docs = await containsCompanies(q);

  // If a substring query found nothing, it may be a typo — surface the closest
  // matches by spelling so Robin can suggest "Did you mean …?".
  if (q && docs.length === 0) {
    return fuzzyCandidates(q, 5);
  }
  return docs;
}

async function resolveCompany(term: string): Promise<ResolveResult> {
  const t = (term || "").trim();
  if (!t) return { error: "No company name/slug given." };

  // 1) exact slug
  const bySlug = await Company.findOne({ slug: t })
    .select({ _id: 0, slug: 1, name: 1 })
    .lean();
  if (bySlug) return { slug: bySlug.slug, name: bySlug.name ?? null };

  // 2) exact (case-insensitive) name
  const byName = await Company.findOne({
    name: { $regex: `^${escapeRegex(t)}$`, $options: "i" },
  })
    .select({ _id: 0, slug: 1, name: 1 })
    .lean();
  if (byName) return { slug: byName.slug, name: byName.name ?? null };

  // 3) substring contains in the company registry (only true substrings
  //    auto-resolve when unique; fuzzy/typo matches are handled in step 5).
  const matches = await containsCompanies(t);
  if (matches.length === 1) return matches[0];
  if (matches.length > 1) return { candidates: matches.slice(0, 10) };

  // 4) fall back to whatever financial_tables knows
  const rx = { $regex: escapeRegex(t), $options: "i" };
  const slugs: string[] = await FinancialTable.distinct("company_slug", {
    $or: [{ company_slug: rx }, { company_name: rx }],
  });
  if (slugs.length === 1) {
    const nameDoc = await FinancialTable.findOne({ company_slug: slugs[0] })
      .select({ company_name: 1 })
      .lean();
    return { slug: slugs[0], name: nameDoc?.company_name ?? null };
  }
  if (slugs.length > 1) {
    return {
      candidates: slugs.slice(0, 10).map((s) => ({ slug: s, name: s })),
    };
  }

  // 5) fuzzy fallback — handles typos / misplaced letters. We deliberately do
  //    NOT auto-resolve these; we return candidates so Robin asks the user to
  //    confirm ("Did you mean …?") before pulling any data.
  const fuzzy = await fuzzyCandidates(t);
  if (fuzzy.length > 0) {
    return { candidates: fuzzy };
  }

  return { error: `No company found matching '${t}'.` };
}

async function companyOverview(companySlug: string): Promise<unknown> {
  const grouped = await FinancialTable.aggregate<{
    _id: { report_type: string; year: number | null; quarter: string | null };
    statements: string[];
  }>([
    { $match: { company_slug: companySlug } },
    {
      $group: {
        _id: {
          report_type: "$report_type",
          year: "$year",
          quarter: "$quarter",
        },
        statements: { $addToSet: "$statement_key" },
      },
    },
    { $sort: { "_id.year": -1, "_id.report_type": 1, "_id.quarter": 1 } },
  ]);

  const periods = grouped.map((g) => ({
    report_type: g._id.report_type,
    year: g._id.year,
    quarter: g._id.quarter,
    statements: [...g.statements].filter(Boolean).sort(),
  }));

  const nameDoc = await FinancialTable.findOne({ company_slug: companySlug })
    .select({ company_name: 1 })
    .lean();

  return {
    company_slug: companySlug,
    company_name: nameDoc?.company_name ?? companySlug,
    period_count: periods.length,
    periods,
  };
}

function slimTable(doc: Record<string, unknown>): unknown {
  const rawRows = (doc.rows as Array<{ cells?: unknown }> | undefined) ?? [];
  const rows: unknown[] = [];
  for (const r of rawRows.slice(0, MAX_ROWS_RETURNED)) {
    if (r && Array.isArray(r.cells)) rows.push(r.cells);
  }
  return {
    company_name: doc.company_name,
    year: doc.year,
    report_type: doc.report_type,
    quarter: doc.quarter,
    period_label: doc.period_label,
    statement_key: doc.statement_key,
    statement_title: doc.statement_title,
    caption: doc.caption,
    header_rows: doc.header_rows ?? [],
    rows,
    row_count: doc.row_count,
    source_pdf: doc.source_pdf,
  };
}

async function getStatement(args: {
  companySlug: string;
  statement_key?: string;
  year?: number;
  report_type?: string;
  quarter?: string;
}): Promise<unknown> {
  const filter: Record<string, unknown> = { company_slug: args.companySlug };
  if (args.statement_key) {
    const rx = { $regex: escapeRegex(args.statement_key), $options: "i" };
    filter.$or = [{ statement_key: rx }, { statement_title: rx }];
  }
  if (typeof args.year === "number") filter.year = args.year;
  if (args.report_type) filter.report_type = args.report_type.toLowerCase();
  if (args.quarter) filter.quarter = args.quarter.toUpperCase();

  const docs = await FinancialTable.find(filter)
    .sort({ year: -1, table_index: 1 })
    .lean();

  if (docs.length === 0) {
    return {
      found: false,
      note: "No matching statement/table stored for that company with those filters.",
    };
  }
  return {
    found: true,
    table_count: docs.length,
    tables: docs.slice(0, MAX_TABLES_RETURNED).map((d) => slimTable(d as Record<string, unknown>)),
  };
}

async function searchLineItems(args: {
  companySlug: string;
  keyword: string;
  year?: number;
  report_type?: string;
}): Promise<unknown> {
  const kw = (args.keyword || "").trim().toLowerCase();
  if (!kw) return { error: "keyword is required for search_line_items" };

  // Expand to synonyms so "net profit" still matches "Profit for the Year" etc.
  const keywords = expandKeywords(kw);

  const filter: Record<string, unknown> = { company_slug: args.companySlug };
  if (typeof args.year === "number") filter.year = args.year;
  if (args.report_type) filter.report_type = args.report_type.toLowerCase();

  const docs = await FinancialTable.find(filter).sort({ year: -1 }).lean();

  const matches: unknown[] = [];
  for (const doc of docs) {
    const headerRows = doc.header_rows ?? [];
    const rows = (doc.rows as Array<{ cells?: unknown }> | undefined) ?? [];
    for (const row of rows) {
      const cells = row?.cells;
      if (!Array.isArray(cells) || cells.length === 0) continue;
      // Match the row label (first cell) against any synonym.
      const label = String(cells[0] ?? "").toLowerCase();
      const matched = keywords.some(
        (k) => label.includes(k) || cells.some((c) => String(c).toLowerCase().includes(k)),
      );
      if (matched) {
        matches.push({
          company_name: doc.company_name,
          year: doc.year,
          report_type: doc.report_type,
          quarter: doc.quarter,
          statement_key: doc.statement_key,
          statement_title: doc.statement_title,
          header_rows: headerRows,
          row: cells,
        });
        if (matches.length >= MAX_MATCHES_RETURNED) {
          return {
            match_count: matches.length,
            truncated: true,
            tried_keywords: keywords,
            matches,
          };
        }
      }
    }
  }
  return {
    match_count: matches.length,
    truncated: false,
    tried_keywords: keywords,
    matches,
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Multi-company comparison
 *
 * Fetches the same line item across SEVERAL companies (and optionally several
 * years) in one call, so requests like "compare net profit for these 4
 * companies 2020–2025" don't require many manual single-company lookups.
 * ────────────────────────────────────────────────────────────────────────── */

interface CompanyComparison {
  company: string;
  resolved: boolean;
  company_name?: string;
  candidates?: CompanyRef[];
  found?: boolean;
  match_count?: number;
  matches?: unknown[];
  error?: string;
}

async function compareCompanies(args: {
  companies: string[];
  keyword: string;
  years?: number[];
  report_type?: string;
}): Promise<unknown> {
  const keyword = (args.keyword || "").trim();
  if (!keyword) return { error: "keyword is required for compare_companies" };
  if (!Array.isArray(args.companies) || args.companies.length === 0) {
    return { error: "companies (array) is required for compare_companies" };
  }

  const keywords = expandKeywords(keyword);
  const years = Array.isArray(args.years)
    ? args.years.filter((y) => typeof y === "number")
    : [];
  const reportType = args.report_type?.toLowerCase();

  const results: CompanyComparison[] = [];

  for (const rawName of args.companies) {
    const name = String(rawName ?? "").trim();
    if (!name) continue;

    const resolved = await resolveCompany(name);

    if ("error" in resolved) {
      results.push({ company: name, resolved: false, error: resolved.error });
      continue;
    }
    if ("candidates" in resolved) {
      results.push({
        company: name,
        resolved: false,
        candidates: resolved.candidates,
      });
      continue;
    }

    // Resolved to a single company — pull matching rows.
    const filter: Record<string, unknown> = { company_slug: resolved.slug };
    if (reportType) filter.report_type = reportType;
    if (years.length > 0) filter.year = { $in: years };

    const docs = await FinancialTable.find(filter).sort({ year: -1 }).lean();
    const matches: unknown[] = [];
    for (const doc of docs) {
      const rows = (doc.rows as Array<{ cells?: unknown }> | undefined) ?? [];
      for (const row of rows) {
        const cells = row?.cells;
        if (!Array.isArray(cells) || cells.length === 0) continue;
        const label = String(cells[0] ?? "").toLowerCase();
        if (keywords.some((k) => label.includes(k))) {
          matches.push({
            year: doc.year,
            report_type: doc.report_type,
            statement_title: doc.statement_title,
            header_rows: doc.header_rows ?? [],
            row: cells,
          });
          if (matches.length >= MAX_MATCHES_RETURNED) break;
        }
      }
      if (matches.length >= MAX_MATCHES_RETURNED) break;
    }

    results.push({
      company: name,
      resolved: true,
      company_name: resolved.name ?? resolved.slug,
      found: matches.length > 0,
      match_count: matches.length,
      matches,
    });
  }

  return {
    keyword,
    tried_keywords: keywords,
    years: years.length > 0 ? years : "all",
    report_type: reportType ?? "any",
    company_count: results.length,
    results,
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * FUTURE FEATURE — "refer to the full report" (intentional placeholder)
 *
 * When the user asks a financial question whose answer is NOT in the database,
 * Robin asks for permission and — if granted — should read the company's FULL
 * report to answer (using the same OpenAI key). The tool is wired up but the
 * heavy report-reading pipeline is left as a stub to build later (e.g. locate
 * the source PDF, chunk/retrieve relevant text, ask OpenAI, optionally write
 * the figure back into financial_tables).
 * ────────────────────────────────────────────────────────────────────────── */

async function referToFullReport(
  companySlug: string,
  question: string,
): Promise<unknown> {
  // Now backed by RAG: semantic-search the company's ingested report text and
  // hand the most relevant passages back so the model can answer from them.
  const result = await searchReportText({
    query: question,
    company: companySlug,
    top_k: 6,
  });
  return {
    implemented: true,
    company_slug: companySlug,
    question,
    ...(result as object),
    note:
      "These passages come from the company's full report via vector search. " +
      "Answer the user's question grounded ONLY in this text and cite the section. " +
      "If the passages don't contain the answer, say so.",
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool dispatch
 * ────────────────────────────────────────────────────────────────────────── */

/**
 * Dispatch a financial / report-data tool by name. Returns `undefined` when the
 * tool name isn't one of these, so callers can fall through to other tool sets.
 * Exported so other agents (e.g. Tuck) can reuse the exact same DB-backed tools.
 */
export async function dispatchFinancialTool(
  name: string,
  args: unknown,
): Promise<unknown | undefined> {
  const a = (args ?? {}) as Record<string, unknown>;

  switch (name) {
    case "list_companies":
      return {
        companies: await listCompanies(
          typeof a.query === "string" ? a.query : undefined,
        ),
      };

    case "company_overview": {
      const resolved = await resolveCompany(String(a.company ?? ""));
      if (!("slug" in resolved)) return resolved;
      return companyOverview(resolved.slug);
    }

    case "get_statement": {
      const resolved = await resolveCompany(String(a.company ?? ""));
      if (!("slug" in resolved)) return resolved;
      return getStatement({
        companySlug: resolved.slug,
        statement_key:
          typeof a.statement_key === "string" ? a.statement_key : undefined,
        year: typeof a.year === "number" ? a.year : undefined,
        report_type:
          typeof a.report_type === "string" ? a.report_type : undefined,
        quarter: typeof a.quarter === "string" ? a.quarter : undefined,
      });
    }

    case "search_line_items": {
      const resolved = await resolveCompany(String(a.company ?? ""));
      if (!("slug" in resolved)) return resolved;
      return searchLineItems({
        companySlug: resolved.slug,
        keyword: String(a.keyword ?? ""),
        year: typeof a.year === "number" ? a.year : undefined,
        report_type:
          typeof a.report_type === "string" ? a.report_type : undefined,
      });
    }

    case "compare_companies": {
      const companies = Array.isArray(a.companies)
        ? a.companies.map((c) => String(c))
        : [];
      const years = Array.isArray(a.years)
        ? a.years
            .map((y) => (typeof y === "number" ? y : Number(y)))
            .filter((y) => Number.isFinite(y))
        : undefined;
      return compareCompanies({
        companies,
        keyword: String(a.keyword ?? ""),
        years,
        report_type:
          typeof a.report_type === "string" ? a.report_type : undefined,
      });
    }

    case "refer_to_full_report": {
      const resolved = await resolveCompany(String(a.company ?? ""));
      const slug = "slug" in resolved ? resolved.slug : String(a.company ?? "");
      return referToFullReport(slug, String(a.question ?? ""));
    }

    case "search_report_text":
      return searchReportText(a);

    default:
      return undefined;
  }
}

async function dispatchTool(name: string, args: unknown): Promise<unknown> {
  const financial = await dispatchFinancialTool(name, args);
  if (financial !== undefined) return financial;

  const nonFinancial = await dispatchNonFinancialTool(name, args);
  if (nonFinancial !== undefined) return nonFinancial;

  const web = await dispatchWebSearchTool(name, args);
  if (web !== undefined) return web;

  const common = dispatchCommonTool(name, (args ?? {}) as Record<string, unknown>);
  if (common !== undefined) return common;

  throw new Error(`Unknown tool: ${name}`);
}

/* ────────────────────────────────────────────────────────────────────────── *
 * System prompt — Robin's persona + grounding rules
 * ────────────────────────────────────────────────────────────────────────── */

function buildSystemPrompt(user: RobinChatInput["user"]): string {
  const displayName =
    [user.first_name, user.last_name].filter(Boolean).join(" ") || user.email;
  return [
    `You are "Robin", a financial & company intelligence assistant inside the Ambeon Console.`,
    `The signed-in user is ${displayName} (role: ${user.role}).`,
    `You answer questions about companies using data extracted from their annual and quarterly reports and stored in a database — both FINANCIAL figures (revenue, profit, assets, etc.) and NON-FINANCIAL facts (sector, business overview, employees, branches, group structure, sustainability, governance, awards, etc.).`,
    currentDateContext(),
    "",
    "Understand the user first:",
    "  • Work out what the user actually wants, even if their wording is short, informal, or has typos. Interpret intent charitably.",
    "  • If the request is genuinely ambiguous, ask ONE short clarifying question instead of guessing.",
    "  • Match the user's tone and language; keep answers clear, friendly and concise.",
    "",
    "You can help with anything, not just finance:",
    "  • Small talk and general chat (\"how are you\", greetings) — reply warmly and briefly.",
    "  • General knowledge, definitions, and explaining hard words or technical concepts — answer from your own knowledge in plain language.",
    "  • Date/time questions — call get_current_time (never guess the date).",
    "  • Any calculation (percentages, growth, ratios, averages, etc.) — use the calculate tool for the exact result rather than doing the maths in your head.",
    "",
    "How to work:",
    "  • For any question about a company's financial figures, USE THE TOOLS to look up real data before answering. Never guess or fabricate numbers.",
    "  • Typical financial flow: resolve the company (list_companies) → see what exists (company_overview) → pull the figure (search_line_items or get_statement).",
    "  • For NON-FINANCIAL questions (sector, what the company does, employees, branches, group structure, subsidiaries, vision/mission, awards, sustainability, governance), use the non-financial tools: list_non_financial_companies → non_financial_overview → get_non_financial_metric with the right keyword.",
    "  • Numbers are stored exactly as printed in the report (with commas, and brackets for negatives). Report them faithfully; only do arithmetic the user asks for, and show your working briefly.",
    "  • Always state which company, year and source a figure came from.",
    "",
    "Finding the right line item (IMPORTANT — don't give up too early):",
    "  • Reports rarely use the user's exact words. 'Net profit' is usually printed as 'Profit for the Year' / 'Profit After Tax'; 'revenue' may be 'Turnover' / 'Total Income'. search_line_items is synonym-aware, but if a keyword returns nothing, TRY ALTERNATIVES (e.g. 'profit for the year', 'profit after tax') or pull the whole statement with get_statement and read the right row yourself.",
    "  • Only say data is unavailable AFTER you've tried synonyms and the relevant statement. Do not conclude 'not found' from a single failed keyword.",
    "",
    "Multi-company / comparison requests (IMPORTANT):",
    "  • When the user asks about SEVERAL companies at once (e.g. 'summarize net profit for these 4 companies 2020–2025'), use the compare_companies tool in ONE call with the list of companies, the keyword and the years. Do not refuse multi-company work — that is exactly what this tool is for.",
    "  • Then present a single comparison: a short summary sentence, a Markdown table (companies as rows or columns, years across), and a chart if a trend/comparison is implied.",
    "  • If some companies resolve to candidates (ambiguous names), note which ones you couldn't resolve and proceed with the rest.",
    "",
    "Company name matching (IMPORTANT):",
    "  • Users often mistype a company name (missing or misplaced letters). When a tool returns `candidates` instead of a resolved company, the name was NOT an exact match.",
    "  • In that case, DO NOT pick one and fetch data silently. Instead ask the user to confirm: e.g. \"Did you mean **Ambeon Holdings PLC**?\" (or list the 2 closest options and ask which one).",
    "  • Only after the user confirms the company should you call the data tools again with the confirmed name/slug.",
    "  • If a company name is genuinely ambiguous, ask which one they meant.",
    "",
    "Formatting (make every answer easy to read and friendly):",
    "  • Prefer short paragraphs and bullet points over long blocks of text.",
    "  • Use **bold** to highlight key figures, company names and labels.",
    "  • Use bullet lists ('- item') for multiple points, and numbered lists ('1. ') for steps or rankings.",
    "  • Do NOT use Markdown heading marks (#, ##, ###). For a section label, use a short **bold** line instead.",
    "  • Keep a warm, clear, conversational tone.",
    "",
    "Tables:",
    "  • When the answer is naturally tabular (multiple years, multiple line items, comparisons), format it as a GitHub-style Markdown table (header row, a |---| separator row, then data rows). The interface renders these as interactive tables.",
    "  • Add one short sentence of context before the table.",
    "",
    "Charts / graphs / data visualizations (IMPORTANT):",
    "  • When the user asks for a chart, graph, plot, trend, pie chart, bar chart, line chart, or any visualization, DO NOT answer with only a table. Emit a chart spec the interface will render as a real chart.",
    "  • First fetch the real figures with the tools, then output a fenced code block tagged `chart` containing a single JSON object with this exact shape:",
    "      ```chart",
    '      {"type":"bar","title":"Annual net profit (2021–2025)","unit":"LKR \'000","categories":["2021","2022","2023","2024","2025"],"series":[{"name":"Net profit","values":[1200,1500,1100,1800,2100]}]}',
    "      ```",
    "  • Fields: `type` is one of \"bar\", \"line\", \"area\", \"pie\", or \"donut\". `categories` are the x-axis labels (or pie slice labels). `series` is an array of {name, values}; `values` must be PLAIN NUMBERS (no commas, currency symbols or brackets — convert bracketed negatives like (1,234) to -1234). Each series' `values` length must match `categories` length.",
    "  • Pick the chart type sensibly: trends over time → line or area; comparing categories/years → bar; composition/share of a total → pie or donut. If the user names a type, use it.",
    "  • For pie/donut, provide exactly one series whose values are the parts of the whole.",
    "  • Put one short sentence of context before the chart. You may also include a small Markdown table after it if helpful, but the chart is the main answer.",
    "  • Only emit a chart block when the user actually wants a visualization. Never put fabricated numbers in it.",
    "",
    "Why / reason / cause questions (e.g. 'why did Dialog lose money in 2022', 'reason for the profit drop') — MANDATORY tool flow:",
    "  • These are NOT answered from memory alone. You MUST call tools before replying.",
    "  • Step 1 — pull the figure: search_line_items / get_statement for the loss/profit in the year asked.",
    "  • Step 2 — call search_report_text with a focused query (e.g. 'reason for net loss 2022 forex provisions impairments management discussion'). Do NOT use refer_to_full_report for these — search_report_text runs automatically without asking the user.",
    "  • Step 3 — if report text is thin or empty, call web_search immediately WITHOUT asking permission (e.g. 'Dialog Axiata 2022 net loss reason forex provisions').",
    "  • Step 4 — only after steps 1–3, if nothing useful was found, say briefly you could not find the specific reason. NEVER reply with 'Would you like me to search online?' — you should already have searched.",
    "  • If a prior turn in this conversation already explained the reason (with figures or narrative), answer from that context directly — do not claim you cannot find it.",
    "",
    "When the data is NOT in the structured database (ESCALATE before apologising — IMPORTANT):",
    "  • NEVER reply with \"sorry, I don't have that\" until you have actually tried the other sources below. A bare apology without trying them is wrong.",
    "  • Step 1 — exhaust the database: try synonyms with search_line_items and pull the whole statement with get_statement / company_overview / the non-financial tools. Don't conclude 'not found' from one failed keyword.",
    "  • Step 2 — read the report text: call search_report_text for anything qualitative or explanatory — e.g. WHY a figure moved, the cause of a loss/profit drop, strategy, risks, management commentary. Never ask permission first.",
    "  • Step 3 — search the web automatically: if the database and report text still don't contain the answer to a COMPANY-SPECIFIC factual or explanatory question (e.g. \"why did <company> make a loss in 2022\", \"reason profit fell\", \"what caused the decline\", recent news), call web_search WITHOUT asking permission first. Pass the company name plus any context you gathered (sector, geography, the actual figures you found) so the result is specific to that company. Then answer from what it returns and clearly label which parts came from web research vs stored data.",
    "  • Step 4 — only if the database, report text AND web search all come back with nothing useful, say briefly that you couldn't find it (e.g. \"I couldn't find the specific reason in the reports or in recent public sources.\"). Do NOT fabricate a reason. Do NOT offer to search the web as a next step — you should have already done it.",
    "  • Never present web-sourced or general-knowledge claims as if they came from the company's filed reports.",
    "",
    "Non-financial company data (IMPORTANT — use the dedicated tools):",
    "  • Single-year questions ('what sector is this company', 'give me a briefing', 'how many employees in 2023', 'how many branches', 'what is the group structure', 'subsidiaries', 'awards', 'sustainability') → call non_financial_overview first, then get_non_financial_metric with keywords like 'employees', 'branches', 'group structure', 'sector', 'about', 'subsidiaries', 'awards'. Read the figure from best_match.value (and numeric_value for charts).",
    "  • Multi-year / trend / CHART questions ('chart of employees from 2020 to 2025', 'how has the branch network grown', 'training hours over time') → use get_non_financial_timeseries in ONE call. It returns one point per year with value + numeric_value. NEVER make separate per-year calls for a trend, and NEVER label a year 'not available' unless that year's point has found=false. Use numeric_value for the chart's values array.",
    "  • The company_overview field from non_financial_overview is ideal for sector / business briefings.",
    "  • If a metric is genuinely not found (found=false), say so for that specific year only; you may fall back to search_report_text for narrative passages.",
    "",
    "Web search (use it as the fallback before giving up — IMPORTANT):",
    "  • Two cases call for web_search: (a) the user asks how an EXTERNAL event might affect a company (wars, geopolitical crises, pandemics, policy changes, global economic trends, commodity prices, competitor moves); and (b) a COMPANY-SPECIFIC fact or explanation the user wants is NOT in our database or report text — e.g. the reason behind a loss/profit drop, why results moved, or recent developments.",
    "  • In BOTH cases, FIRST gather the company's profile and the relevant figures from the database (non_financial_overview + relevant metrics + the financial figure in question), THEN call web_search with that context so the analysis is specific to the company.",
    "  • For case (b) you do NOT need to ask the user for permission first — search the web automatically once the stored sources fall short, then answer.",
    "  • Clearly separate what comes from stored database/report data vs web search in your answer (e.g. add a short \"From public sources:\" note).",
    "  • Do NOT use web_search for facts that ARE already in the database — always check the DB tools and report text first; web search is the fallback, not the first move.",
    "",
    "Qualitative / narrative questions (report text — RAG):",
    "  • For questions about what a report SAYS (strategy, risks, governance, sustainability, chairman's/CEO's outlook, business model, 'about the company', WHY profits fell, reasons for losses, forex/provision impacts, etc.), use the search_report_text tool. It runs vector search over the ingested report text and returns the most relevant passages with citations.",
    "  • Call it automatically — never ask the user for permission. refer_to_full_report is only for when the user explicitly agrees after you could not find structured figures; prefer search_report_text for narrative and 'why' questions.",
    "  • Ground your answer ONLY in the returned passages and cite the section (e.g. \"Risk Management Report\"). If nothing relevant comes back, proceed to web_search before giving up.",
    "",
    "Reports & PDF export:",
    "  • When the user asks for a report or to create/download a PDF, produce a clean, well-structured one: a short **bold** title line, then a brief summary paragraph, then the supporting figures as Markdown tables and/or chart blocks, then a short closing note. Always ground every figure with the tools first.",
    "  • Such an answer can be downloaded as a PDF, so keep the structure tidy with **bold** labels, bullet points and tables — but never use # heading marks.",
    "",
    "General chat:",
    "  • If the user just chats or asks something general (not about a specific company), answer normally and conversationally without using the data tools.",
    "",
    RESPONSE_STYLE_GUIDE,
    "",
    "Greet the user by their first name on the very first turn. Keep replies clear and concise; use short tables or bullet points for multiple figures.",
  ].join("\n");
}

/* ────────────────────────────────────────────────────────────────────────── *
 * OpenAI chat loop
 * ────────────────────────────────────────────────────────────────────────── */

interface OpenAIChoiceMessage {
  role: "assistant";
  content: string | null;
  tool_calls?: ToolCall[];
}

interface OpenAIResponse {
  choices: Array<{ message: OpenAIChoiceMessage; finish_reason: string }>;
}

async function callOpenAI(messages: ChatMessage[]): Promise<OpenAIChoiceMessage> {
  if (!env.openai.apiKey) {
    throw HttpError.badRequest(
      "OPENAI_API_KEY is not configured on the server. Add it to backend/.env to enable Robin.",
    );
  }
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.openai.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: env.openai.model,
      temperature: 0.2,
      messages,
      tools: TOOLS,
      tool_choice: "auto",
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new HttpError(
      res.status === 401 ? 500 : 502,
      `OpenAI request failed (${res.status})`,
      text || undefined,
    );
  }

  const data = (await res.json()) as OpenAIResponse;
  const choice = data.choices?.[0];
  if (!choice) throw HttpError.internal("OpenAI returned no choices");
  return choice.message;
}

export async function runRobinChat(
  input: RobinChatInput,
): Promise<RobinChatOutput> {
  const systemPrompt = buildSystemPrompt(input.user);

  const history: ChatMessage[] = [
    { role: "system", content: systemPrompt },
    ...input.messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role, content: m.content ?? "" })),
  ];

  const events: ToolEvent[] = [];

  for (let round = 0; round < MAX_TOOL_ROUNDS; round += 1) {
    const assistant = await callOpenAI(history);

    history.push({
      role: "assistant",
      content: assistant.content ?? null,
      tool_calls: assistant.tool_calls,
    });

    if (!assistant.tool_calls || assistant.tool_calls.length === 0) {
      return {
        reply: assistant.content ?? "",
        messages: history,
        tool_events: events,
      };
    }

    for (const call of assistant.tool_calls) {
      let parsedArgs: unknown = {};
      try {
        parsedArgs = call.function.arguments
          ? JSON.parse(call.function.arguments)
          : {};
      } catch {
        parsedArgs = {};
      }

      let result: unknown;
      let ok = true;
      let errorMessage: string | undefined;
      try {
        result = await dispatchTool(call.function.name, parsedArgs);
        if (result && typeof result === "object" && "error" in result) {
          ok = false;
          errorMessage = String((result as { error: unknown }).error);
        }
      } catch (err) {
        ok = false;
        errorMessage = err instanceof Error ? err.message : String(err);
        result = { error: errorMessage };
      }

      events.push({
        tool: call.function.name,
        arguments: parsedArgs,
        result,
        ok,
        error: errorMessage,
      });

      history.push({
        role: "tool",
        tool_call_id: call.id,
        name: call.function.name,
        content: JSON.stringify(result),
      });
    }
  }

  // Ran out of tool rounds — ask for a final natural-language answer.
  history.push({
    role: "user",
    content: "Please give your best final answer now based on what you found.",
  });
  const final = await callOpenAI(history);
  history.push({ role: "assistant", content: final.content ?? null });
  return {
    reply: final.content ?? "",
    messages: history,
    tool_events: events,
  };
}
