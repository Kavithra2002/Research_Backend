import fs from "node:fs/promises";
import path from "node:path";

import { env } from "../config/env";
import { generateReportWithWebSearch } from "./openaiReport";
import { getCseSnapshots, type CseSnapshot } from "./cseMarketService";

/* ────────────────────────────────────────────────────────────────────────── *
 * Robin — financial configuration / report generation.
 *
 * The configuration panel lets a user pick companies from the demo data
 * archive and tick which valuation metrics they care about (EPS, P/E, P/B …).
 * Robin then uses OpenAI (with web search) to fetch the latest figures off the
 * internet and write a concise, grounded summary report.
 * ────────────────────────────────────────────────────────────────────────── */

export interface DemoCompany {
  /** Folder name on disk (also used as the stable id). */
  name: string;
  /** Number of report periods detected (best-effort, for display only). */
  reportCount: number;
}

/** Catalog of metrics the panel exposes. `enabled` ones are selectable today. */
export const ROBIN_METRICS = [
  { id: "eps", label: "EPS (Earnings Per Share)", enabled: true },
  { id: "pe", label: "P/E (Price to Earnings)", enabled: true },
  { id: "pb", label: "P/B (Price to Book)", enabled: true },
  { id: "roe", label: "ROE (Return on Equity)", enabled: false },
  { id: "dividend_yield", label: "Dividend Yield", enabled: false },
  { id: "market_cap", label: "Market Capitalisation", enabled: false },
] as const;

export type RobinMetricId = (typeof ROBIN_METRICS)[number]["id"];

const METRIC_LABELS: Record<string, string> = Object.fromEntries(
  ROBIN_METRICS.map((m) => [m.id, m.label]),
);

const ENABLED_METRIC_IDS = new Set<string>(
  ROBIN_METRICS.filter((m) => m.enabled).map((m) => m.id),
);

async function countReportPeriods(companyDir: string): Promise<number> {
  let count = 0;
  for (const kind of ["Annual", "Quarterly"]) {
    try {
      const entries = await fs.readdir(path.join(companyDir, kind), {
        withFileTypes: true,
      });
      count += entries.filter((e) => e.isDirectory()).length;
    } catch {
      // sub-folder may not exist — ignore
    }
  }
  return count;
}

/** List the companies available in the demo data folder. */
export async function listDemoCompanies(): Promise<DemoCompany[]> {
  let entries: import("node:fs").Dirent[];
  try {
    entries = await fs.readdir(env.demoDataDir, { withFileTypes: true });
  } catch {
    return [];
  }

  const dirs = entries.filter((e) => e.isDirectory());
  const companies = await Promise.all(
    dirs.map(async (d) => ({
      name: d.name,
      reportCount: await countReportPeriods(path.join(env.demoDataDir, d.name)),
    })),
  );
  return companies.sort((a, b) => a.name.localeCompare(b.name));
}

export interface RobinReportInput {
  companies: string[];
  metrics: string[];
  user: { first_name: string; last_name: string; email: string };
}

export interface RobinReportOutput {
  report: string;
  usedWebSearch: boolean;
  /** True when at least one company was anchored on live CSE market data. */
  usedCse: boolean;
  companies: string[];
  metrics: string[];
}

function buildInstructions(hasCseData: boolean): string {
  return [
    'You are "Robin", a financial research assistant for the Ambeon Console.',
    "You produce concise, well-structured investment summary reports for companies listed on the Colombo Stock Exchange (CSE), Sri Lanka.",
    "",
    ...(hasCseData
      ? [
          "AUTHORITATIVE CSE MARKET DATA (use this FIRST — it is the source of truth):",
          "  • The user's message includes a 'CSE MARKET DATA' block with LIVE figures pulled directly from the Colombo Stock Exchange (last traded price, previous close, day change, market capitalisation, shares outstanding, as-of time).",
          "  • You MUST use those exact CSE figures for price and market capitalisation. Do NOT overwrite them with web-search values; if the web disagrees, trust the CSE block.",
          "  • Compute valuation ratios from the CSE price: P/E = CSE price ÷ EPS (trailing); P/B = CSE price ÷ book value per share (NAV per share). Show the inputs you used.",
          "  • Use web_search ONLY to fill in fundamentals the CSE block does not contain (e.g. trailing EPS, book value per share / NAV, dividend per share). Prefer the company's latest CSE-filed annual/interim financial statements for those.",
          "  • Clearly label CSE-sourced figures (price, market cap) as 'per CSE, as of <time>'.",
          "",
        ]
      : []),
    "RESEARCH PROCESS (important):",
    "  • Use the web_search tool to look up the latest figures for EACH company before writing anything. Do not rely on memory.",
    "  • For every company, run targeted searches such as: \"<company> CSE EPS P/E P/B\", \"<company> share price cse.lk\", \"<company> annual report earnings per share\". Try the company's CSE symbol too.",
    "  • Good sources: the CSE website (cse.lk), the company's latest annual report / financial statements, and reputable market-data sites (e.g. marketscreener, tradingview, wsj, ft, investing.com).",
    "  • Do at least 2 different searches per company before concluding a metric is unavailable. Only say a figure is unavailable AFTER you have genuinely searched and found nothing.",
    "  • Always include the as-of date / reporting period for each figure, and never invent numbers.",
    "",
    "Formatting rules (keep it SHORT, scannable and user-friendly):",
    "  • Start with a short **bold** title line, then ONE short overview sentence.",
    "  • Present the requested metrics per company in a compact GitHub-style Markdown table (companies as rows, metrics as columns).",
    "  • For each company add a **bold** name line followed by at most 2–3 SHORT bullet points (one line each). No long paragraphs.",
    "  • Finish with a **Takeaway** of 2–3 short bullets comparing the companies.",
    "  • Do NOT use Markdown heading marks (#, ##). Use short **bold** lines for section labels.",
    "",
    "STRICT — no links or URLs:",
    "  • Do NOT put any URLs, hyperlinks, or Markdown links in the report. No inline citations like ([site](https://…)).",
    "  • Do NOT include tracking/query strings or '(stockanalysis.com)' style source tags in the body.",
    "  • Instead, end with ONE plain line: 'Sources: <source names only>, as of <date>.' (names only — e.g. CSE, StockAnalysis — never a URL).",
    "  • Keep the whole report tight enough to read at a glance; it can be exported to PDF.",
  ].join("\n");
}

