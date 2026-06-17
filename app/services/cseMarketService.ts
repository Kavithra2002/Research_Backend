import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * CSE (Colombo Stock Exchange) live market-data service.
 *
 * The CSE website is backed by a small public JSON API. The single
 * `tradeSummary` endpoint returns, for EVERY listed equity, the authoritative
 * live figures we care about (last traded price, previous close, day range,
 * market cap, change %). We fetch it once, cache it briefly, and resolve a
 * company name to its row with the same fuzzy matching the report uses.
 *
 * This lets Robin anchor its report on REAL exchange data (price + market cap)
 * instead of numbers a language model guessed off the open web.
 * ────────────────────────────────────────────────────────────────────────── */

const CSE_ORIGIN = "https://www.cse.lk";
const CSE_API = `${CSE_ORIGIN}/api`;
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

/** How long a fetched trade summary stays fresh (ms). CSE prices move slowly. */
const CACHE_TTL_MS = 5 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 15000;

/** A single equity row as returned by `/api/tradeSummary`. */
interface TradeSummaryRow {
  name?: string;
  symbol?: string;
  logoUrl?: string;
  price?: number;
  previousClose?: number;
  change?: number;
  percentageChange?: number;
  high?: number;
  low?: number;
  open?: number;
  closingPrice?: number;
  marketCap?: number;
  turnover?: number;
  sharevolume?: number;
  tradevolume?: number;
  lastTradedTime?: number;
}

/** A live, listed-equity snapshot for the Sector Lens "Live" universe. */
export interface CseLiveRow {
  name: string;
  symbol: string;
  logoUrl: string | null;
  price: number | null;
  previousClose: number | null;
  change: number | null;
  changePercent: number | null;
  dayHigh: number | null;
  dayLow: number | null;
  open: number | null;
  marketCap: number | null;
  turnover: number | null;
  shareVolume: number | null;
  tradeVolume: number | null;
  asOf: string | null;
}

/** Authoritative live market snapshot for one company. */
export interface CseSnapshot {
  /** Name the caller asked for. */
  query: string;
  /** Official CSE company name. */
  name: string;
  /** Trading symbol, e.g. "SAMP.N0000". */
  symbol: string;
  /** Last traded price (LKR). */
  price: number | null;
  previousClose: number | null;
  change: number | null;
  changePercent: number | null;
  dayHigh: number | null;
  dayLow: number | null;
  marketCap: number | null;
  /** Derived: shares outstanding ≈ marketCap / price. */
  sharesOutstanding: number | null;
  /** When the figure was last traded (ISO), best-effort. */
  asOf: string | null;
  /** Fuzzy-match confidence in [0,1]. */
  matchScore: number;
}

interface CacheEntry {
  rows: TradeSummaryRow[];
  fetchedAt: number;
}

let cache: CacheEntry | null = null;
let inflight: Promise<TradeSummaryRow[]> | null = null;

