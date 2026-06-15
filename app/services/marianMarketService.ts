import { MarianAgentConfig } from "../models/MarianAgentConfig";
import { MarianReport } from "../models/MarianReport";
import { generateReportFromChat } from "./openaiReport";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Marian — "Daily Market Wrap" agent.
 *
 * Marian rebuilds the Ambeon Securities DAILY MARKET WRAP from live Colombo
 * Stock Exchange (cse.lk) data. The user ticks which report components they
 * want — split into TABLE components and CHART components — and Marian assembles
 * the underlying data, derives the league tables the wrap needs (top turnover,
 * top volume, market breadth, crossings, foreign flow) and asks OpenAI to render
 * a clean, branded Markdown report (tables + chart blocks).
 *
 * Every figure is grounded in real CSE data; nothing is invented.
 * ────────────────────────────────────────────────────────────────────────── */

const CSE_BASE = "https://www.cse.lk/api";

export type MarianSectionKind = "table" | "chart";

/**
 * Catalog of report components.
 *  • `kind` groups them into "Tables" and "Charts" in the configuration UI.
 *  • `coverage` documents how completely live CSE data can populate the item:
 *      - "full":    fully available from CSE
 *      - "partial": only partially available (e.g. just today + previous day)
 *      - "derived": not a direct field — computed from the trade summary
 */
export const MARIAN_SECTIONS = [
  // ── Tables (in the original Daily Market Wrap order) ──────────────────────
  {
    id: "market_statistics",
    label: "Market statistics (today vs previous day)",
    kind: "table",
    coverage: "full",
  },
  {
    id: "foreign_activity",
    label: "Foreign activity (buying / selling / net flow)",
    kind: "table",
    coverage: "full",
  },
  {
    id: "returns",
    label: "Index returns (YTD / 1-month / 1-year)",
    kind: "table",
    coverage: "none",
  },
  {
    id: "top_turnover",
    label: "Top 10 turnover (LKR)",
    kind: "table",
    coverage: "derived",
  },
  {
    id: "top_volume",
    label: "Top 10 volume",
    kind: "table",
    coverage: "derived",
  },
  {
    id: "contributors",
    label: "Top 5 positive / negative contributors",
    kind: "table",
    coverage: "derived",
  },
  {
    id: "crossings",
    label: "Crossings (large negotiated deals)",
    kind: "table",
    coverage: "partial",
  },
  {
    id: "dividends",
    label: "Dividends (amount / description / XD date)",
    kind: "table",
    coverage: "none",
  },
  // ── Charts ─────────────────────────────────────────────────────────────────
  {
    id: "aspi_chart",
    label: "ASPI index trend (line)",
    kind: "chart",
    coverage: "partial",
  },
  {
    id: "snp_chart",
    label: "S&P SL20 index trend (line)",
    kind: "chart",
    coverage: "partial",
  },
  {
    id: "turnover_volume_chart",
    label: "Turnover & volume (bar + line)",
    kind: "chart",
    coverage: "partial",
  },
  {
    id: "foreign_buying_chart",
    label: "Top 5 foreign buying (bar)",
    kind: "chart",
    coverage: "none",
  },
  {
    id: "foreign_selling_chart",
    label: "Top 5 foreign selling (bar)",
    kind: "chart",
    coverage: "none",
  },
] as const;

export type MarianSectionId = (typeof MARIAN_SECTIONS)[number]["id"];
type MarianSection = (typeof MARIAN_SECTIONS)[number];

const SECTION_BY_ID = new Map<string, MarianSection>(
  MARIAN_SECTIONS.map((s) => [s.id, s]),
);

