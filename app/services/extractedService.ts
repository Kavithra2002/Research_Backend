import { FinancialTable, type ReportType } from "../models/FinancialTable";
import { Company } from "../models/Company";

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
 * Build the Sector Lens screener rows: every company in the registry with its
 * classified sector and the years of data we hold. Market metrics (P/E, market
 * cap, price, returns) are sourced from a live market feed which the demo does
 * NOT have, so they are returned as null and rendered as "-" in the UI.
 */
export async function getSectorLens(): Promise<SectorLensPayload> {
  const [registry, grouped] = await Promise.all([
    Company.find({})
      .select({ slug: 1, name: 1, sector: 1, sector_detail: 1 })
      .lean(),
    FinancialTable.aggregate<{
      _id: string;
      years: number[];
    }>([
      { $match: { year: { $ne: null } } },
      { $group: { _id: "$company_slug", years: { $addToSet: "$year" } } },
    ]),
  ]);

  const yearsBySlug = new Map<string, number[]>();
  for (const g of grouped) {
    const years = (g.years ?? [])
      .filter((y): y is number => typeof y === "number")
      .sort((a, b) => b - a);
    yearsBySlug.set(g._id, years);
  }

  const rows: SectorLensRow[] = registry.map((c) => {
    const years = yearsBySlug.get(c.slug) ?? [];
    return {
      slug: c.slug,
      name: c.name,
      sector: (c as { sector?: string | null }).sector ?? null,
      sectorDetail: (c as { sector_detail?: string | null }).sector_detail ?? null,
      latestYear: years[0] ?? null,
      reportYears: years,
      peRatio: null,
      revenueT12M: null,
      cceLF: null,
      marketCap: null,
      priceD1: null,
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
