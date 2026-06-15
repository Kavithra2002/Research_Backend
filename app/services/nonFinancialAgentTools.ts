import { NonFinancialData } from "../models/NonFinancialData";
import type {
  NonFinancialCategory,
  NonFinancialMetric,
} from "../models/NonFinancialData";

/* ────────────────────────────────────────────────────────────────────────── *
 * Non-financial DB tools — shared by every data agent (Robin, Tuck, …).
 *
 * These query the `non_financial_data` collection (sector / business, group
 * structure, people & workforce, branches & distribution network, awards,
 * sustainability, governance, etc.) that was extracted from companies' annual
 * reports. They mirror the shape of the financial DB tools so an agent can
 * spread them into its TOOLS array and dispatch by name.
 *
 * Every value an agent reports through these tools is grounded in stored data;
 * nothing is invented.
 *
 * Tools:
 *   • list_non_financial_companies  — discover / resolve companies
 *   • non_financial_overview        — company profile + what is stored
 *   • get_non_financial_metric      — precise facts for ONE report year
 *   • get_non_financial_timeseries  — a metric across ALL years in ONE call
 *                                     (use this for trends / charts)
 * ────────────────────────────────────────────────────────────────────────── */

const MAX_METRIC_MATCHES = 20;
const MAX_DETAIL_CHARS = 1200;

type NfCompanyRef = { slug: string; name: string | null };