export function normalizeSections(sections: unknown): string[] {
  if (!Array.isArray(sections)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const s of sections) {
    const id = String(s ?? "").trim();
    if (SECTION_BY_ID.has(id) && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

/* ── CSE fetch ──────────────────────────────────────────────────────────── */

async function callCSE(endpoint: string): Promise<unknown> {
  try {
    const res = await fetch(`${CSE_BASE}/${endpoint}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json, text/plain, */*",
        "User-Agent":
          "Mozilla/5.0 (compatible; AmbeonConsole/1.0; +https://www.cse.lk)",
      },
      body: "",
    });
    const text = await res.text();
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  } catch {
    return null;
  }
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** A single trade-summary row (only the fields we use). */
interface TradeRow {
  symbol?: string;
  name?: string;
  price?: number;
  previousClose?: number;
  change?: number;
  percentageChange?: number;
  turnover?: number;
  sharevolume?: number;
  marketCap?: number;
  crossingVolume?: number;
  crossingTradeVol?: number;
}

/* ── Daily market summary (today + previous day) ─────────────────────────── */

interface DailySummary {
  tradeDate: number | null;
  marketTurnover: number | null;
  marketCap: number | null;
  shareVolume: number | null;
  trades: number | null;
  asi: number | null;
  spsl20: number | null;
  per: number | null;
  pbv: number | null;
  dy: number | null;
  foreignPurchase: number | null;
  foreignSales: number | null;
  foreignNet: number | null;
}

function toDailySummary(row: Record<string, unknown>): DailySummary {
  const fp = num(row.equityForeignPurchase);
  const fs = num(row.equityForeignSales);
  return {
    tradeDate: num(row.tradeDate),
    marketTurnover: num(row.marketTurnover),
    marketCap: num(row.marketCap),
    shareVolume: num(row.volumeOfTurnOverNumber),
    trades: num(row.marketTrades),
    asi: num(row.asi),
    spsl20: num(row.spt) ?? num(row.spp),
    per: num(row.per),
    pbv: num(row.pbv),
    dy: num(row.dy),
    foreignPurchase: fp,
    foreignSales: fs,
    foreignNet: fp !== null && fs !== null ? fp - fs : null,
  };
}

/* ── Derived league tables from the full trade summary ───────────────────── */

function deriveFromTradeSummary(rows: TradeRow[]) {
  const clean = rows.filter((r) => r && r.symbol);

  const topTurnover = [...clean]
    .filter((r) => num(r.turnover))
    .sort((a, b) => (b.turnover ?? 0) - (a.turnover ?? 0))
    .slice(0, 10)
    .map((r) => ({
      symbol: r.symbol,
      name: r.name,
      turnover: r.turnover,
      price: r.price ?? null,
      changePercent: r.percentageChange ?? null,
    }));

  const topVolume = [...clean]
    .filter((r) => num(r.sharevolume))
    .sort((a, b) => (b.sharevolume ?? 0) - (a.sharevolume ?? 0))
    .slice(0, 10)
    .map((r) => ({
      symbol: r.symbol,
      name: r.name,
      shareVolume: r.sharevolume,
      price: r.price ?? null,
      changePercent: r.percentageChange ?? null,
    }));

  const crossings = [...clean]
    .filter((r) => (r.crossingVolume ?? 0) > 0)
    .sort((a, b) => (b.crossingVolume ?? 0) - (a.crossingVolume ?? 0))
    .slice(0, 10)
    .map((r) => ({
      symbol: r.symbol,
      name: r.name,
      price: r.price ?? null,
      crossingVolume: r.crossingVolume,
      crossingTrades: r.crossingTradeVol ?? null,
      approxValue:
        num(r.crossingVolume) !== null && num(r.price) !== null
          ? Math.round((r.crossingVolume as number) * (r.price as number))
          : null,
    }));

  let advancers = 0;
  let decliners = 0;
  let unchanged = 0;
  for (const r of clean) {
    const ch = num(r.change);
    if (ch === null || ch === 0) unchanged += 1;
    else if (ch > 0) advancers += 1;
    else decliners += 1;
  }

  return {
    listed: clean.length,
    advancers,
    decliners,
    unchanged,
    topTurnover,
    topVolume,
    crossings,
    rows: clean,
  };
}

/**
 * Approximate each stock's contribution to the ASPI move, in index points.
 *
 * The CSE feed does not publish true float-adjusted index-point contributions,
 * so we approximate: a stock's change in market cap is `mcap * (change/price)`,
 * and its index-point contribution ≈ (deltaMcap / totalPrevMarketCap) * prevASI.
 * Returns the top 5 positive and top 5 negative movers plus the counts.
 */
function deriveContributors(
  rows: TradeRow[],
  prevAsi: number | null,
  prevTotalMcap: number | null,
): {
  positive: { symbol?: string; name?: string; points: number }[];
  negative: { symbol?: string; name?: string; points: number }[];
  positiveCount: number;
  negativeCount: number;
} | null {
  if (!prevAsi || !prevTotalMcap) return null;
  type Contribution = { symbol?: string; name?: string; points: number };
  const scored: Contribution[] = [];
  for (const r of rows) {
    const mcap = num(r.marketCap);
    const price = num(r.price);
    const change = num(r.change);
    if (mcap === null || price === null || price === 0 || change === null) continue;
    const deltaMcap = mcap * (change / price);
    const points = (deltaMcap / prevTotalMcap) * prevAsi;
    if (!Number.isFinite(points)) continue;
    scored.push({ symbol: r.symbol, name: r.name, points });
  }

  const positiveCount = scored.filter((x) => x.points > 0).length;
  const negativeCount = scored.filter((x) => x.points < 0).length;
  const positive = [...scored]
    .sort((a, b) => b.points - a.points)
    .slice(0, 5);
  const negative = [...scored]
    .sort((a, b) => a.points - b.points)
    .slice(0, 5);
  return { positive, negative, positiveCount, negativeCount };
}

/* ── Which raw CSE endpoints does a set of sections need? ─────────────────── */

const ENDPOINT_FOR_SECTION: Record<string, string[]> = {
  market_statistics: ["dailyMarketSummery", "aspiData", "snpData"],
  foreign_activity: ["dailyMarketSummery"],
  returns: [],
  top_turnover: ["tradeSummary"],
  top_volume: ["tradeSummary"],
  contributors: ["tradeSummary", "dailyMarketSummery"],
  crossings: ["tradeSummary"],
  dividends: [],
  aspi_chart: ["dailyMarketSummery", "aspiData"],
  snp_chart: ["dailyMarketSummery", "snpData"],
  turnover_volume_chart: ["dailyMarketSummery"],
  foreign_buying_chart: [],
  foreign_selling_chart: [],
};

export interface MarianDataset {
  sections: string[];
  asOf: string;
  /** Map of section id -> the data the model should render for it. */
  data: Record<string, unknown>;
  /** Requested sections that came back with no usable data. */
  unavailable: string[];
}

/**
 * Fetch every CSE slice the requested sections need (once each), then build a
 * compact per-section dataset with the league tables already derived.
 */
export async function buildMarianDataset(
  sections: string[],
): Promise<MarianDataset> {
  const wanted = normalizeSections(sections);

  const endpoints = new Set<string>();
  for (const id of wanted) {
    for (const e of ENDPOINT_FOR_SECTION[id] ?? []) endpoints.add(e);
  }
  const endpointList = [...endpoints];
  const results = await Promise.all(endpointList.map((e) => callCSE(e)));
  const raw: Record<string, unknown> = {};
  endpointList.forEach((e, i) => {
    raw[e] = results[i];
  });

  // Daily summary: [[today], [prev]]
  const dms = raw.dailyMarketSummery as unknown[][] | undefined;
  const today =
    Array.isArray(dms) && Array.isArray(dms[0]) && dms[0][0]
      ? toDailySummary(dms[0][0] as Record<string, unknown>)
      : null;
  const prev =
    Array.isArray(dms) && Array.isArray(dms[1]) && dms[1][0]
      ? toDailySummary(dms[1][0] as Record<string, unknown>)
      : null;

  const tradeRows =
    ((raw.tradeSummary as { reqTradeSummery?: TradeRow[] } | undefined)
      ?.reqTradeSummery as TradeRow[] | undefined) ?? [];
  const derived = tradeRows.length > 0 ? deriveFromTradeSummary(tradeRows) : null;
  const contributors =
    derived && tradeRows.length > 0
      ? deriveContributors(derived.rows, prev?.asi ?? null, prev?.marketCap ?? null)
      : null;

  const aspi = raw.aspiData as Record<string, unknown> | undefined;
  const snp = raw.snpData as Record<string, unknown> | undefined;

  const data: Record<string, unknown> = {};
  const unavailable: string[] = [];

  const set = (id: string, value: unknown, present: boolean) => {
    if (present) data[id] = value;
    else unavailable.push(id);
  };

  for (const id of wanted) {
    switch (id) {
      case "market_statistics":
        set(
          id,
          {
            today,
            previous: prev,
            liveAspi: aspi ? { value: num(aspi.value), changePercent: num(aspi.percentage) } : null,
            liveSnp: snp ? { value: num(snp.value), changePercent: num(snp.percentage) } : null,
            note: "Turnover & market cap are in LKR. PE = market PER, PBV = price-to-book.",
          },
          Boolean(today),
        );
        break;
      case "foreign_activity":
        set(
          id,
          {
            today: today
              ? {
                  buying: today.foreignPurchase,
                  selling: today.foreignSales,
                  net: today.foreignNet,
                }
              : null,
            previous: prev
              ? {
                  buying: prev.foreignPurchase,
                  selling: prev.foreignSales,
                  net: prev.foreignNet,
                }
              : null,
            note: "Market-level foreign equity flows in LKR. Per-company foreign buying/selling is not exposed by the CSE feed.",
          },
          Boolean(today?.foreignPurchase !== null || today?.foreignSales !== null),
        );
        break;
      case "returns":
        // Index YTD / 1-month / 1-year returns are not exposed by the CSE feed.
        // Rendered as a placeholder table (dashes) to mirror the original layout.
        set(
          id,
          {
            available: false,
            note: "Index YTD / 1-month / 1-year returns are not available from the live CSE feed.",
          },
          false,
        );
        break;
      case "top_turnover":
        set(id, derived?.topTurnover, Boolean(derived?.topTurnover?.length));
        break;
      case "top_volume":
        set(id, derived?.topVolume, Boolean(derived?.topVolume?.length));
        break;
      case "contributors":
        set(
          id,
          contributors
            ? {
                ...contributors,
                note: "Index-point contributions are approximated from each stock's market cap × price change (the CSE feed does not publish exact float-adjusted contributions).",
              }
            : null,
          Boolean(contributors),
        );
        break;
      case "dividends":
        // Dividend announcements are not exposed by the live CSE feed.
        // Rendered as a placeholder table (dashes) to mirror the original layout.
        set(
          id,
          {
            available: false,
            note: "Dividend amount / description / XD date are not available from the live CSE feed.",
          },
          false,
        );
        break;
      case "crossings":
        set(
          id,
          derived
            ? {
                crossings: derived.crossings,
                note: "Aggregated per-company crossing volume (with approximate value = volume × price). The CSE feed does not expose each individual crossing deal.",
              }
            : null,
          Boolean(derived?.crossings?.length),
        );
        break;
      case "aspi_chart":
        set(
          id,
          {
            points: [
              prev ? { label: "Prev. day", value: prev.asi } : null,
              today ? { label: "Today", value: today.asi ?? (aspi ? num(aspi.value) : null) } : null,
            ].filter(Boolean),
            note: "Only today and the previous day are available from the daily summary; a longer 5-day index history is NOT exposed by the feed.",
          },
          Boolean(today?.asi || aspi?.value),
        );
        break;
      case "snp_chart":
        set(
          id,
          {
            points: [
              prev ? { label: "Prev. day", value: prev.spsl20 } : null,
              today ? { label: "Today", value: today.spsl20 ?? (snp ? num(snp.value) : null) } : null,
            ].filter(Boolean),
            note: "Only today and the previous day are available; a longer history is NOT exposed by the feed.",
          },
          Boolean(today?.spsl20 || snp?.value),
        );
        break;
      case "turnover_volume_chart":
        set(
          id,
          {
            points: [
              prev
                ? {
                    label: "Prev. day",
                    turnover: prev.marketTurnover,
                    volume: prev.shareVolume,
                  }
                : null,
              today
                ? {
                    label: "Today",
                    turnover: today.marketTurnover,
                    volume: today.shareVolume,
                  }
                : null,
            ].filter(Boolean),
            note: "Only today and the previous day are available from the feed.",
          },
          Boolean(today?.marketTurnover),
        );
        break;
      case "foreign_buying_chart":
        // Per-company foreign buying is not exposed by the live CSE feed.
        set(
          id,
          {
            available: false,
            note: "Top 5 foreign buying by company is not available from the live CSE feed (only market-level foreign flow is exposed).",
          },
          false,
        );
        break;
      case "foreign_selling_chart":
        set(
          id,
          {
            available: false,
            note: "Top 5 foreign selling by company is not available from the live CSE feed (only market-level foreign flow is exposed).",
          },
          false,
        );
        break;
      default:
        break;
    }
  }

  return {
    sections: wanted,
    asOf: new Date().toISOString(),
    data,
    unavailable,
  };
}

/* ── Report generation ──────────────────────────────────────────────────── */

function buildInstructions(): string {
  return [
    'You are "Marian", the Daily Market Wrap analyst for Ambeon Securities (Colombo Stock Exchange, Sri Lanka).',
    "You produce a professional DAILY MARKET WRAP from the structured live CSE data provided. Use ONLY the data given — never invent figures.",
    "",
    "Output format (Markdown, NO '#' heading marks — use short **bold** lines for section labels):",
    "  • Line 1: a bold title: **DAILY MARKET WRAP — <date>**.",
    "  • Then a 2–3 sentence market overview: was the market up or down, the ASPI and S&P SL20 moves, turnover, and overall tone.",
    "  • Then render EACH requested component in the order given.",
    "",
    "Rendering rules per component kind:",
    "  • TABLE components → a compact GitHub-style Markdown table with a one-line **bold** label above it. Show 'Today' vs 'Previous day' columns where both are present, and a change/% column when meaningful.",
    "  • CHART components → a fenced ```chart block containing ONE JSON object: {\"type\":\"bar|line|area\",\"title\":...,\"unit\":...,\"categories\":[...],\"series\":[{\"name\":...,\"values\":[numbers]}]}. values MUST be plain numbers (no commas/currency). Use a line chart for index/turnover trends and a bar chart for gainers/losers and foreign flow.",
    "",
    "Number formatting: turnover and market cap are in LKR — present them compactly (e.g. LKR 2.61Bn or LKR 2,613 Mn). Keep percentages to 2 decimals. Round share volumes to millions where large.",
    "",
    "Data availability (IMPORTANT): some components may come back with partial data or a `note` flagging a limitation (e.g. only today + previous day available for an index trend). Render what you can and add a short *italic* caveat line beneath it. Never fabricate missing values.",
    "",
    "End with a one-line closing note and a brief italic disclaimer: '*For informational purposes only; not investment advice.*'",
  ].join("\n");
}

function buildInput(dataset: MarianDataset): string {
  const labels = dataset.sections
    .map((id) => SECTION_BY_ID.get(id)?.label ?? id)
    .join(", ");
  let json = JSON.stringify(dataset.data);
  const MAX = 24000;
  if (json.length > MAX) json = `${json.slice(0, MAX)}…(truncated)`;
  const lines = [
    `Date: ${new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "long", year: "numeric" })}`,
    `Requested components (render in this order): ${labels}`,
  ];
  if (dataset.unavailable.length > 0) {
    const unLabels = dataset.unavailable
      .map((id) => SECTION_BY_ID.get(id)?.label ?? id)
      .join(", ");
    lines.push(
      `Components with NO usable live data right now (note this briefly, do not fabricate): ${unLabels}`,
    );
  }
  lines.push(
    "",
    "Structured live CSE data (JSON, keyed by component id):",
    json,
    "",
    "Write the Daily Market Wrap now.",
  );
  return lines.join("\n");
}

export async function generateMarianReport(
  sections: string[],
): Promise<{ report: string; sections: string[]; unavailable: string[] }> {
  const wanted = normalizeSections(sections);
  if (wanted.length === 0) {
    throw new Error("Select at least one report component.");
  }
  const dataset = await buildMarianDataset(wanted);
  const report = await generateReportFromChat({
    instructions: buildInstructions(),
    input: buildInput(dataset),
  });
  return { report, sections: wanted, unavailable: dataset.unavailable };
}

/* ── Config CRUD ────────────────────────────────────────────────────────── */

export async function getMarianConfig(userId: string) {
  const doc = await MarianAgentConfig.findOne({ user_id: userId }).lean();
  if (!doc) return null;
  return {
    sections: doc.sections ?? [],
    enabled: doc.enabled ?? true,
  };
}

export async function saveMarianConfig(
  userId: string,
  sections: string[],
  enabled: boolean,
) {
  const normalized = normalizeSections(sections);
  const doc = await MarianAgentConfig.findOneAndUpdate(
    { user_id: userId },
    { $set: { sections: normalized, enabled } },
    { new: true, upsert: true },
  ).lean();
  return {
    sections: doc.sections ?? [],
    enabled: doc.enabled ?? true,
  };
}

export async function deleteMarianConfig(userId: string) {
  await MarianAgentConfig.deleteOne({ user_id: userId });
}

/* ── Reports persistence ────────────────────────────────────────────────── */

export async function saveMarianReport(
  userId: string,
  content: string,
  sections: string[],
  source: "scheduled" | "manual",
) {
  const doc = await MarianReport.create({
    user_id: userId,
    content,
    sections,
    source,
  });
  return doc.toObject();
}

export async function listMarianReports(userId: string, limit = 20) {
  return MarianReport.find({ user_id: userId })
    .sort({ createdAt: -1 })
    .limit(limit)
    .lean();
}

export async function runDailyMarianReportsForAllUsers(): Promise<number> {
  const configs = await MarianAgentConfig.find({ enabled: true }).lean();
  let generated = 0;
  for (const config of configs) {
    const sections = config.sections ?? [];
    if (sections.length === 0) continue;
    try {
      const { report } = await generateMarianReport(sections);
      if (report.trim()) {
        await saveMarianReport(config.user_id, report, sections, "scheduled");
        generated += 1;
      }
    } catch (err) {
      logger.error(`[Marian] Failed daily report for user ${config.user_id}`, err);
    }
  }
  return generated;
}
