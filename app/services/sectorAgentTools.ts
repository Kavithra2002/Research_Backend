import { Company } from "../models/Company";
import { FinancialTable } from "../models/FinancialTable";

/* ────────────────────────────────────────────────────────────────────────── *
 * Sector discovery tools — shared by Robin, Marian, Tuck, and Sage.
 *
 * Lets agents list which companies exist in the DB and filter them by sector
 * before answering sector-wise questions (buy ideas, comparisons, etc.).
 * ────────────────────────────────────────────────────────────────────────── */

export type CompanySectorRef = {
  slug: string;
  name: string | null;
  sector: string | null;
  sector_detail: string | null;
  has_financial_data: boolean;
};

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Slugs that have at least one row in financial_tables. */
async function financialDataSlugs(): Promise<Set<string>> {
  const slugs = await FinancialTable.distinct("company_slug");
  return new Set(
    slugs.filter((s): s is string => typeof s === "string" && s.length > 0),
  );
}

/** Registry + financial-table companies with sector metadata. */
export async function listDatabaseCompaniesWithSector(): Promise<
  CompanySectorRef[]
> {
  const dataSlugs = await financialDataSlugs();
  if (dataSlugs.size === 0) return [];

  const [registryDocs, tableNames] = await Promise.all([
    Company.find({ slug: { $in: [...dataSlugs] } })
      .select({ slug: 1, name: 1, sector: 1, sector_detail: 1 })
      .lean(),
    FinancialTable.aggregate<{ _id: string; name: string | null }>([
      { $match: { company_slug: { $in: [...dataSlugs] } } },
      { $group: { _id: "$company_slug", name: { $first: "$company_name" } } },
    ]),
  ]);

  const bySlug = new Map<string, CompanySectorRef>();
  for (const c of registryDocs) {
    bySlug.set(c.slug, {
      slug: c.slug,
      name: c.name ?? null,
      sector: (c as { sector?: string | null }).sector ?? null,
      sector_detail: (c as { sector_detail?: string | null }).sector_detail ?? null,
      has_financial_data: true,
    });
  }
  for (const row of tableNames) {
    const existing = bySlug.get(row._id);
    if (existing) {
      if (!existing.name) existing.name = row.name ?? null;
    } else {
      bySlug.set(row._id, {
        slug: row._id,
        name: row.name ?? null,
        sector: null,
        sector_detail: null,
        has_financial_data: true,
      });
    }
  }

  return [...bySlug.values()].sort((a, b) =>
    (a.name ?? a.slug).localeCompare(b.name ?? b.slug, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );
}

function sectorMatches(query: string, sector: string | null): boolean {
  if (!sector) return false;
  const q = query.trim().toLowerCase();
  const s = sector.trim().toLowerCase();
  if (!q) return false;
  if (s.includes(q) || q.includes(s)) return true;
  // "telecommunication" ↔ "telecommunications"
  const stripS = (v: string) => (v.endsWith("s") ? v.slice(0, -1) : v);
  return stripS(s).includes(stripS(q)) || stripS(q).includes(stripS(s));
}

export async function listSectorsInDatabase(): Promise<{
  sectors: Array<{
    sector: string;
    company_count: number;
    companies: Array<{ slug: string; name: string | null }>;
  }>;
  unclassified_count: number;
  total_companies_with_data: number;
}> {
  const companies = await listDatabaseCompaniesWithSector();
  const bySector = new Map<string, Array<{ slug: string; name: string | null }>>();
  let unclassified = 0;

  for (const c of companies) {
    if (!c.sector?.trim()) {
      unclassified += 1;
      continue;
    }
    const key = c.sector.trim();
    if (!bySector.has(key)) bySector.set(key, []);
    bySector.get(key)!.push({ slug: c.slug, name: c.name });
  }

  const sectors = [...bySector.entries()]
    .map(([sector, list]) => ({
      sector,
      company_count: list.length,
      companies: list,
    }))
    .sort((a, b) => a.sector.localeCompare(b.sector));

  return {
    sectors,
    unclassified_count: unclassified,
    total_companies_with_data: companies.length,
  };
}

export async function listCompaniesBySector(sectorQuery: string): Promise<{
  sector_query: string;
  matched_sectors: string[];
  companies: CompanySectorRef[];
  available_sectors: string[];
  note?: string;
}> {
  const q = sectorQuery.trim();
  if (!q) {
    const { sectors } = await listSectorsInDatabase();
    return {
      sector_query: q,
      matched_sectors: [],
      companies: [],
      available_sectors: sectors.map((s) => s.sector),
      note: "A sector name is required. Pass the sector the user asked about.",
    };
  }

  const all = await listDatabaseCompaniesWithSector();
  const matched = all.filter((c) => sectorMatches(q, c.sector));
  const matchedSectors = [
    ...new Set(
      matched
        .map((c) => c.sector?.trim())
        .filter((s): s is string => Boolean(s)),
    ),
  ];
  const available_sectors = [
    ...new Set(
      all
        .map((c) => c.sector?.trim())
        .filter((s): s is string => Boolean(s)),
    ),
  ].sort();

  return {
    sector_query: q,
    matched_sectors: matchedSectors,
    companies: matched,
    available_sectors,
    ...(matched.length === 0
      ? {
          note:
            "No companies in the database matched this sector. Use available_sectors for what is stored, or list_sectors for the full breakdown.",
        }
      : {}),
  };
}

/** Optional name/slug filter on top of sector listing. */
export async function filterDatabaseCompanies(args: {
  sector?: string;
  query?: string;
}): Promise<{ companies: CompanySectorRef[] }> {
  let companies = await listDatabaseCompaniesWithSector();

  if (args.sector?.trim()) {
    companies = companies.filter((c) => sectorMatches(args.sector!, c.sector));
  }

  const q = args.query?.trim();
  if (q) {
    const rx = new RegExp(escapeRegex(q), "i");
    companies = companies.filter(
      (c) => rx.test(c.name ?? "") || rx.test(c.slug),
    );
  }

  return { companies };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool schemas
 * ────────────────────────────────────────────────────────────────────────── */

export const SECTOR_DB_TOOLS = [
  {
    type: "function" as const,
    function: {
      name: "list_sectors",
      description:
        "List every business sector represented by companies that have financial data in the database, with the company names under each sector. Call this FIRST when the user asks what sectors you cover, which companies are in a sector, sector-wise buy recommendations, or 'you tell me' after a sector question. Never claim a sector is empty without calling this or list_companies_by_sector.",
      parameters: {
        type: "object",
        properties: {},
        additionalProperties: false,
      },
    },
  },
  {
    type: "function" as const,
    function: {
      name: "list_companies_by_sector",
      description:
        "List companies in the database that belong to a given sector (e.g. 'Telecommunications', 'Banks', 'telecommunication'). Matching is case-insensitive and tolerates singular/plural. Use before answering sector-specific financial questions, buy suggestions, or comparisons. Then pull figures with search_line_items / compare_companies for the returned companies.",
      parameters: {
        type: "object",
        properties: {
          sector: {
            type: "string",
            description:
              "Sector name or fragment from the user's question, e.g. 'Telecommunications', 'banking', 'tobacco'.",
          },
        },
        required: ["sector"],
        additionalProperties: false,
      },
    },
  },
] as const;

export const SECTOR_QUERY_GUIDANCE = [
  "Sector-wise questions (IMPORTANT — mandatory tool flow):",
  "  • When the user asks about a SECTOR (buy ideas, best companies, comparisons, 'what do you have in telecom', 'you tell me' after a sector question) → call list_companies_by_sector with their sector FIRST, or list_sectors to see the full universe.",
  "  • NEVER say a sector has no companies without calling list_sectors or list_companies_by_sector. The database may use a slightly different label (e.g. 'Telecommunications' vs 'telecommunication').",
  "  • After you have the sector's companies, call screen_available_companies or compare_companies on those companies, then answer with 1–2 picks (not every company as equally good).",
  "  • If the sector matches zero companies, show available_sectors from the tool result and offer those instead.",
].join("\n");

export async function dispatchSectorTool(
  name: string,
  args: Record<string, unknown>,
): Promise<unknown | undefined> {
  switch (name) {
    case "list_sectors":
      return listSectorsInDatabase();
    case "list_companies_by_sector":
      return listCompaniesBySector(String(args.sector ?? ""));
    default:
      return undefined;
  }
}
