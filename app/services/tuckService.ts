import { TuckAgentConfig } from "../models/TuckAgentConfig";
import { TuckReport } from "../models/TuckReport";
import { generateReportFromChat } from "./openaiReport";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Tuck — daily CSE market-announcement agent.
 *
 * The user ticks which market data slices they care about. Tuck fetches those
 * slices live from the Colombo Stock Exchange (cse.lk) and uses OpenAI to write
 * a daily market summary report. A scheduler runs this automatically once a day
 * for every user who has the agent enabled.
 * ────────────────────────────────────────────────────────────────────────── */

const CSE_BASE = "https://www.cse.lk/api";

/** Catalog of report sections and the CSE endpoint(s) backing each. */
export const TUCK_SECTIONS = [
  { id: "market_summary", label: "Market summary", endpoints: ["marketSummery", "marketStatus"] },
  { id: "aspi", label: "ASPI index", endpoints: ["aspiData"] },
  { id: "snp", label: "S&P SL20 index", endpoints: ["snpData"] },
  { id: "daily_history", label: "Daily market summary history", endpoints: ["dailyMarketSummery"] },
  { id: "top_gainers", label: "Top gainers", endpoints: ["topGainers"] },
  { id: "top_losers", label: "Top losers", endpoints: ["topLooses"] },
  { id: "most_active", label: "Most active trades", endpoints: ["mostActiveTrades"] },
  { id: "today_prices", label: "Today's share prices", endpoints: ["todaySharePrice"] },
] as const;

export type TuckSectionId = (typeof TUCK_SECTIONS)[number]["id"];

type TuckSection = (typeof TUCK_SECTIONS)[number];
const SECTION_BY_ID = new Map<string, TuckSection>(
  TUCK_SECTIONS.map((s) => [s.id, s]),
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

export function normalizeCompanies(companies: unknown): string[] {
  if (!Array.isArray(companies)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const c of companies) {
    const name = String(c ?? "").trim();
    if (name && !seen.has(name)) {
      seen.add(name);
      out.push(name);
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

/** Fetch all CSE data slices for the requested sections. */
export async function fetchCseData(
  sections: string[],
): Promise<Record<string, unknown>> {
  const endpoints = new Set<string>();
  for (const id of sections) {
    const section = SECTION_BY_ID.get(id);
    if (section) for (const e of section.endpoints) endpoints.add(e);
  }

  const list = [...endpoints];
  const results = await Promise.all(list.map((e) => callCSE(e)));

  const data: Record<string, unknown> = {};
  list.forEach((endpoint, i) => {
    data[endpoint] = results[i];
  });
  return data;
}

/* ── Report generation ──────────────────────────────────────────────────── */

function buildInstructions(): string {
  return [
    'You are "Tuck", a market-announcement assistant for the Ambeon Console.',
    "You write a concise DAILY market summary for the Colombo Stock Exchange (CSE), Sri Lanka, from the live JSON data provided.",
    "Only use the data given — do not invent figures. If a section's data is empty or missing, note that briefly.",
    "",
    "Formatting rules:",
    "  • Start with a **bold** title line that includes today's date, then a 1–2 sentence market overview (was the market up or down, overall tone).",
    "  • Add a short **bold** sub-section for each requested data slice with the key numbers (use compact figures, e.g. LKR 2.4B).",
    "  • For gainers / losers / most active, use small Markdown tables (symbol, price, change %).",
    "  • Round sensibly and keep it scannable. End with a one-line closing note.",
    "  • Do NOT use Markdown heading marks (#, ##). Use short **bold** lines for section labels.",
  ].join("\n");
}

function buildInput(
  sections: string[],
  companies: string[],
  data: Record<string, unknown>,
): string {
  const labels = sections
    .map((id) => SECTION_BY_ID.get(id)?.label ?? id)
    .join(", ");
  // Cap the serialised payload so a huge today-prices list can't blow context.
  let json = JSON.stringify(data);
  const MAX = 24000;
  if (json.length > MAX) json = `${json.slice(0, MAX)}…(truncated)`;
  const lines = [
    `Requested report sections: ${labels}`,
    `Date: ${new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "long", year: "numeric" })}`,
  ];
  if (companies.length > 0) {
    lines.push(
      `Companies of interest (highlight these if they appear in the data, e.g. their share price / movement): ${companies.join(", ")}`,
    );
  }
  lines.push(
    "",
    "Live CSE data (JSON):",
    json,
    "",
    "Write the daily market summary report now.",
  );
  return lines.join("\n");
}

export async function generateTuckReport(
  sections: string[],
  companies: string[] = [],
): Promise<{ report: string; sections: string[]; companies: string[] }> {
  const wanted = normalizeSections(sections);
  if (wanted.length === 0) {
    throw new Error("Select at least one market data section.");
  }
  const focus = normalizeCompanies(companies);
  const data = await fetchCseData(wanted);
  const report = await generateReportFromChat({
    instructions: buildInstructions(),
    input: buildInput(wanted, focus, data),
  });
  return { report, sections: wanted, companies: focus };
}

/* ── Config CRUD ────────────────────────────────────────────────────────── */

export async function getTuckConfig(userId: string) {
  const doc = await TuckAgentConfig.findOne({ user_id: userId }).lean();
  if (!doc) return null;
  return {
    sections: doc.sections ?? [],
    companies: doc.companies ?? [],
    enabled: doc.enabled ?? true,
  };
}

export async function saveTuckConfig(
  userId: string,
  sections: string[],
  companies: string[],
  enabled: boolean,
) {
  const normalizedSections = normalizeSections(sections);
  const normalizedCompanies = normalizeCompanies(companies);
  const doc = await TuckAgentConfig.findOneAndUpdate(
    { user_id: userId },
    {
      $set: {
        sections: normalizedSections,
        companies: normalizedCompanies,
        enabled,
      },
    },
    { new: true, upsert: true },
  ).lean();
  return {
    sections: doc.sections ?? [],
    companies: doc.companies ?? [],
    enabled: doc.enabled ?? true,
  };
}

export async function deleteTuckConfig(userId: string) {
  await TuckAgentConfig.deleteOne({ user_id: userId });
}

/* ── Reports persistence ────────────────────────────────────────────────── */

export async function saveTuckReport(
  userId: string,
  content: string,
  sections: string[],
  source: "scheduled" | "manual",
) {
  const doc = await TuckReport.create({
    user_id: userId,
    content,
    sections,
    source,
  });
  return doc.toObject();
}

export async function listTuckReports(userId: string, limit = 20) {
  return TuckReport.find({ user_id: userId })
    .sort({ createdAt: -1 })
    .limit(limit)
    .lean();
}

/* ── Scheduled daily run (used by tuckScheduler) ────────────────────────── */

export async function runDailyReportsForAllUsers(): Promise<number> {
  const configs = await TuckAgentConfig.find({ enabled: true }).lean();
  let generated = 0;
  for (const config of configs) {
    const sections = config.sections ?? [];
    if (sections.length === 0) continue;
    try {
      const { report } = await generateTuckReport(
        sections,
        config.companies ?? [],
      );
      if (report.trim()) {
        await saveTuckReport(config.user_id, report, sections, "scheduled");
        generated += 1;
      }
    } catch (err) {
      logger.error(
        `[Tuck] Failed daily report for user ${config.user_id}`,
        err,
      );
    }
  }
  return generated;
}
