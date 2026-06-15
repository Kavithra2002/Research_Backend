import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import { OpenAiBalanceSnapshot } from "../models/OpenAiBalanceSnapshot";

/* ────────────────────────────────────────────────────────────────────────── *
 * OpenAI spend + balance (Costs API)
 *
 * OpenAI does NOT expose a "remaining credit balance" through any API usable
 * with a regular or admin key — that number is only visible in the dashboard.
 *
 * What we CAN read (with an Organization Admin key, sk-admin-...) is actual
 * USD spend via the Costs API:
 *
 *   GET https://api.openai.com/v1/organization/costs
 *
 * To still show a live "remaining" figure, the admin records a manual balance
 * snapshot (OPENAI_CREDIT_BALANCE) and the date it was true
 * (OPENAI_CREDIT_BALANCE_AS_OF). We then subtract the real spend incurred since
 * that date to estimate what's left.
 *
 * We fetch daily ("1d") buckets and derive: today's spend, month-to-date spend,
 * and spend-since-snapshot. Results are cached briefly.
 * ────────────────────────────────────────────────────────────────────────── */

export interface OpenAiBalance {
  /** The manually recorded balance snapshot, in USD. */
  initial: number;
  /** ISO date the snapshot was accurate (UTC). */
  asOf: string;
  /** Spend since the snapshot date, in USD (0 if no admin key). */
  spentSince: number;
  /** initial - spentSince, in USD. */
  remaining: number;
  /** True when spend could be measured (admin key present). */
  liveDecrement: boolean;
}

export interface OpenAiSpend {
  /** Spend for the current UTC day, in USD (null if no admin key). */
  today: number | null;
  /** Month-to-date spend (current UTC month), in USD (null if no admin key). */
  month: number | null;
  /** Manual balance estimate (null if not configured). */
  balance: OpenAiBalance | null;
  currency: string;
  /** ISO timestamp of when this data was fetched. */
  updatedAt: string;
}

interface CostsResult {
  // OpenAI returns `value` as a high-precision string (e.g. "0.14244750..."),
  // not a number — parse defensively for both.
  amount?: { value?: number | string; currency?: string };
}

interface CostsBucket {
  start_time?: number;
  end_time?: number;
  results?: CostsResult[];
}

interface CostsResponse {
  data?: CostsBucket[];
  has_more?: boolean;
  next_page?: string | null;
}

const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes
let cache: { value: OpenAiSpend; expiresAt: number } | null = null;

function utcDayStartSeconds(d: Date): number {
  return Math.floor(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 1000,
  );
}

function utcMonthStartSeconds(d: Date): number {
  return Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 1000);
}

function sumBucket(bucket: CostsBucket): {
  amount: number;
  currency: string | null;
} {
  let amount = 0;
  let currency: string | null = null;
  for (const r of bucket.results ?? []) {
    const raw = r.amount?.value;
    const parsed = typeof raw === "string" ? Number(raw) : raw;
    if (typeof parsed === "number" && Number.isFinite(parsed)) {
      amount += parsed;
    }
    if (!currency && r.amount?.currency) currency = r.amount.currency;
  }
  return { amount, currency };
}

/** Parse OPENAI_CREDIT_BALANCE_AS_OF (YYYY-MM-DD) into UTC seconds, or null. */
function parseAsOfSeconds(raw: string | null): number | null {
  if (!raw) return null;
  const ts = Date.parse(`${raw}T00:00:00Z`);
  if (Number.isNaN(ts)) return null;
  return Math.floor(ts / 1000);
}

/** Fetch every daily cost bucket from `startTime` (paginating as needed). */
async function fetchDailyBuckets(startTime: number): Promise<CostsBucket[]> {
  const adminKey = env.openai.adminKey;
  if (!adminKey) return [];

  const buckets: CostsBucket[] = [];
  let page: string | null = null;

  // Safety cap: at most ~12 pages (each up to 180 days) to avoid runaway loops.
  for (let i = 0; i < 12; i += 1) {
    const url = new URL("https://api.openai.com/v1/organization/costs");
    url.searchParams.set("start_time", String(startTime));
    url.searchParams.set("bucket_width", "1d");
    url.searchParams.set("limit", "180");
    if (page) url.searchParams.set("page", page);

    let res: Response;
    try {
      res = await fetch(url, {
        headers: {
          Authorization: `Bearer ${adminKey}`,
          "Content-Type": "application/json",
        },
      });
    } catch (err) {
      throw HttpError.internal(
        `Failed to reach OpenAI Costs API: ${(err as Error).message}`,
      );
    }

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      if (res.status === 401 || res.status === 403) {
        throw new HttpError(
          502,
          "OpenAI rejected the admin key. Ensure OPENAI_ADMIN_KEY is a valid " +
            "Organization Admin key (sk-admin-...) with billing access.",
          body || undefined,
        );
      }
      throw new HttpError(
        502,
        `OpenAI Costs API returned ${res.status}`,
        body || undefined,
      );
    }

    const json = (await res.json()) as CostsResponse;
    buckets.push(...(json.data ?? []));
    if (!json.has_more || !json.next_page) break;
    page = json.next_page;
  }

  return buckets;
}

