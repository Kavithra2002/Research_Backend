import { FinancialTable, type ReportType } from "../models/FinancialTable";
import { Company } from "../models/Company";
import {
  getCseSnapshots,
  getCseUniverse,
  type CseSnapshot,
  type CseLiveRow,
} from "./cseMarketService";

export type PeriodLabel = "Annual" | "Quarterly";

export type PeriodSummary = {
  statements: string[];
  statementCount: number;
  tableCount: number;
  model: string | null;
  generatedAt: string | null;
};

export type YearNode = {
  year: number;
  annual: PeriodSummary | null;
  quarterly: PeriodSummary | null;
  availablePeriods: PeriodLabel[];
};

export type ExtractedCompanyNode = {
  name: string;
  displayName: string;
  sector: string | null;
  sectorDetail: string | null;
  years: YearNode[];
  /** Flat union for quick period toggles when year is not selected yet */
  hasAnnual: boolean;
  hasQuarterly: boolean;
  availablePeriods: PeriodLabel[];
};

function prettifyCompanyName(slug: string) {
  return slug.replace(/_+/g, " ").replace(/\s+/g, " ").trim();
}

function periodFromReportType(reportType: string): PeriodLabel | null {
  const t = reportType.toLowerCase();
  if (t === "annual") return "Annual";
  if (t === "quarterly") return "Quarterly";
  return null;
}

function reportTypeFromPeriod(period: PeriodLabel): ReportType {
  return period === "Quarterly" ? "quarterly" : "annual";
}

export async function countFinancialTables(): Promise<number> {
  return FinancialTable.estimatedDocumentCount();
}

