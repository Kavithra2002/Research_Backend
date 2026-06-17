import { FinancialTable } from "../models/FinancialTable";
import { getSectorLens } from "./extractedService";
import { listDatabaseCompaniesWithSector } from "./sectorAgentTools";

/* ────────────────────────────────────────────────────────────────────────── *
 * Investment screening — compare ALL companies in our database before buy advice.
 * ────────────────────────────────────────────────────────────────────────── */

const PAT_LABEL_TERMS = [
  "profit after tax",
  "profit for the year",
  "profit for the period",
  "profit attributable",
];

const REVENUE_LABEL_TERMS = [
  "revenue",
  "turnover",
  "total income",
  "income from operations",
];

function parseAmount(cell: unknown): number | null {
  const raw = String(cell ?? "").trim();
  if (!raw || raw === "-" || raw === "—") return null;
  const neg = raw.startsWith("(") && raw.endsWith(")");
  const cleaned = raw.replace(/[(),\s]/g, "");
  const n = Number(cleaned);
  if (!Number.isFinite(n)) return null;
  return neg ? -n : n;
}

function rowLabelMatches(label: string, terms: string[]): boolean {
  const l = label.toLowerCase();
  return terms.some((t) => l.includes(t));
}

/** Read the main amount column (usually index 1) from an annual income row. */
function valueFromIncomeRow(cells: unknown[], terms: string[]): number | null {
  if (!Array.isArray(cells) || cells.length < 2) return null;
  const label = String(cells[0] ?? "");
  if (!rowLabelMatches(label, terms)) return null;
  for (let i = 1; i < cells.length; i += 1) {
    const v = parseAmount(cells[i]);
    if (v !== null) return v;
  }
  return null;
}

async function annualMetricByYear(
  slug: string,
  terms: string[],
  years: number[],
): Promise<Record<number, number>> {
  const out: Record<number, number> = {};
  if (years.length === 0) return out;

  const docs = await FinancialTable.find({
    company_slug: slug,
    report_type: "annual",
    year: { $in: years },
  })
    .select({ year: 1, statement_key: 1, rows: 1 })
    .lean();

  const byYear = new Map<number, typeof docs>();
  for (const doc of docs) {
    if (doc.year == null) continue;
    const list = byYear.get(doc.year) ?? [];
    list.push(doc);
    byYear.set(doc.year, list);
  }

  for (const year of years) {
    const tables = byYear.get(year) ?? [];
    const incomeFirst = [
      ...tables.filter((t) =>
        String(t.statement_key ?? "").toLowerCase().includes("income"),
      ),
      ...tables.filter(
        (t) => !String(t.statement_key ?? "").toLowerCase().includes("income"),
      ),
    ];
    for (const doc of incomeFirst) {
      const rows = (doc.rows as Array<{ cells?: unknown }> | undefined) ?? [];
      for (const row of rows) {
        const cells = row?.cells;
        if (!Array.isArray(cells)) continue;
        const v = valueFromIncomeRow(cells, terms);
        if (v !== null) {
          out[year] = v;
          break;
        }
      }
      if (out[year] != null) break;
    }
  }

  return out;
}

function growthPct(current: number | null, prior: number | null): number | null {
  if (current === null || prior === null || prior === 0) return null;
  return ((current - prior) / Math.abs(prior)) * 100;
}

export type ScreenedCompany = {
  slug: string;
  name: string;
  sector: string | null;
  sector_detail: string | null;
  latest_year: number | null;
  report_years: number[];
  revenue_t12m: number | null;
  eps: number | null;
  pe_ratio: number | null;
  market_cap: number | null;
  cce_lf: number | null;
  profit_after_tax_by_year: Record<string, number>;
  revenue_by_year: Record<string, number>;
  profit_growth_latest_yoy_pct: number | null;
  revenue_growth_latest_yoy_pct: number | null;
};