interface NfDocLite {
  company_slug: string;
  company_name: string;
  year: number | null;
  report_key: string;
  reporting_year: string | null;
  company_overview: string;
  categories: NonFinancialCategory[];
}

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeName(value: string): string {
  return String(value ?? "")
    .toLowerCase()
    .replace(/\bplc\b|\blimited\b|\bltd\b|\bpvt\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Metric targeting
 *
 * Maps the words a user is likely to type to the EXACT taxonomy metric key, so
 * "how many employees" reliably targets `total_employees`, "branches" targets
 * `branches_outlets`, "group structure" targets the structure metrics, etc.
 * Longer alias phrases are matched first so "training hours" doesn't collide
 * with "employees".
 * ────────────────────────────────────────────────────────────────────────── */

const METRIC_ALIASES: Record<string, string[]> = {
  total_employees: [
    "number of employees",
    "how many employees",
    "total employees",
    "employee count",
    "headcount",
    "workforce size",
    "staff strength",
    "employees",
    "employee",
    "workforce",
    "staff",
    "people",
  ],
  gender_diversity: ["gender diversity", "gender", "female", "male", "diversity ratio"],
  employee_turnover: ["employee turnover", "attrition", "turnover rate"],
  training_hours: ["training hours", "training investment", "learning hours"],
  workplace_safety: ["workplace safety", "safety record", "lost time", "injuries", "accidents"],
  employee_benefits: ["employee benefits", "welfare", "benefits scheme"],
  branches_outlets: [
    "number of branches",
    "how many branches",
    "branches",
    "branch network",
    "outlets",
    "outlet",
    "distribution network",
    "points of sale",
    "branch",
  ],
  atms: ["atm", "atms", "self-service", "self service"],
  digital_users: ["digital users", "app users", "mobile users", "active users", "app downloads"],
  network_coverage: ["network coverage", "towers", "base stations", "coverage"],
  customer_count: ["customer count", "subscriber count", "customers", "subscribers"],
  group_structure_diagram: [
    "group structure",
    "corporate structure",
    "organisation structure",
    "organization structure",
    "structure of the group",
    "structure",
  ],
  subsidiaries: ["list of subsidiaries", "subsidiaries", "subsidiary"],
  business_segments: [
    "business segments",
    "segments",
    "divisions",
    "lines of business",
    "what sector",
    "which sector",
    "sector",
    "industry",
  ],
  overseas_presence: ["overseas presence", "foreign presence", "international presence", "overseas", "global presence"],
  new_branch_openings: ["new branches", "new branch openings", "expansion", "new openings"],
  about_company: [
    "about the company",
    "who we are",
    "what does the company do",
    "business description",
    "company overview",
    "company profile",
    "brief",
    "briefing",
    "about",
  ],
  vision_mission: ["vision and mission", "vision", "mission", "purpose"],
  core_values: ["core values", "values"],
  company_theme: ["annual report theme", "company theme", "theme"],
  company_milestones: ["milestones", "company history", "our story"],
  awards: ["awards", "award", "recognition", "accolade"],
  certifications: ["certifications", "certification", "iso"],
  credit_ratings: ["credit rating", "credit ratings", "brand rating"],
  industry_rankings: ["industry ranking", "market position", "rankings"],
  ghg_emissions: ["ghg emissions", "ghg", "carbon footprint", "carbon", "emissions", "scope 1", "scope 2"],
  energy_consumption: ["energy consumption", "energy", "renewable energy", "renewable"],
  water_usage: ["water usage", "water"],
  waste_management: ["waste management", "waste", "recycling"],
  carbon_neutrality: ["carbon neutrality", "net zero", "net-zero", "carbon neutral"],
  esg_strategy: ["esg strategy", "esg", "sustainability framework", "sustainability strategy", "sustainability"],
  csr_spend: ["csr spend", "csr", "community investment", "community spend"],
  beneficiaries: ["beneficiaries", "people impacted", "communities impacted"],
  financial_inclusion: ["financial inclusion", "financial literacy"],
  dei: ["dei", "diversity equity", "inclusion"],
  board_profiles: ["board of directors", "board profiles", "directors", "board"],
  board_committees: ["board committees", "audit committee", "remuneration committee"],
  senior_management: ["senior management", "c-suite", "management team"],
  chairman_message: ["chairman", "chairperson", "chairman's message"],
  ceo_review: ["ceo review", "ceo", "managing director", "md review"],
  governance_framework: ["corporate governance", "governance framework", "governance"],
  stakeholder_engagement: ["stakeholder engagement", "stakeholders"],
  material_matters: ["material matters", "materiality"],
  value_creation: ["value creation", "capitals"],
  operating_environment: ["operating environment", "macro review", "economic context"],
  nonfin_snapshot: ["non-financial highlights", "non financial highlights", "highlights snapshot"],
  strategy_in_action: ["strategy in action", "strategic navigator", "strategic objectives"],
  risk_framework: ["risk management", "risk framework", "key risks"],
  digital_transformation: ["digital transformation", "digitalisation", "digitalization"],
};

/** Resolve the user's query to the exact taxonomy metric key(s) it targets. */
function resolveTargetKeys(query: string): string[] {
  const q = ` ${query.toLowerCase()} `;
  const hits: Array<{ key: string; len: number }> = [];
  for (const [key, aliases] of Object.entries(METRIC_ALIASES)) {
    let best = 0;
    for (const alias of aliases) {
      if (q.includes(alias.toLowerCase())) best = Math.max(best, alias.length);
    }
    if (best > 0) hits.push({ key, len: best });
  }
  // Longer (more specific) alias matches first.
  hits.sort((a, b) => b.len - a.len);
  return hits.map((h) => h.key);
}

/** Every company that has non-financial data stored. */
async function allNfCompanies(): Promise<NfCompanyRef[]> {
  const rows = await NonFinancialData.aggregate<{
    _id: string;
    name: string | null;
  }>([
    {
      $group: {
        _id: "$company_slug",
        name: { $first: "$company_name" },
      },
    },
  ]);
  return rows.map((r) => ({ slug: r._id, name: r.name ?? null }));
}

type NfResolveResult =
  | { slug: string; name: string | null }
  | { candidates: NfCompanyRef[] }
  | { error: string };

/** Resolve a typed company term to a slug within the non-financial collection. */
async function resolveNfCompany(term: string): Promise<NfResolveResult> {
  const t = (term || "").trim();
  if (!t) return { error: "No company name/slug given." };

  // 1) exact slug
  const bySlug = await NonFinancialData.findOne({ company_slug: t })
    .select({ _id: 0, company_slug: 1, company_name: 1 })
    .lean();
  if (bySlug) {
    return { slug: bySlug.company_slug, name: bySlug.company_name ?? null };
  }

  // 2) exact (case-insensitive) name
  const byName = await NonFinancialData.findOne({
    company_name: { $regex: `^${escapeRegex(t)}$`, $options: "i" },
  })
    .select({ _id: 0, company_slug: 1, company_name: 1 })
    .lean();
  if (byName) {
    return { slug: byName.company_slug, name: byName.company_name ?? null };
  }

  // 3) substring contains on name or slug
  const rx = { $regex: escapeRegex(t), $options: "i" };
  const matches = await NonFinancialData.aggregate<{
    _id: string;
    name: string | null;
  }>([
    { $match: { $or: [{ company_name: rx }, { company_slug: rx }] } },
    { $group: { _id: "$company_slug", name: { $first: "$company_name" } } },
  ]);
  if (matches.length === 1) {
    return { slug: matches[0]._id, name: matches[0].name ?? null };
  }
  if (matches.length > 1) {
    return {
      candidates: matches
        .slice(0, 10)
        .map((m) => ({ slug: m._id, name: m.name ?? null })),
    };
  }

  // 4) fuzzy fallback by normalized-token overlap so typos still surface options.
  const all = await allNfCompanies();
  const want = normalizeName(t);
  const wantTokens = new Set(want.split(" ").filter(Boolean));
  const scored = all
    .map((ref) => {
      const cand = normalizeName(ref.name ?? ref.slug);
      const candTokens = new Set(cand.split(" ").filter(Boolean));
      let overlap = 0;
      for (const tok of wantTokens) if (candTokens.has(tok)) overlap += 1;
      const score =
        overlap / Math.max(1, Math.max(wantTokens.size, candTokens.size)) +
        (cand.includes(want) || want.includes(cand) ? 0.4 : 0);
      return { ref, score };
    })
    .filter((s) => s.score >= 0.4)
    .sort((a, b) => b.score - a.score);

  if (scored.length > 0) {
    return { candidates: scored.slice(0, 5).map((s) => s.ref) };
  }

  return { error: `No non-financial data found for a company matching '${t}'.` };
}

/** Fetch every stored non-financial report for a company, oldest → newest. */
async function fetchReports(
  companySlug: string,
  years?: number[],
): Promise<NfDocLite[]> {
  const filter: Record<string, unknown> = { company_slug: companySlug };
  if (years && years.length > 0) filter.year = { $in: years };
  const docs = await NonFinancialData.find(filter)
    .select({
      _id: 0,
      company_slug: 1,
      company_name: 1,
      year: 1,
      report_key: 1,
      reporting_year: 1,
      company_overview: 1,
      categories: 1,
    })
    .sort({ year: 1 })
    .lean();
  return docs.map((doc) => ({
    company_slug: doc.company_slug,
    company_name: doc.company_name,
    year: doc.year ?? null,
    report_key: doc.report_key,
    reporting_year: doc.reporting_year ?? null,
    company_overview: doc.company_overview ?? "",
    categories: Array.isArray(doc.categories)
      ? (doc.categories as NonFinancialCategory[])
      : [],
  }));
}

/** Pick the report for a company — latest year unless a specific year is asked. */
async function pickReport(
  companySlug: string,
  year?: number,
): Promise<NfDocLite | null> {
  const filter: Record<string, unknown> = { company_slug: companySlug };
  if (typeof year === "number") filter.year = year;
  const doc = await NonFinancialData.findOne(filter)
    .select({
      _id: 0,
      company_slug: 1,
      company_name: 1,
      year: 1,
      report_key: 1,
      reporting_year: 1,
      company_overview: 1,
      categories: 1,
    })
    .sort({ year: -1 })
    .lean();
  if (!doc) return null;
  return {
    company_slug: doc.company_slug,
    company_name: doc.company_name,
    year: doc.year ?? null,
    report_key: doc.report_key,
    reporting_year: doc.reporting_year ?? null,
    company_overview: doc.company_overview ?? "",
    categories: Array.isArray(doc.categories)
      ? (doc.categories as NonFinancialCategory[])
      : [],
  };
}

function clampDetail(text: string): string {
  const t = String(text ?? "").trim();
  if (t.length <= MAX_DETAIL_CHARS) return t;
  return `${t.slice(0, MAX_DETAIL_CHARS)}…`;
}

/** Pull a clean number out of a stored value string ("7,599" → 7599, "249 permanent" → 249, "35% female" → 35). */
function parseNumeric(value: string): number | null {
  const m = String(value ?? "")
    .replace(/,/g, "")
    .match(/-?\d+(?:\.\d+)?/);
  return m ? Number(m[0]) : null;
}

interface ScoredMetric {
  category: NonFinancialCategory;
  metric: NonFinancialMetric;
  score: number;
}

/**
 * Score how well a stored metric answers the query. Exact taxonomy-key targeting
 * dominates; then title / value / detail / category text overlap; a found metric
 * is preferred over an empty placeholder.
 */
function scoreMetrics(
  categories: NonFinancialCategory[],
  query: string,
  targetKeys: string[],
): ScoredMetric[] {
  const q = query.toLowerCase();
  const terms = new Set(q.split(/\s+/).filter((w) => w.length > 2));
  const targetSet = new Set(targetKeys);

  const out: ScoredMetric[] = [];
  for (const category of categories) {
    const catText = `${category.title ?? ""} ${category.key ?? ""}`.toLowerCase();
    for (const metric of category.metrics ?? []) {
      let score = 0;
      if (targetSet.has(metric.key)) {
        // Rank multiple targeted keys by alias specificity (order in targetKeys).
        score += 200 - targetKeys.indexOf(metric.key);
      }
      const title = (metric.title ?? "").toLowerCase();
      const key = (metric.key ?? "").toLowerCase();
      const valueText = (metric.value ?? "").toLowerCase();
      const detailText = (metric.detail ?? "").toLowerCase();
      for (const term of terms) {
        if (key.includes(term)) score += 12;
        if (title.includes(term)) score += 8;
        if (catText.includes(term)) score += 3;
        if (valueText.includes(term)) score += 2;
        if (detailText.includes(term)) score += 1;
      }
      if (score <= 0) continue;
      score += metric.found ? 6 : -2;
      out.push({ category, metric, score });
    }
  }
  out.sort((a, b) => b.score - a.score);
  return out;
}

function slimMetric(category: NonFinancialCategory, metric: NonFinancialMetric) {
  return {
    category: category.title,
    category_key: category.key,
    metric: metric.title,
    metric_key: metric.key,
    found: metric.found,
    value: metric.value ?? "",
    numeric_value: parseNumeric(metric.value ?? ""),
    detail: clampDetail(metric.detail ?? ""),
    pages: Array.isArray(metric.pages) ? metric.pages : [],
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool schema exposed to the model
 * ────────────────────────────────────────────────────────────────────────── */

export const NON_FINANCIAL_DB_TOOLS = [
  {
    type: "function",
    function: {
      name: "list_non_financial_companies",
      description:
        "List companies that have NON-FINANCIAL data in the database (sector/business description, group structure, employee counts, branch network, sustainability, governance, awards, etc.). Use this to discover companies or resolve a name the user typed to its exact slug.",
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
      name: "non_financial_overview",
      description:
        "Get a company's non-financial PROFILE: a short company overview / what it does, plus the list of non-financial categories available (e.g. company identity, group structure, people & workforce, distribution network, sustainability, governance) and which metrics within each were found. Call this FIRST for a single-period non-financial question (sector, what the company does, employees, branches, group structure for one year) so you know what is stored, then drill in with get_non_financial_metric. For trends across multiple years, use get_non_financial_timeseries instead.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
          year: {
            type: "integer",
            description:
              "Optional reporting year. Omit to use the most recent report on file.",
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
      name: "get_non_financial_metric",
      description:
        "Fetch a specific NON-FINANCIAL fact for a company for ONE report year by searching its stored metrics. Use this for single-year questions like 'how many employees in 2023', 'how many branches', 'what is the group structure', 'subsidiaries', 'what sector / what does it do', 'vision / mission', 'awards', 'sustainability', 'board / governance'. Returns the best-matching metric (with its exact value, a parsed number when numeric, supporting detail and source pages) plus other related metrics. Good query keywords: 'employees', 'branches', 'group structure', 'subsidiaries', 'sector', 'about', 'vision', 'awards', 'sustainability', 'board'.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
          query: {
            type: "string",
            description:
              "Keyword(s) describing the fact you need, e.g. 'employees', 'branches', 'group structure', 'sector', 'awards'.",
          },
          year: {
            type: "integer",
            description:
              "Optional reporting year. Omit to use the most recent report on file.",
          },
        },
        required: ["company", "query"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_non_financial_timeseries",
      description:
        "Fetch ONE non-financial metric across ALL available report years for a company in a SINGLE call. ALWAYS use this (not repeated get_non_financial_metric calls) for any trend, multi-year question, or CHART/graph over time — e.g. 'chart of employees from 2020 to 2025', 'how has the branch network grown', 'training hours trend'. Returns one data point per year with the exact value, a parsed numeric value for charting, and whether it was found in that year's report. Years where the metric is genuinely absent are flagged found=false so you can show 'n/a' for only those years — do not assume a year is missing without checking here.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string", description: "Company name or slug." },
          query: {
            type: "string",
            description:
              "The metric to track over time, e.g. 'employees', 'branches', 'training hours', 'GHG emissions'.",
          },
          years: {
            type: "array",
            items: { type: "integer" },
            description:
              "Optional list of years to include, e.g. [2020,2021,2022,2023,2024,2025]. Omit for all available years.",
          },
        },
        required: ["company", "query"],
        additionalProperties: false,
      },
    },
  },
] as const;

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool implementations
 * ────────────────────────────────────────────────────────────────────────── */

async function listNfCompanies(query?: string): Promise<NfCompanyRef[]> {
  const q = query?.trim();
  const filter: Record<string, unknown> = {};
  if (q) {
    const rx = { $regex: escapeRegex(q), $options: "i" };
    filter.$or = [{ company_name: rx }, { company_slug: rx }];
  }
  const rows = await NonFinancialData.aggregate<{
    _id: string;
    name: string | null;
  }>([
    { $match: filter },
    { $group: { _id: "$company_slug", name: { $first: "$company_name" } } },
    { $sort: { name: 1 } },
    { $limit: 60 },
  ]);
  return rows.map((r) => ({ slug: r._id, name: r.name ?? null }));
}

async function nonFinancialOverview(
  companySlug: string,
  year?: number,
): Promise<unknown> {
  const doc = await pickReport(companySlug, year);
  if (!doc) {
    return {
      found: false,
      note: "No non-financial report stored for that company / year.",
    };
  }
  const categories = doc.categories.map((cat) => ({
    key: cat.key,
    title: cat.title,
    metrics: (cat.metrics ?? []).map((m) => ({
      key: m.key,
      title: m.title,
      found: m.found,
    })),
  }));
  return {
    found: true,
    company_name: doc.company_name,
    report_key: doc.report_key,
    year: doc.year,
    reporting_year: doc.reporting_year,
    company_overview: clampDetail(doc.company_overview),
    categories,
  };
}

async function getNonFinancialMetric(args: {
  companySlug: string;
  query: string;
  year?: number;
}): Promise<unknown> {
  const kw = (args.query || "").trim();
  if (!kw) return { error: "query is required for get_non_financial_metric" };

  const doc = await pickReport(args.companySlug, args.year);
  if (!doc) {
    return {
      found: false,
      note: "No non-financial report stored for that company / year.",
    };
  }

  const targetKeys = resolveTargetKeys(kw);
  const scored = scoreMetrics(doc.categories, kw, targetKeys);

  if (scored.length === 0) {
    return {
      found: false,
      company_name: doc.company_name,
      report_key: doc.report_key,
      year: doc.year,
      reporting_year: doc.reporting_year,
      query: args.query,
      target_keys: targetKeys,
      note: "No metric matched that query in this report. Try a different keyword, or use search_report_text for narrative passages.",
    };
  }

  const best = scored.find((s) => s.metric.found) ?? scored[0];
  const related = scored
    .slice(0, MAX_METRIC_MATCHES)
    .map((s) => slimMetric(s.category, s.metric));

  return {
    found: best.metric.found,
    company_name: doc.company_name,
    report_key: doc.report_key,
    year: doc.year,
    reporting_year: doc.reporting_year,
    query: args.query,
    target_keys: targetKeys,
    best_match: slimMetric(best.category, best.metric),
    related_metrics: related,
  };
}

async function getNonFinancialTimeseries(args: {
  companySlug: string;
  query: string;
  years?: number[];
}): Promise<unknown> {
  const kw = (args.query || "").trim();
  if (!kw) return { error: "query is required for get_non_financial_timeseries" };

  const reports = await fetchReports(args.companySlug, args.years);
  if (reports.length === 0) {
    return {
      found: false,
      note: "No non-financial reports stored for that company.",
    };
  }

  const targetKeys = resolveTargetKeys(kw);
  let metricTitle: string | null = null;
  let metricKey: string | null = null;

  const points = reports.map((doc) => {
    const scored = scoreMetrics(doc.categories, kw, targetKeys);
    const best = scored.find((s) => s.metric.found) ?? scored[0] ?? null;
    if (best && !metricKey) {
      metricKey = best.metric.key;
      metricTitle = best.metric.title;
    }
    if (!best) {
      return {
        year: doc.year,
        reporting_year: doc.reporting_year,
        report_key: doc.report_key,
        found: false,
        value: "",
        numeric_value: null,
        detail: "",
      };
    }
    return {
      year: doc.year,
      reporting_year: doc.reporting_year,
      report_key: doc.report_key,
      metric: best.metric.title,
      metric_key: best.metric.key,
      found: best.metric.found,
      value: best.metric.value ?? "",
      numeric_value: parseNumeric(best.metric.value ?? ""),
      detail: clampDetail(best.metric.detail ?? ""),
    };
  });

  return {
    company_name: reports[0].company_name,
    query: args.query,
    target_keys: targetKeys,
    metric_resolved: metricTitle,
    metric_key: metricKey,
    found_years: points.filter((p) => p.found).map((p) => p.year),
    point_count: points.length,
    points,
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Dispatch
 * ────────────────────────────────────────────────────────────────────────── */

/**
 * Dispatch a non-financial DB tool by name. Returns `undefined` when the tool
 * name isn't one of these so callers can fall through to other tool sets.
 */
export async function dispatchNonFinancialTool(
  name: string,
  args: unknown,
): Promise<unknown | undefined> {
  const a = (args ?? {}) as Record<string, unknown>;

  switch (name) {
    case "list_non_financial_companies":
      return {
        companies: await listNfCompanies(
          typeof a.query === "string" ? a.query : undefined,
        ),
      };

    case "non_financial_overview": {
      const resolved = await resolveNfCompany(String(a.company ?? ""));
      if (!("slug" in resolved)) return resolved;
      return nonFinancialOverview(
        resolved.slug,
        typeof a.year === "number" ? a.year : undefined,
      );
    }

    case "get_non_financial_metric": {
      const resolved = await resolveNfCompany(String(a.company ?? ""));
      if (!("slug" in resolved)) return resolved;
      return getNonFinancialMetric({
        companySlug: resolved.slug,
        query: String(a.query ?? ""),
        year: typeof a.year === "number" ? a.year : undefined,
      });
    }

    case "get_non_financial_timeseries": {
      const resolved = await resolveNfCompany(String(a.company ?? ""));
      if (!("slug" in resolved)) return resolved;
      const years = Array.isArray(a.years)
        ? a.years
            .map((y) => (typeof y === "number" ? y : Number(y)))
            .filter((y) => Number.isFinite(y))
        : undefined;
      return getNonFinancialTimeseries({
        companySlug: resolved.slug,
        query: String(a.query ?? ""),
        years,
      });
    }

    default:
      return undefined;
  }
}