/** A resolved balance snapshot (initial USD amount + the date it was true). */
interface ResolvedSnapshot {
  balance: number;
  asOf: string | null;
}

/**
 * The effective balance snapshot. Prefers the value stored in the DB (set from
 * the UI) and falls back to the backend/.env values. Returns null when neither
 * is configured.
 */
async function resolveSnapshot(): Promise<ResolvedSnapshot | null> {
  const stored = await OpenAiBalanceSnapshot.findOne()
    .sort({ createdAt: -1 })
    .lean()
    .exec();
  if (stored && Number.isFinite(stored.balance)) {
    return { balance: stored.balance, asOf: stored.asOf ?? null };
  }

  const balanceRaw = env.openai.creditBalance;
  const fromEnv = balanceRaw != null ? Number(balanceRaw) : null;
  if (fromEnv != null && Number.isFinite(fromEnv)) {
    return { balance: fromEnv, asOf: env.openai.creditBalanceAsOf };
  }

  return null;
}

/**
 * Record a new credit-balance snapshot from the UI. This becomes the new
 * "initial" balance and spend is decremented from `asOf` onward. Clears the
 * spend cache so the next read reflects the change immediately.
 */
export async function setOpenAiBalance(input: {
  balance: number;
  asOf?: string | null;
  updatedBy?: string | null;
}): Promise<OpenAiSpend> {
  if (!Number.isFinite(input.balance) || input.balance < 0) {
    throw HttpError.badRequest("Balance must be a non-negative number.");
  }

  let asOf = (input.asOf ?? "").trim();
  if (asOf) {
    if (Number.isNaN(Date.parse(`${asOf}T00:00:00Z`))) {
      throw HttpError.badRequest("asOf must be a valid YYYY-MM-DD date.");
    }
  } else {
    asOf = new Date().toISOString().slice(0, 10);
  }

  await OpenAiBalanceSnapshot.create({
    balance: input.balance,
    asOf,
    updatedBy: input.updatedBy ?? null,
  });

  // Invalidate the cache so the new balance is reflected on the next read.
  cache = null;
  return getOpenAiSpend(true);
}

export async function getOpenAiSpend(force = false): Promise<OpenAiSpend> {
  const adminKey = env.openai.adminKey;
  const snapshot = await resolveSnapshot();
  const initialBalance = snapshot?.balance ?? null;
  const hasBalance =
    initialBalance != null && Number.isFinite(initialBalance);

  if (!adminKey && !hasBalance) {
    throw HttpError.badRequest(
      "OpenAI usage is not configured. Add OPENAI_ADMIN_KEY (for spend) " +
        "and/or OPENAI_CREDIT_BALANCE (for the balance estimate) to backend/.env.",
    );
  }

  if (!force && cache && cache.expiresAt > Date.now()) {
    return cache.value;
  }

  const now = new Date();
  const dayStart = utcDayStartSeconds(now);
  const monthStart = utcMonthStartSeconds(now);
  const asOfSeconds = hasBalance
    ? parseAsOfSeconds(snapshot?.asOf ?? null) ?? dayStart
    : null;

  // Fetch from the earliest point we need data for (snapshot date or month).
  const fetchStart =
    asOfSeconds != null ? Math.min(asOfSeconds, monthStart) : monthStart;

  // The recorded balance is "as of" the snapshot day, so only subtract spend
  // from days strictly AFTER it — otherwise we double-count spend that already
  // happened before the snapshot was taken on that day.
  const DAY_SECONDS = 86_400;
  const decrementFrom = asOfSeconds != null ? asOfSeconds + DAY_SECONDS : null;

  let today: number | null = null;
  let month: number | null = null;
  let spentSince = 0;
  let currency = "usd";

  if (adminKey) {
    const buckets = await fetchDailyBuckets(fetchStart);
    today = 0;
    month = 0;
    for (const bucket of buckets) {
      const { amount, currency: cur } = sumBucket(bucket);
      if (cur) currency = cur;
      const start = bucket.start_time;
      if (typeof start !== "number") continue;
      if (start >= monthStart) month += amount;
      if (start === dayStart) today += amount;
      if (decrementFrom != null && start >= decrementFrom) spentSince += amount;
    }
    today = Number(today.toFixed(4));
    month = Number(month.toFixed(4));
  }

  let balance: OpenAiBalance | null = null;
  if (hasBalance && initialBalance != null) {
    const asOfIso = new Date((asOfSeconds ?? dayStart) * 1000)
      .toISOString()
      .slice(0, 10);
    const remaining = Number((initialBalance - spentSince).toFixed(4));
    balance = {
      initial: initialBalance,
      asOf: asOfIso,
      spentSince: Number(spentSince.toFixed(4)),
      remaining,
      liveDecrement: Boolean(adminKey),
    };
  }

  const value: OpenAiSpend = {
    today,
    month,
    balance,
    currency,
    updatedAt: new Date().toISOString(),
  };

  cache = { value, expiresAt: Date.now() + CACHE_TTL_MS };
  return value;
}
