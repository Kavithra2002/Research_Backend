import {
  NonFinancialData,
  type NonFinancialCategory,
} from "../models/NonFinancialData";

export type NonFinancialReport = {
  reportKey: string;
  year: number | null;
  reportingYear: string | null;
  reportGroup: string | null;
  foundCount: number;
  totalCount: number;
  categoryCount: number;
  model: string | null;
  extractedAt: string | null;
  uploadedAt: string | null;
};

export type NonFinancialCompany = {
  name: string;
  displayName: string;
  reports: NonFinancialReport[];
};

function prettifyCompanyName(slug: string) {
  return slug.replace(/_+/g, " ").replace(/\s+/g, " ").trim();
}

export async function countNonFinancialData(): Promise<number> {
  return NonFinancialData.estimatedDocumentCount();
}

/** List every stored non-financial record, ONE ROW PER REPORT, grouped by company. */
export async function listNonFinancialCompanies(): Promise<
  NonFinancialCompany[]
> {
  const docs = await NonFinancialData.find({})
    .select({
      company_slug: 1,
      company_name: 1,
      year: 1,
      report_key: 1,
      report_group: 1,
      reporting_year: 1,
      found_count: 1,
      total_count: 1,
      categories: 1,
      extraction_model: 1,
      extracted_at: 1,
      uploaded_at: 1,
    })
    .lean();

  const byCompany = new Map<
    string,
    { displayName: string; reports: NonFinancialReport[] }
  >();

  for (const doc of docs) {
    const slug = doc.company_slug;
    const displayName = doc.company_name || prettifyCompanyName(slug);
    if (!byCompany.has(slug)) {
      byCompany.set(slug, { displayName, reports: [] });
    }
    const categories = Array.isArray(doc.categories)
      ? (doc.categories as NonFinancialCategory[])
      : [];
    byCompany.get(slug)!.reports.push({
      reportKey: doc.report_key,
      year: doc.year ?? null,
      reportingYear: doc.reporting_year ?? null,
      reportGroup: doc.report_group ?? null,
      foundCount: doc.found_count ?? 0,
      totalCount: doc.total_count ?? 0,
      categoryCount: categories.length,
      model: doc.extraction_model ?? null,
      extractedAt: doc.extracted_at ?? null,
      uploadedAt: doc.uploaded_at ?? null,
    });
  }

  const companies: NonFinancialCompany[] = [];
  for (const [slug, node] of byCompany) {
    node.reports.sort((a, b) => {
      const ay = a.year ?? -Infinity;
      const by = b.year ?? -Infinity;
      return by - ay;
    });
    companies.push({
      name: slug,
      displayName: node.displayName,
      reports: node.reports,
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

/** Fetch ONE stored non-financial record (by company + report key). */
export async function getNonFinancialData(
  companySlug: string,
  reportKey: string,
): Promise<{
  company: string;
  displayName: string;
  reportKey: string;
  meta: Record<string, unknown>;
  reportingYear: string | null;
  companyOverview: string;
  categories: NonFinancialCategory[];
  foundCount: number;
  totalCount: number;
}> {
  const doc = await NonFinancialData.findOne({
    company_slug: companySlug,
    report_key: reportKey,
  }).lean();

  if (!doc) {
    const err = new Error("Non-financial data not found");
    (err as Error & { statusCode?: number }).statusCode = 404;
    throw err;
  }

  const categories = Array.isArray(doc.categories)
    ? (doc.categories as NonFinancialCategory[])
    : [];

  return {
    company: companySlug,
    displayName: doc.company_name || prettifyCompanyName(companySlug),
    reportKey: doc.report_key,
    meta: {
      company: doc.company_name ?? companySlug,
      model: doc.extraction_model ?? null,
      generated_at: doc.extracted_at ?? null,
      uploaded_at: doc.uploaded_at ?? null,
      year: doc.year ?? null,
      report_group: doc.report_group ?? null,
      source: "mongodb",
    },
    reportingYear: doc.reporting_year ?? null,
    companyOverview: doc.company_overview ?? "",
    categories,
    foundCount: doc.found_count ?? 0,
    totalCount: doc.total_count ?? 0,
  };
}
