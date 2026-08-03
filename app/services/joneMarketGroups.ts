/* ────────────────────────────────────────────────────────────────────────── *
 * Jone — market summary group sources (My List watchlists + CSE presets).
 * ────────────────────────────────────────────────────────────────────────── */

const CSE_BASE = "https://www.cse.lk/api";

export type JoneGroupKind = "watchlist" | "top_gainers" | "top_losers";

export const JONE_PRESET_GROUPS = [
  { id: "top_gainers", kind: "top_gainers" as const, name: "Top Gainers" },
  { id: "top_losers", kind: "top_losers" as const, name: "Top Losers" },
] as const;

const PRESET_BY_ID = new Map<string, (typeof JONE_PRESET_GROUPS)[number]>(
  JONE_PRESET_GROUPS.map((g) => [g.id, g]),
);

export function normalizeGroupKind(value: unknown): JoneGroupKind {
  const v = String(value ?? "").trim();
  if (v === "top_gainers" || v === "top_losers") return v;
  return "watchlist";
}

export function isPresetGroupId(id: string): boolean {
  return id === "top_gainers" || id === "top_losers";
}

export function presetNameForId(id: string): string | null {
  return PRESET_BY_ID.get(id)?.name ?? null;
}

async function callCseEndpoint(endpoint: string): Promise<unknown> {
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

function extractSymbols(data: unknown): string[] {
  if (!Array.isArray(data)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const row of data) {
    if (!row || typeof row !== "object") continue;
    const sym = String((row as { symbol?: unknown }).symbol ?? "").trim();
    if (sym && !seen.has(sym)) {
      seen.add(sym);
      out.push(sym);
    }
  }
  return out;
}

export async function fetchPresetSymbols(
  kind: "top_gainers" | "top_losers",
): Promise<string[]> {
  const endpoint = kind === "top_gainers" ? "topGainers" : "topLooses";
  const data = await callCseEndpoint(endpoint);
  return extractSymbols(data);
}

export async function resolveGroupSymbols(input: {
  groupKind: JoneGroupKind;
  watchlistId: string;
  watchlistSymbols: string[];
}): Promise<string[]> {
  if (input.groupKind === "top_gainers") {
    return fetchPresetSymbols("top_gainers");
  }
  if (input.groupKind === "top_losers") {
    return fetchPresetSymbols("top_losers");
  }
  return input.watchlistSymbols;
}

export async function countPresetSymbols(
  kind: "top_gainers" | "top_losers",
): Promise<number> {
  const symbols = await fetchPresetSymbols(kind);
  return symbols.length;
}
