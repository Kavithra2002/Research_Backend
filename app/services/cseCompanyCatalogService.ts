import { CseListedCompany } from "../models/CseListedCompany";
import { logger } from "../utils/logger";

const CSE_ORIGIN = "https://www.cse.lk";
const CSE_API = `${CSE_ORIGIN}/api`;
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const REQUEST_TIMEOUT_MS = 15000;

export type CseCatalogCompany = {
  name: string;
  symbol: string;
  displayName: string;
};

export type CseCatalogResult = {
  companies: CseCatalogCompany[];
  source: "live" | "database";
  syncedAt: string | null;
  stale?: boolean;
};

type TradeSummaryRow = {
  name?: string;
  symbol?: string;
};

async function fetchLiveTradeSummary(): Promise<TradeSummaryRow[]> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${CSE_API}/tradeSummary`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        Origin: CSE_ORIGIN,
        Referer: `${CSE_ORIGIN}/`,
        "User-Agent": USER_AGENT,
        Accept: "application/json, text/plain, */*",
      },
      body: "",
      signal: controller.signal,
      cache: "no-store",
    });
    if (!res.ok) {
      throw new Error(`CSE tradeSummary responded ${res.status}`);
    }
    const data = (await res.json()) as Record<string, unknown>;
    const rows =
      (data?.reqTradeSummery as TradeSummaryRow[] | undefined) ??
      (data?.reqTradeSummary as TradeSummaryRow[] | undefined) ??
      [];
    if (!Array.isArray(rows)) return [];
    return rows.filter((r) => r?.symbol && r?.name);
  } finally {
    clearTimeout(timer);
  }
}

function normalizeCompanies(rows: TradeSummaryRow[]): CseCatalogCompany[] {
  const seen = new Set<string>();
  const out: CseCatalogCompany[] = [];
  for (const row of rows) {
    const symbol = String(row.symbol ?? "").trim();
    const name = String(row.name ?? "").trim();
    if (!symbol || !name || seen.has(symbol)) continue;
    seen.add(symbol);
    out.push({ name, symbol, displayName: name });
  }
  out.sort((a, b) =>
    a.name.localeCompare(b.name, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );
  return out;
}

async function syncCompaniesToDatabase(companies: CseCatalogCompany[]): Promise<Date> {
  const syncedAt = new Date();
  if (companies.length === 0) return syncedAt;

  const ops = companies.map((c) => ({
    updateOne: {
      filter: { symbol: c.symbol },
      update: { $set: { symbol: c.symbol, name: c.name, synced_at: syncedAt } },
      upsert: true,
    },
  }));

  await CseListedCompany.bulkWrite(ops, { ordered: false });

  const liveSymbols = companies.map((c) => c.symbol);
  await CseListedCompany.deleteMany({ symbol: { $nin: liveSymbols } });

  logger.info(
    `[CSE catalog] Synced ${companies.length} listed companies to MongoDB.`,
  );
  return syncedAt;
}

async function loadCompaniesFromDatabase(): Promise<{
  companies: CseCatalogCompany[];
  syncedAt: Date | null;
}> {
  const docs = await CseListedCompany.find({})
    .sort({ name: 1 })
    .lean()
    .exec();

  if (docs.length === 0) {
    return { companies: [], syncedAt: null };
  }

  const syncedAt = docs.reduce<Date | null>((latest, doc) => {
    const at = doc.synced_at instanceof Date ? doc.synced_at : new Date(doc.synced_at);
    return !latest || at > latest ? at : latest;
  }, null);

  return {
    companies: docs.map((doc) => ({
      name: doc.name,
      symbol: doc.symbol,
      displayName: doc.name,
    })),
    syncedAt,
  };
}

/** Refresh from CSE when live data exists; always persists to MongoDB. */
export async function refreshCseCompanyCatalog(): Promise<CseCatalogResult> {
  try {
    const liveRows = await fetchLiveTradeSummary();
    const companies = normalizeCompanies(liveRows);
    if (companies.length === 0) {
      const cached = await loadCompaniesFromDatabase();
      return {
        companies: cached.companies,
        source: "database",
        syncedAt: cached.syncedAt?.toISOString() ?? null,
        stale: cached.companies.length > 0,
      };
    }

    const syncedAt = await syncCompaniesToDatabase(companies);
    return {
      companies,
      source: "live",
      syncedAt: syncedAt.toISOString(),
    };
  } catch (err) {
    logger.warn("[CSE catalog] Live refresh failed; using database cache.", err);
    const cached = await loadCompaniesFromDatabase();
    return {
      companies: cached.companies,
      source: "database",
      syncedAt: cached.syncedAt?.toISOString() ?? null,
      stale: true,
    };
  }
}

/** List CSE companies: prefer live tradeSummary, fall back to MongoDB cache. */
export async function listCseCompanies(): Promise<CseCatalogResult> {
  return refreshCseCompanyCatalog();
}