async function postForm(path: string, body: string): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${CSE_API}/${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        Origin: CSE_ORIGIN,
        Referer: `${CSE_ORIGIN}/`,
        "User-Agent": USER_AGENT,
        Accept: "application/json, text/plain, */*",
      },
      body,
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new Error(`CSE ${path} responded ${res.status}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function fetchTradeSummary(): Promise<TradeSummaryRow[]> {
  const data = (await postForm("tradeSummary", "")) as Record<string, unknown>;
  const rows =
    (data?.reqTradeSummery as TradeSummaryRow[] | undefined) ??
    (data?.reqTradeSummary as TradeSummaryRow[] | undefined) ??
    [];
  if (!Array.isArray(rows)) return [];
  return rows.filter((r) => r && r.symbol && r.name);
}

/** Fetch the trade summary, reusing a fresh cache / coalescing concurrent calls. */
async function getTradeSummary(): Promise<TradeSummaryRow[]> {
  const now = Date.now();
  if (cache && now - cache.fetchedAt < CACHE_TTL_MS) {
    return cache.rows;
  }
  if (inflight) return inflight;

  inflight = (async () => {
    try {
      const rows = await fetchTradeSummary();
      cache = { rows, fetchedAt: Date.now() };
      logger.info(`[CSE] Trade summary cached (${rows.length} symbols).`);
      return rows;
    } finally {
      inflight = null;
    }
  })();

  return inflight;
}

/* ── Company-name resolution ─────────────────────────────────────────────── */

const SUFFIX_NOISE = /\b(PLC|P\.L\.C\.|LTD|LIMITED|INC|CORP|CORPORATION)\b\.?/gi;

function normalizeName(value: string): string {
  return String(value ?? "")
    .toUpperCase()
    .replace(SUFFIX_NOISE, " ")
    .replace(/[^\w\s&]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Dice-coefficient-ish token + substring score in [0,1]. */
function scoreNames(query: string, candidate: string): number {
  const q = normalizeName(query);
  const c = normalizeName(candidate);
  if (!q || !c) return 0;
  if (q === c) return 1;
  if (q.includes(c) || c.includes(q)) return 0.92;

  const qTokens = new Set(q.split(" ").filter(Boolean));
  const cTokens = new Set(c.split(" ").filter(Boolean));
  if (qTokens.size === 0 || cTokens.size === 0) return 0;

  let overlap = 0;
  for (const t of qTokens) if (cTokens.has(t)) overlap += 1;
  const tokenScore = overlap / Math.max(qTokens.size, cTokens.size);

  // Reward full containment of a multi-word query in the candidate name.
  if (qTokens.size >= 2 && [...qTokens].every((t) => cTokens.has(t))) {
    return Math.max(tokenScore, 0.88);
  }
  return tokenScore;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function rowToSnapshot(
  query: string,
  row: TradeSummaryRow,
  matchScore: number,
): CseSnapshot {
  const price = num(row.price);
  const marketCap = num(row.marketCap);
  const sharesOutstanding =
    price && marketCap && price > 0 ? Math.round(marketCap / price) : null;
  let asOf: string | null = null;
  if (typeof row.lastTradedTime === "number" && row.lastTradedTime > 0) {
    const ms =
      row.lastTradedTime > 10_000_000_000
        ? row.lastTradedTime
        : row.lastTradedTime * 1000;
    const d = new Date(ms);
    if (!Number.isNaN(d.getTime())) asOf = d.toISOString();
  }
  return {
    query,
    name: String(row.name ?? query),
    symbol: String(row.symbol ?? ""),
    price,
    previousClose: num(row.previousClose),
    change: num(row.change),
    changePercent: num(row.percentageChange),
    dayHigh: num(row.high),
    dayLow: num(row.low),
    marketCap,
    sharesOutstanding,
    asOf,
    matchScore,
  };
}

function tradeTimeToIso(lastTradedTime: unknown): string | null {
  if (typeof lastTradedTime !== "number" || lastTradedTime <= 0) return null;
  const ms =
    lastTradedTime > 10_000_000_000 ? lastTradedTime : lastTradedTime * 1000;
  const d = new Date(ms);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/**
 * Return EVERY equity listed on the CSE with its live trade figures. Used by the
 * Sector Lens "Live" tab. Never throws — returns an empty list on any failure.
 */
export async function getCseUniverse(): Promise<CseLiveRow[]> {
  let rows: TradeSummaryRow[];
  try {
    rows = await getTradeSummary();
  } catch (err) {
    logger.warn(
      `[CSE] Could not fetch trade summary: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
    return [];
  }

  return rows
    .map((row) => ({
      name: String(row.name ?? "").trim(),
      symbol: String(row.symbol ?? "").trim(),
      logoUrl: row.logoUrl ? String(row.logoUrl) : null,
      price: num(row.price),
      previousClose: num(row.previousClose),
      change: num(row.change),
      changePercent: num(row.percentageChange),
      dayHigh: num(row.high),
      dayLow: num(row.low),
      open: num(row.open),
      marketCap: num(row.marketCap),
      turnover: num(row.turnover),
      shareVolume: num(row.sharevolume),
      tradeVolume: num(row.tradevolume),
      asOf: tradeTimeToIso(row.lastTradedTime),
    }))
    .filter((r) => r.name && r.symbol)
    .sort((a, b) => (b.marketCap ?? 0) - (a.marketCap ?? 0));
}

/** Minimum confidence before we treat a fuzzy name match as the right company. */
const MIN_MATCH_SCORE = 0.6;

/**
 * Resolve a list of company names to live CSE market snapshots. Returns one
 * entry per name that matched a listed equity with sufficient confidence;
 * unmatched names are simply omitted. Never throws — on any network/parse
 * failure it returns an empty list so report generation can carry on.
 */
export async function getCseSnapshots(
  names: string[],
): Promise<CseSnapshot[]> {
  const cleaned = names.map((n) => String(n ?? "").trim()).filter(Boolean);
  if (cleaned.length === 0) return [];

  let rows: TradeSummaryRow[];
  try {
    rows = await getTradeSummary();
  } catch (err) {
    logger.warn(
      `[CSE] Could not fetch trade summary: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
    return [];
  }
  if (rows.length === 0) return [];

  const out: CseSnapshot[] = [];
  for (const query of cleaned) {
    let best: TradeSummaryRow | null = null;
    let bestScore = 0;
    for (const row of rows) {
      const score = scoreNames(query, String(row.name ?? ""));
      if (score > bestScore) {
        bestScore = score;
        best = row;
      }
    }
    if (best && bestScore >= MIN_MATCH_SCORE) {
      out.push(rowToSnapshot(query, best, Number(bestScore.toFixed(2))));
    }
  }
  return out;
}