export async function listExtractedCompanies(): Promise<ExtractedCompanyNode[]> {
  const [registry, grouped] = await Promise.all([
    Company.find({}).select({ slug: 1, name: 1, sector: 1, sector_detail: 1 }).lean(),
    FinancialTable.aggregate<{
      _id: {
        company_slug: string;
        year: number | null;
        report_type: string;
      };
      company_name: string;
      statements: string[];
      table_count: number;
      model: string | null;
      generated_at: string | null;
    }>([
      { $match: { year: { $ne: null } } },
      {
        $group: {
          _id: {
            company_slug: "$company_slug",
            year: "$year",
            report_type: "$report_type",
          },
          company_name: { $first: "$company_name" },
          statements: { $addToSet: "$statement_key" },
          table_count: { $sum: 1 },
          model: { $first: "$extraction_model" },
          generated_at: { $max: "$extracted_at" },
        },
      },
    ]),
  ]);

  const displayNames = new Map<string, string>();
  const sectors = new Map<string, { sector: string | null; detail: string | null }>();
  for (const c of registry) {
    displayNames.set(c.slug, c.name);
    sectors.set(c.slug, {
      sector: (c as { sector?: string | null }).sector ?? null,
      detail: (c as { sector_detail?: string | null }).sector_detail ?? null,
    });
  }

  const byCompany = new Map<
    string,
    { displayName: string; years: Map<number, { annual: PeriodSummary | null; quarterly: PeriodSummary | null }> }
  >();

  for (const row of grouped) {
    const slug = row._id.company_slug;
    const year = row._id.year;
    if (year == null) continue;
    const period = periodFromReportType(row._id.report_type);
    if (!period) continue;

    const displayName =
      displayNames.get(slug) ?? row.company_name ?? prettifyCompanyName(slug);
    displayNames.set(slug, displayName);

    if (!byCompany.has(slug)) {
      byCompany.set(slug, { displayName, years: new Map() });
    }
    const companyNode = byCompany.get(slug)!;
    if (!companyNode.years.has(year)) {
      companyNode.years.set(year, { annual: null, quarterly: null });
    }
    const yearNode = companyNode.years.get(year)!;
    const statements = [...row.statements].sort();
    const summary: PeriodSummary = {
      statements,
      statementCount: statements.length,
      tableCount: row.table_count,
      model: row.model ?? null,
      generatedAt: row.generated_at ?? null,
    };

    if (period === "Annual") yearNode.annual = summary;
    else yearNode.quarterly = summary;
  }

  // Include registry-only companies (no tables yet).
  for (const c of registry) {
    if (!byCompany.has(c.slug)) {
      byCompany.set(c.slug, {
        displayName: c.name,
        years: new Map(),
      });
    }
  }

  const companies: ExtractedCompanyNode[] = [];
  for (const [slug, node] of byCompany) {
    const years: YearNode[] = [...node.years.entries()]
      .sort(([a], [b]) => b - a)
      .map(([year, periods]) => {
        const availablePeriods: PeriodLabel[] = [];
        if (periods.annual) availablePeriods.push("Annual");
        if (periods.quarterly) availablePeriods.push("Quarterly");
        return {
          year,
          annual: periods.annual,
          quarterly: periods.quarterly,
          availablePeriods,
        };
      });

    const hasAnnual = years.some((y) => y.annual != null);
    const hasQuarterly = years.some((y) => y.quarterly != null);
    const availablePeriods: PeriodLabel[] = [];
    if (hasAnnual) availablePeriods.push("Annual");
    if (hasQuarterly) availablePeriods.push("Quarterly");

    const sectorInfo = sectors.get(slug);
    companies.push({
      name: slug,
      displayName: node.displayName,
      sector: sectorInfo?.sector ?? null,
      sectorDetail: sectorInfo?.detail ?? null,
      years,
      hasAnnual,
      hasQuarterly,
      availablePeriods,
    });
  }

  companies.sort((a, b) =>
    a.displayName.localeCompare(b.displayName, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );

  return companies;
}

// ─────────────────────────────────────────────────────────────────────────────
// Sector Lens
// ─────────────────────────────────────────────────────────────────────────────

export type SectorLensMetric = number | null;

export type SectorLensRow = {
  slug: string;
  name: string;
  sector: string | null;
  sectorDetail: string | null;
  /** Most recent reporting year we hold tables for. */
  latestYear: number | null;
  reportYears: number[];
  /** Bloomberg-style market metrics — null when we have no live market data. */
  peRatio: SectorLensMetric;
  revenueT12M: SectorLensMetric;
  cceLF: SectorLensMetric;
  marketCap: SectorLensMetric;
  priceD1: SectorLensMetric;
  totalReturnYTD: SectorLensMetric;
};

export type SectorLensPayload = {
  asOf: string;
  universeCount: number;
  rows: SectorLensRow[];
};

/**
 * Resolve the "As of" date for the screener. Accepts a date string (e.g.
 * "YYYY-MM-DD" or an ISO timestamp) and returns a normalised ISO string. Falls
 * back to the current time when the input is missing or unparseable.
 */
function resolveAsOf(asOf?: string): string {
  if (asOf) {
    const parsed = new Date(asOf);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
  }
  return new Date().toISOString();
}

type SectorLensTableDoc = {
  company_slug: string;
  year: number | null;
  report_type: string;
  quarter: string | null;
  statement_key: string;
  statement_title: string;
  rows: Array<{ cells?: unknown }>;
};

const INCOME_STMT = /income|profit.?loss|statement.of.comprehensive|revenue/i;
const BALANCE_STMT = /sofp|balance|financial.position/i;

const SECONDARY_STMT = /ten_year|investor|shareholder|note|summary/i;

const REVENUE_TERMS = [
  "total revenue",
  "group revenue",
  "revenue from contracts",
  "total income",
  "net interest income",
  "interest income",
  "turnover",
  "revenue",
  "sales",
];

const CCE_TERMS = [
  "cash and cash equivalents",
  "cash & cash equivalents",
  "cash equivalents",
];

const EPS_TERMS = [
  "earnings per share",
  "earning per share",
  "eps",
];

const LABEL_NOISE =
  /per share|margin|ratio|growth|expense|cost of|segment|note |%|\(\*\)/i;

function parseNumeric(value: unknown): number | null {
  const m = String(value ?? "")
    .replace(/,/g, "")
    .match(/-?\d+(?:\.\d+)?/);
  if (!m) return null;
  const n = Number(m[0]);
  return Number.isFinite(n) ? n : null;
}

/** Pick a figure from amount columns, skipping note refs and trailing % columns. */
function extractMonetaryValue(cells: unknown[]): number | null {
  const nums = cells
    .slice(1)
    .map(parseNumeric)
    .filter((n): n is number => n !== null);
  if (!nums.length) return null;

  const filtered = nums.filter((n) => {
    const looksLikeNote = Number.isInteger(n) && n >= 1 && n <= 99;
    const hasSubstantial = nums.some((v) => Math.abs(v) >= 1000);
    if (looksLikeNote && hasSubstantial) return false;
    return true;
  });

  const substantial = filtered.filter((n) => Math.abs(n) >= 1000);
  if (substantial.length) return substantial[0];

  const withoutPctNoise = filtered.filter((n) => {
    if (Math.abs(n) > 200) return true;
    return !filtered.some((v) => Math.abs(v) >= 1000);
  });
  if (!withoutPctNoise.length) return null;
  return withoutPctNoise.reduce((best, n) =>
    Math.abs(n) > Math.abs(best) ? n : best,
  );
}

/** EPS and similar per-share figures are small numbers in the last amount column. */
function extractPerShareValue(cells: unknown[]): number | null {
  const nums = cells
    .slice(1)
    .map(parseNumeric)
    .filter((n): n is number => n !== null);
  if (!nums.length) return null;

  const candidates = nums.filter((n) => n > 0 && n < 10_000);
  if (candidates.length) return candidates[candidates.length - 1];
  return nums[nums.length - 1] ?? null;
}

function labelMatches(label: string, terms: string[]): boolean {
  const l = label.toLowerCase().trim();
  if (!l || LABEL_NOISE.test(l)) return false;
  return terms.some((term) => l.includes(term.toLowerCase()));
}

function statementScore(
  doc: SectorLensTableDoc,
  preferred: RegExp,
): number {
  const text = `${doc.statement_key} ${doc.statement_title}`;
  let score = preferred.test(text) ? 20 : 0;
  if (SECONDARY_STMT.test(text)) score -= 25;
  return score;
}

function sortTablesNewestFirst(a: SectorLensTableDoc, b: SectorLensTableDoc): number {
  const yearDiff = (b.year ?? 0) - (a.year ?? 0);
  if (yearDiff !== 0) return yearDiff;
  const typeDiff =
    (b.report_type === "quarterly" ? 1 : 0) - (a.report_type === "quarterly" ? 1 : 0);
  if (typeDiff !== 0) return typeDiff;
  return quarterRank(b.quarter) - quarterRank(a.quarter);
}

function extractLineItemFromDoc(
  doc: SectorLensTableDoc,
  terms: string[],
  preferredStatement: RegExp,
  mode: "amount" | "perShare" = "amount",
): number | null {
  let best: { score: number; value: number } | null = null;
  const stmtBonus = statementScore(doc, preferredStatement);
  const pickValue =
    mode === "perShare" ? extractPerShareValue : extractMonetaryValue;

  for (const row of doc.rows ?? []) {
    const cells = row?.cells;
    if (!Array.isArray(cells) || cells.length === 0) continue;
    const label = String(cells[0] ?? "");
    if (!labelMatches(label, terms)) continue;

    const value = pickValue(cells);
    if (value === null) continue;

    const ll = label.toLowerCase();
    let termScore = 0;
    for (let i = 0; i < terms.length; i += 1) {
      if (ll.includes(terms[i].toLowerCase())) {
        termScore = Math.max(termScore, 100 - i);
      }
    }

    const score =
      stmtBonus +
      termScore +
      (doc.year ?? 0) * 0.001 +
      quarterRank(doc.quarter) * 0.01;
    if (!best || score > best.score) {
      best = { score, value };
    }
  }

  return best?.value ?? null;
}

function findBestLineItem(
  tables: SectorLensTableDoc[],
  terms: string[],
  preferredStatement: RegExp,
  reportType?: ReportType,
  mode: "amount" | "perShare" = "amount",
  maxYear?: number | null,
): number | null {
  const sorted = tables
    .filter((doc) => doc.year != null)
    .filter((doc) => maxYear == null || (doc.year ?? 0) <= maxYear)
    .filter((doc) => !reportType || doc.report_type === reportType)
    .sort(sortTablesNewestFirst);

  let best: { score: number; value: number } | null = null;
  for (const doc of sorted) {
    const value = extractLineItemFromDoc(
      doc,
      terms,
      preferredStatement,
      mode,
    );
    if (value === null) continue;
    const score =
      statementScore(doc, preferredStatement) +
      (doc.year ?? 0) * 0.001 +
      quarterRank(doc.quarter) * 0.01;
    if (!best || score > best.score) {
      best = { score, value };
    }
  }
  return best?.value ?? null;
}

function computeRevenueT12M(
  tables: SectorLensTableDoc[],
  maxYear?: number | null,
): number | null {
  const quarterly = tables
    .filter((doc) => doc.report_type === "quarterly" && doc.year != null)
    .filter((doc) => maxYear == null || (doc.year ?? 0) <= maxYear)
    .sort(sortTablesNewestFirst);

  const values: number[] = [];
  const seen = new Set<string>();

  for (const doc of quarterly) {
    const key = `${doc.year}:${doc.quarter ?? ""}`;
    if (seen.has(key)) continue;
    const value = extractLineItemFromDoc(doc, REVENUE_TERMS, INCOME_STMT);
    if (value === null) continue;
    values.push(value);
    seen.add(key);
    if (values.length >= 4) break;
  }

  if (values.length >= 4) {
    return values.slice(0, 4).reduce((sum, value) => sum + value, 0);
  }

  return findBestLineItem(
    tables,
    REVENUE_TERMS,
    INCOME_STMT,
    "annual",
    "amount",
    maxYear,
  );
}

function computeCceLF(
  tables: SectorLensTableDoc[],
  maxYear?: number | null,
): number | null {
  return findBestLineItem(
    tables,
    CCE_TERMS,
    BALANCE_STMT,
    undefined,
    "amount",
    maxYear,
  );
}

function computeEps(
  tables: SectorLensTableDoc[],
  maxYear?: number | null,
): number | null {
  return findBestLineItem(
    tables,
    EPS_TERMS,
    INCOME_STMT,
    undefined,
    "perShare",
    maxYear,
  );
}

type CompanyMetrics = {
  revenueT12M: number | null;
  cceLF: number | null;
  eps: number | null;
  /** The most recent reporting year at or before the requested cut-off. */
  effectiveYear: number | null;
};

function buildFinancialMetricsBySlug(
  tables: SectorLensTableDoc[],
  maxYear?: number | null,
): Map<string, CompanyMetrics> {
  const bySlug = new Map<string, SectorLensTableDoc[]>();
  for (const doc of tables) {
    const list = bySlug.get(doc.company_slug) ?? [];
    list.push(doc);
    bySlug.set(doc.company_slug, list);
  }

  const out = new Map<string, CompanyMetrics>();
  for (const [slug, docs] of bySlug) {
    const eligibleYears = docs
      .map((d) => d.year)
      .filter((y): y is number => typeof y === "number")
      .filter((y) => maxYear == null || y <= maxYear);
    const effectiveYear = eligibleYears.length ? Math.max(...eligibleYears) : null;

    out.set(slug, {
      revenueT12M: computeRevenueT12M(docs, maxYear),
      cceLF: computeCceLF(docs, maxYear),
      eps: computeEps(docs, maxYear),
      effectiveYear,
    });
  }
  return out;
}

function buildCseByName(snapshots: CseSnapshot[]): Map<string, CseSnapshot> {
  const out = new Map<string, CseSnapshot>();
  for (const snap of snapshots) {
    out.set(snap.query, snap);
  }
  return out;
}

/**
 * Build the Sector Lens screener rows: every company in the registry with its
 * classified sector, reporting years, live CSE market figures, and fundamentals
 * pulled from extracted financial statements.
 */
export async function getSectorLens(asOf?: string): Promise<SectorLensPayload> {
  const registry = await Company.find({})
    .select({ slug: 1, name: 1, sector: 1, sector_detail: 1 })
    .lean();

  const slugs = registry.map((c) => c.slug);
  const [grouped, financialTables, cseSnapshots] = await Promise.all([
    FinancialTable.aggregate<{
      _id: string;
      years: number[];
    }>([
      { $match: { company_slug: { $in: slugs }, year: { $ne: null } } },
      { $group: { _id: "$company_slug", years: { $addToSet: "$year" } } },
    ]),
    slugs.length > 0
      ? FinancialTable.find({ company_slug: { $in: slugs } })
          .select({
            company_slug: 1,
            year: 1,
            report_type: 1,
            quarter: 1,
            statement_key: 1,
            statement_title: 1,
            rows: 1,
          })
          .lean()
      : Promise.resolve([]),
    getCseSnapshots(registry.map((c) => c.name)),
  ]);

  const yearsBySlug = new Map<string, number[]>();
  for (const g of grouped) {
    const years = (g.years ?? [])
      .filter((y): y is number => typeof y === "number")
      .sort((a, b) => b - a);
    yearsBySlug.set(g._id, years);
  }

  // The calendar's "as of" date selects which reporting year to show: we use the
  // most recent report at or before that calendar year, so picking a past date
  // surfaces the figures that were the latest known as of then.
  const asOfIso = resolveAsOf(asOf);
  const cutoffYear = new Date(asOfIso).getUTCFullYear();

  const metricsBySlug = buildFinancialMetricsBySlug(
    financialTables as SectorLensTableDoc[],
    cutoffYear,
  );
  const cseByName = buildCseByName(cseSnapshots);

  const rows: SectorLensRow[] = registry.map((c) => {
    const years = yearsBySlug.get(c.slug) ?? [];
    const metrics = metricsBySlug.get(c.slug);
    const cse = cseByName.get(c.name) ?? null;
    const eps = metrics?.eps ?? null;
    const price = cse?.price ?? null;
    const peRatio =
      price !== null && eps !== null && eps > 0 ? price / eps : null;

    return {
      slug: c.slug,
      name: c.name,
      sector: (c as { sector?: string | null }).sector ?? null,
      sectorDetail: (c as { sector_detail?: string | null }).sector_detail ?? null,
      latestYear: metrics?.effectiveYear ?? years[0] ?? null,
      reportYears: years,
      peRatio,
      revenueT12M: metrics?.revenueT12M ?? null,
      cceLF: metrics?.cceLF ?? null,
      marketCap: cse?.marketCap ?? null,
      priceD1: cse?.previousClose ?? null,
      totalReturnYTD: null,
    };
  });

  rows.sort((a, b) => {
    const sa = a.sector ?? "ZZZ";
    const sb = b.sector ?? "ZZZ";
    if (sa !== sb) return sa.localeCompare(sb);
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  });

  return {
    asOf: resolveAsOf(asOf),
    universeCount: rows.length,
    rows,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Sector Lens — Live tab (entire CSE universe, live figures only)
// ─────────────────────────────────────────────────────────────────────────────

export type SectorLensLivePayload = {
  asOf: string;
  universeCount: number;
  rows: CseLiveRow[];
};

/**
 * Build the Sector Lens "Live" tab: every equity currently listed on the CSE
 * with its live trade figures. No DB / historical data is involved, so there is
 * no date scoping here — the calendar does not apply to this view.
 */
export async function getSectorLensLive(): Promise<SectorLensLivePayload> {
  const rows = await getCseUniverse();
  return {
    asOf: new Date().toISOString(),
    universeCount: rows.length,
    rows,
  };
}

export type StoredReport = {
  reportKey: string;
  reportType: ReportType;
  period: PeriodLabel;
  year: number | null;
  quarter: string | null;
  reportGroup: string | null;
  periodLabel: string | null;
  statements: string[];
  statementCount: number;
  tableCount: number;
  model: string | null;
  generatedAt: string | null;
};

export type StoredReportCompany = {
  name: string;
  displayName: string;
  reports: StoredReport[];
};

function quarterRank(quarter: string | null): number {
  if (!quarter) return 0;
  const m = /([1-4])/.exec(quarter);
  return m ? Number(m[1]) : 0;
}

/**
 * List every stored report ONE ROW PER REPORT (i.e. per `report_key`), so the
 * four quarters of a year stay distinct. Ordered Annual-first, then Quarterly,
 * newest year first, then by quarter.
 */
export async function listStoredReports(): Promise<StoredReportCompany[]> {
  const [registry, grouped] = await Promise.all([
    Company.find({}).select({ slug: 1, name: 1 }).lean(),
    FinancialTable.aggregate<{
      _id: { company_slug: string; report_type: string; report_key: string };
      company_name: string;
      year: number | null;
      quarter: string | null;
      report_group: string | null;
      period_label: string | null;
      statements: string[];
      table_count: number;
      model: string | null;
      generated_at: string | null;
    }>([
      {
        $group: {
          _id: {
            company_slug: "$company_slug",
            report_type: "$report_type",
            report_key: "$report_key",
          },
          company_name: { $first: "$company_name" },
          year: { $max: "$year" },
          quarter: { $first: "$quarter" },
          report_group: { $first: "$report_group" },
          period_label: { $first: "$period_label" },
          statements: { $addToSet: "$statement_key" },
          table_count: { $sum: 1 },
          model: { $first: "$extraction_model" },
          generated_at: { $max: "$extracted_at" },
        },
      },
    ]),
  ]);

  const displayNames = new Map<string, string>();
  for (const c of registry) displayNames.set(c.slug, c.name);

  const byCompany = new Map<
    string,
    { displayName: string; reports: StoredReport[] }
  >();

  for (const row of grouped) {
    const slug = row._id.company_slug;
    const reportType = row._id.report_type as ReportType;
    const period = periodFromReportType(reportType);
    if (!period) continue;

    const displayName =
      displayNames.get(slug) ?? row.company_name ?? prettifyCompanyName(slug);
    displayNames.set(slug, displayName);

    if (!byCompany.has(slug)) {
      byCompany.set(slug, { displayName, reports: [] });
    }

    const statements = [...row.statements].sort();
    byCompany.get(slug)!.reports.push({
      reportKey: row._id.report_key,
      reportType,
      period,
      year: row.year ?? null,
      quarter: row.quarter ?? null,
      reportGroup: row.report_group ?? null,
      periodLabel: row.period_label ?? null,
      statements,
      statementCount: statements.length,
      tableCount: row.table_count,
      model: row.model ?? null,
      generatedAt: row.generated_at ?? null,
    });
  }

  for (const c of registry) {
    if (!byCompany.has(c.slug)) {
      byCompany.set(c.slug, { displayName: c.name, reports: [] });
    }
  }

  const companies: StoredReportCompany[] = [];
  for (const [slug, node] of byCompany) {
    node.reports.sort((a, b) => {
      // Annual before Quarterly.
      if (a.period !== b.period) return a.period === "Annual" ? -1 : 1;
      // Newest year first.
      const ay = a.year ?? -Infinity;
      const by = b.year ?? -Infinity;
      if (ay !== by) return by - ay;
      // Then by quarter (Q1 → Q4).
      return quarterRank(a.quarter) - quarterRank(b.quarter);
    });
    companies.push({ name: slug, displayName: node.displayName, reports: node.reports });
  }

  companies.sort((a, b) =>
    a.displayName.localeCompare(b.displayName, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );

  return companies;
}

/** Build the statements/results map for ONE stored report (by report_key). */
export async function getReportTables(
  companySlug: string,
  reportType: ReportType,
  reportKey: string,
): Promise<{
  company: string;
  reportType: ReportType;
  reportKey: string;
  meta: Record<string, unknown> | null;
  results: Record<string, unknown>;
}> {
  const docs = await FinancialTable.find({
    company_slug: companySlug,
    report_type: reportType,
    report_key: reportKey,
  })
    .sort({ statement_key: 1, table_index: 1 })
    .lean();

  if (docs.length === 0) {
    const err = new Error("Stored report not found");
    (err as Error & { statusCode?: number }).statusCode = 404;
    throw err;
  }

  const results: Record<string, unknown> = {};
  let model: string | null = null;
  let generatedAt: string | null = null;
  let companyName: string | null = null;
  let year: number | null = null;
  let quarter: string | null = null;
  let periodLabel: string | null = null;

  for (const doc of docs) {
    companyName = doc.company_name;
    model = model ?? doc.extraction_model ?? null;
    year = year ?? doc.year ?? null;
    quarter = quarter ?? doc.quarter ?? null;
    periodLabel = periodLabel ?? doc.period_label ?? null;
    if (doc.extracted_at && (!generatedAt || doc.extracted_at > generatedAt)) {
      generatedAt = doc.extracted_at;
    }

    const key = doc.statement_key;
    let stmt = results[key] as Record<string, unknown> | undefined;
    if (!stmt) {
      stmt = {
        status: doc.extraction_status || "ok",
        title: doc.statement_label ?? doc.statement_title,
        data: {
          statement_title: doc.statement_title,
          preamble: doc.preamble ?? "",
          footnotes: doc.footnotes ?? "",
          tables: [] as unknown[],
        },
      };
      results[key] = stmt;
    }

    const data = stmt.data as Record<string, unknown>;
    const tables = data.tables as unknown[];
    tables.push({
      caption: doc.caption,
      header_rows: doc.header_rows ?? [],
      rows: doc.rows ?? [],
    });
  }

  return {
    company: companySlug,
    reportType,
    reportKey,
    meta: {
      company: companyName ?? companySlug,
      model,
      generated_at: generatedAt,
      year,
      quarter,
      period_label: periodLabel,
      source: "mongodb",
    },
    results,
  };
}

export async function getExtractedResults(
  companySlug: string,
  year: number,
  period: PeriodLabel,
): Promise<{
  company: string;
  year: number;
  period: PeriodLabel;
  meta: Record<string, unknown> | null;
  results: Record<string, unknown>;
}> {
  const reportType = reportTypeFromPeriod(period);
  const docs = await FinancialTable.find({
    company_slug: companySlug,
    year,
    report_type: reportType,
  })
    .sort({ statement_key: 1, table_index: 1 })
    .lean();

  if (docs.length === 0) {
    const err = new Error("Extracted results not found");
    (err as Error & { statusCode?: number }).statusCode = 404;
    throw err;
  }

  const results: Record<string, unknown> = {};
  let model: string | null = null;
  let generatedAt: string | null = null;
  let companyName: string | null = null;

  for (const doc of docs) {
    companyName = doc.company_name;
    model = model ?? doc.extraction_model ?? null;
    if (doc.extracted_at && (!generatedAt || doc.extracted_at > generatedAt)) {
      generatedAt = doc.extracted_at;
    }

    const key = doc.statement_key;
    let stmt = results[key] as Record<string, unknown> | undefined;
    if (!stmt) {
      stmt = {
        status: doc.extraction_status || "ok",
        title: doc.statement_label ?? doc.statement_title,
        data: {
          statement_title: doc.statement_title,
          preamble: doc.preamble ?? "",
          footnotes: doc.footnotes ?? "",
          tables: [] as unknown[],
        },
      };
      results[key] = stmt;
    }

    const data = stmt.data as Record<string, unknown>;
    const tables = data.tables as unknown[];
    tables.push({
      caption: doc.caption,
      header_rows: doc.header_rows ?? [],
      rows: doc.rows ?? [],
    });
  }

  return {
    company: companySlug,
    year,
    period,
    meta: {
      company: companyName ?? companySlug,
      model,
      generated_at: generatedAt,
      year,
      period,
      source: "mongodb",
    },
    results,
  };
}