function fmtNum(value: number | null, opts: { decimals?: number } = {}): string {
  if (value === null) return "n/a";
  const decimals = opts.decimals ?? 2;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  });
}

/** Render the live CSE snapshots as a compact, model-readable ground-truth block. */
function buildCseBlock(snapshots: CseSnapshot[]): string {
  const lines: string[] = [
    "CSE MARKET DATA (authoritative — live from the Colombo Stock Exchange):",
  ];
  for (const s of snapshots) {
    const parts = [
      `last price LKR ${fmtNum(s.price)}`,
      s.previousClose !== null ? `prev close LKR ${fmtNum(s.previousClose)}` : null,
      s.changePercent !== null
        ? `day change ${s.changePercent >= 0 ? "+" : ""}${fmtNum(s.changePercent)}%`
        : null,
      s.marketCap !== null ? `market cap LKR ${fmtNum(s.marketCap, { decimals: 0 })}` : null,
      s.sharesOutstanding !== null
        ? `shares outstanding ${fmtNum(s.sharesOutstanding, { decimals: 0 })}`
        : null,
      s.asOf ? `as of ${s.asOf}` : null,
    ].filter(Boolean);
    lines.push(`  - ${s.name} (${s.symbol}): ${parts.join("; ")}`);
  }
  return lines.join("\n");
}

function buildInput(args: {
  companies: string[];
  metricLabels: string[];
  userName: string;
  cseSnapshots: CseSnapshot[];
}): string {
  const sections = [
    `Requested by: ${args.userName}`,
    "",
    `Companies to analyse:\n${args.companies.map((c) => `  - ${c}`).join("\n")}`,
    "",
    `Valuation metrics to report for each company:\n${args.metricLabels
      .map((m) => `  - ${m}`)
      .join("\n")}`,
  ];

  if (args.cseSnapshots.length > 0) {
    sections.push("", buildCseBlock(args.cseSnapshots));
  }

  sections.push(
    "",
    "Produce the summary report now. Use the CSE market data above as the source of truth for price and market cap, and the latest figures you can find online for the remaining fundamentals.",
  );
  return sections.join("\n");
}

/**
 * Strip noisy inline citations / URLs the web-search model tends to add, so the
 * report stays clean and user-friendly. Keeps the visible text of any link.
 */
function cleanReport(text: string): string {
  return (
    text
      // Drop zero-width / invisible characters the web search model sometimes
      // injects (they break downstream PDF width calculations and add gaps).
      .replace(/[\u200B-\u200D\u2060\uFEFF]/g, "")
      // Normalise exotic unicode spaces to a regular space.
      .replace(/[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]/g, " ")
      // Parenthesised citation groups: ([site](url)) or ([a](u), [b](v))
      .replace(/\s*\((?:\[[^\]]*\]\([^)]*\)[,;\s]*)+\)/g, "")
      // Any remaining Markdown links [text](url) -> text
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      // Bare URLs
      .replace(/https?:\/\/[^\s)]+/g, "")
      // Leftover empty parentheses / brackets
      .replace(/\(\s*\)/g, "")
      .replace(/\[\s*\]/g, "")
      // Collapse spaces left behind, but preserve line breaks
      .replace(/[ \t]{2,}/g, " ")
      .replace(/[ \t]+([,.;])/g, "$1")
      .trim()
  );
}

export async function generateRobinReport(
  input: RobinReportInput,
): Promise<RobinReportOutput> {
  const companies = input.companies
    .map((c) => String(c ?? "").trim())
    .filter(Boolean);
  if (companies.length === 0) {
    throw new Error("Select at least one company to generate a report.");
  }

  // Only honour metrics that are currently enabled.
  const metrics = input.metrics
    .map((m) => String(m ?? "").trim().toLowerCase())
    .filter((m) => ENABLED_METRIC_IDS.has(m));
  if (metrics.length === 0) {
    throw new Error("Select at least one available metric (EPS, P/E or P/B).");
  }

  const metricLabels = metrics.map((m) => METRIC_LABELS[m] ?? m);
  const userName =
    [input.user.first_name, input.user.last_name].filter(Boolean).join(" ") ||
    input.user.email ||
    "a user";

  // Anchor the report on authoritative live CSE market data where available.
  // Best-effort: if CSE is unreachable, fall back to web-search figures only.
  const cseSnapshots = await getCseSnapshots(companies);
  const usedCse = cseSnapshots.length > 0;

  const { report, usedWebSearch } = await generateReportWithWebSearch({
    instructions: buildInstructions(usedCse),
    input: buildInput({ companies, metricLabels, userName, cseSnapshots }),
  });

  return { report: cleanReport(report), usedWebSearch, usedCse, companies, metrics };
}