export async function screenAvailableCompanies(): Promise<{
  universe_count: number;
  as_of: string;
  companies: ScreenedCompany[];
  screening_note: string;
}> {
  const [universe, lens] = await Promise.all([
    listDatabaseCompaniesWithSector(),
    getSectorLens(),
  ]);

  const lensBySlug = new Map(lens.rows.map((r) => [r.slug, r]));
  const screened: ScreenedCompany[] = [];

  for (const c of universe) {
    const row = lensBySlug.get(c.slug);
    const reportYears = (row?.reportYears ?? []).sort((a, b) => b - a);
    const latestYear = row?.latestYear ?? reportYears[0] ?? null;
    const trendYears =
      latestYear != null
        ? [latestYear, latestYear - 1, latestYear - 2].filter((y) => y > 1990)
        : [];

    const [patByYear, revByYear] = await Promise.all([
      annualMetricByYear(c.slug, PAT_LABEL_TERMS, trendYears),
      annualMetricByYear(c.slug, REVENUE_LABEL_TERMS, trendYears),
    ]);

    const patKeys = Object.keys(patByYear).map(Number).sort((a, b) => b - a);
    const revKeys = Object.keys(revByYear).map(Number).sort((a, b) => b - a);
    const latestPat = patKeys.length ? patByYear[patKeys[0]] : null;
    const priorPat = patKeys.length > 1 ? patByYear[patKeys[1]] : null;
    const latestRev = revKeys.length ? revByYear[revKeys[0]] : null;
    const priorRev = revKeys.length > 1 ? revByYear[revKeys[1]] : null;

    const price = row?.priceD1 ?? null;
    const pe = row?.peRatio ?? null;
    const eps =
      price !== null && pe !== null && pe > 0 ? price / pe : null;

    screened.push({
      slug: c.slug,
      name: c.name ?? c.slug,
      sector: c.sector,
      sector_detail: c.sector_detail,
      latest_year: latestYear,
      report_years: reportYears,
      revenue_t12m: row?.revenueT12M ?? null,
      eps,
      pe_ratio: pe,
      market_cap: row?.marketCap ?? null,
      cce_lf: row?.cceLF ?? null,
      profit_after_tax_by_year: Object.fromEntries(
        Object.entries(patByYear).map(([y, v]) => [String(y), v]),
      ),
      revenue_by_year: Object.fromEntries(
        Object.entries(revByYear).map(([y, v]) => [String(y), v]),
      ),
      profit_growth_latest_yoy_pct: growthPct(latestPat, priorPat),
      revenue_growth_latest_yoy_pct: growthPct(latestRev, priorRev),
    });
  }

  screened.sort((a, b) => {
    const pa = a.profit_growth_latest_yoy_pct ?? -Infinity;
    const pb = b.profit_growth_latest_yoy_pct ?? -Infinity;
    if (pa !== pb) return pb - pa;
    return a.name.localeCompare(b.name);
  });

  return {
    universe_count: screened.length,
    as_of: lens.asOf,
    companies: screened,
    screening_note:
      "Use these figures to RANK and SELECT 1–2 companies to recommend — do NOT treat every company as equally good. " +
      "Compare profit/revenue trends, PE, EPS and sector outlook. Then call web_search for the top pick(s) to find recent articles and include markdown links.",
  };
}

export const INVESTMENT_SCREENING_TOOLS = [
  {
    type: "function" as const,
    function: {
      name: "screen_available_companies",
      description:
        "Screen EVERY company that has financial data in our database: latest revenue, profit-after-tax by year, YoY growth, EPS, PE ratio, market cap, sector. MANDATORY first step when the user asks what shares to buy, best stocks, investment picks, or buy recommendations — before answering. Use the results to pick 1–2 clear winners (not all companies). Follow with compare_companies and web_search for supporting articles.",
      parameters: {
        type: "object",
        properties: {},
        additionalProperties: false,
      },
    },
  },
] as const;

export const BUY_RECOMMENDATION_GUIDANCE = [
  "Buy / share recommendation questions (IMPORTANT — take time, use tools, be selective):",
  "  • When the user asks what shares to buy, best stocks, good investments, or buy ideas → call screen_available_companies FIRST. Do not rush the answer.",
  "  • Then use compare_companies on profit/revenue across the full universe if you need more detail for the latest years.",
  "  • Then call web_search for your top recommendation(s) — e.g. recent outlook, analyst view, news — and include clickable markdown links [Article title](url) when URLs are available.",
  "  • Answer honestly about scope: \"We only have data for **N** companies in our database. Based on their latest reported figures, I recommend **Company X** shares because …\" — cite specific numbers (profit growth %, revenue trend, PE, debt/cash) from the tools.",
  "  • Pick **1–2 clear winners** (or one per sector only if they asked broadly). Do NOT list all available companies as equally good buys.",
  "  • If metrics are mixed, say which is best on growth vs value and why you still prefer one pick.",
  "  • This is analysis from stored reports + public sources, not personal financial advice — add a brief disclaimer.",
].join("\n");

export async function dispatchInvestmentTool(
  name: string,
  _args: Record<string, unknown>,
): Promise<unknown | undefined> {
  if (name === "screen_available_companies") {
    return screenAvailableCompanies();
  }
  return undefined;
}
