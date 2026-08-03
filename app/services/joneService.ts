import { JoneAgentConfig } from "../models/JoneAgentConfig";
import { getCseUniverse, type CseLiveRow } from "./cseMarketService";
import {
  normalizeGroupKind,
  presetNameForId,
  resolveGroupSymbols,
  type JoneGroupKind,
} from "./joneMarketGroups";

/** Full CSE trade summary columns (matches Analytics CSV export). */
export const TRADE_SUMMARY_COLUMNS = [
  { id: "name", label: "Company Name" },
  { id: "symbol", label: "Symbol" },
  { id: "shareVolume", label: "Share Volume" },
  { id: "tradeVolume", label: "Trade Volume" },
  { id: "previousClose", label: "Previous Close (Rs.)" },
  { id: "open", label: "Open (Rs.)" },
  { id: "high", label: "High (Rs.)" },
  { id: "low", label: "Low (Rs.)" },
  { id: "price", label: "Last Trade (Rs.)" },
  { id: "change", label: "Change (Rs.)" },
  { id: "changePct", label: "Change (%)" },
] as const;

export const MARKET_SUMMARY_COLUMNS = TRADE_SUMMARY_COLUMNS;

export const ALL_TRADE_SUMMARY_COLUMN_IDS: string[] = TRADE_SUMMARY_COLUMNS.map(
  (c) => c.id,
);

const COLUMN_BY_ID = new Map<string, (typeof TRADE_SUMMARY_COLUMNS)[number]>(
  TRADE_SUMMARY_COLUMNS.map((c) => [c.id, c]),
);

export interface JoneConfigPublic {
  columns: string[];
  groupKind: JoneGroupKind;
  watchlistId: string;
  watchlistName: string;
  watchlistSymbols: string[];
  enabled: boolean;
}

export function normalizeColumns(columns: unknown): string[] {
  if (!Array.isArray(columns)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const c of columns) {
    const id = String(c ?? "").trim();
    if (COLUMN_BY_ID.has(id) && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

export function normalizeSymbols(symbols: unknown): string[] {
  if (!Array.isArray(symbols)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const s of symbols) {
    const sym = String(s ?? "").trim().toUpperCase();
    if (sym && !seen.has(sym)) {
      seen.add(sym);
      out.push(sym);
    }
  }
  return out;
}

function toPublic(doc: Record<string, unknown>): JoneConfigPublic {
  const watchlistId = String(doc.watchlist_id ?? "");
  const inferredKind: JoneGroupKind =
    watchlistId === "top_gainers" || watchlistId === "top_losers"
      ? watchlistId
      : normalizeGroupKind(doc.group_kind);
  return {
    columns: normalizeColumns(doc.columns),
    groupKind: inferredKind,
    watchlistId,
    watchlistName: String(doc.watchlist_name ?? ""),
    watchlistSymbols: normalizeSymbols(doc.watchlist_symbols),
    enabled: Boolean(doc.enabled ?? true),
  };
}

export async function getJoneConfig(
  userId: string,
): Promise<JoneConfigPublic | null> {
  const doc = await JoneAgentConfig.findOne({ user_id: userId }).lean();
  if (!doc) return null;
  return toPublic(doc as Record<string, unknown>);
}

export async function saveJoneConfig(
  userId: string,
  columns: string[],
  groupKind: JoneGroupKind,
  watchlistId: string,
  watchlistName: string,
  watchlistSymbols: string[],
  enabled: boolean,
): Promise<JoneConfigPublic> {
  const normalizedColumns = normalizeColumns(columns);
  if (normalizedColumns.length === 0) {
    throw new Error("Select at least one trade summary column.");
  }
  const kind = normalizeGroupKind(groupKind);
  const id = String(watchlistId ?? "").trim();
  const name =
    String(watchlistName ?? "").trim() ||
    presetNameForId(id) ||
    "";
  const symbols = normalizeSymbols(watchlistSymbols);
  if (!id || !name) {
    throw new Error("Select a My List group or market preset.");
  }
  if (kind === "watchlist" && symbols.length === 0) {
    throw new Error("The selected watchlist has no companies.");
  }

  const updated = await JoneAgentConfig.findOneAndUpdate(
    { user_id: userId },
    {
      user_id: userId,
      columns: normalizedColumns,
      group_kind: kind,
      watchlist_id: id,
      watchlist_name: name,
      watchlist_symbols: kind === "watchlist" ? symbols : [],
      enabled,
    },
    { upsert: true, new: true, setDefaultsOnInsert: true },
  ).lean();

  return toPublic(updated as Record<string, unknown>);
}

export async function deleteJoneConfig(userId: string): Promise<void> {
  await JoneAgentConfig.deleteOne({ user_id: userId });
}

function symbolMatches(rowSymbol: string, watchSymbol: string): boolean {
  const row = rowSymbol.trim().toUpperCase();
  const watch = watchSymbol.trim().toUpperCase();
  if (!row || !watch) return false;
  if (row === watch) return true;
  const rowRoot = row.split(".")[0];
  const watchRoot = watch.split(".")[0];
  return rowRoot === watchRoot || row.startsWith(watchRoot);
}

function pickColumnValue(
  colId: string,
  row: CseLiveRow,
): string | number | null {
  switch (colId) {
    case "name":
      return row.name;
    case "symbol":
      return row.symbol;
    case "shareVolume":
      return row.shareVolume;
    case "tradeVolume":
      return row.tradeVolume;
    case "previousClose":
      return row.previousClose;
    case "open":
      return row.open;
    case "high":
      return row.dayHigh;
    case "low":
      return row.dayLow;
    case "price":
      return row.price;
    case "change":
      return row.change;
    case "changePct":
      return row.changePercent;
    default:
      return null;
  }
}

function resolveColumns(columnIds?: string[]) {
  const ids =
    columnIds && columnIds.length > 0
      ? columnIds
      : ALL_TRADE_SUMMARY_COLUMN_IDS;
  return ids
    .map((id) => COLUMN_BY_ID.get(id))
    .filter((c): c is (typeof TRADE_SUMMARY_COLUMNS)[number] => !!c);
}

/** Live CSE rows for the configured group. Reports always use every trade summary column. */
export async function fetchWatchlistMarketSnapshot(
  config: JoneConfigPublic,
  options?: { allColumns?: boolean },
): Promise<{
  watchlistName: string;
  columns: Array<{ id: string; label: string }>;
  rows: Array<Record<string, string | number | null>>;
  asOf: string | null;
}> {
  const columns = resolveColumns(
    options?.allColumns ? ALL_TRADE_SUMMARY_COLUMN_IDS : config.columns,
  );

  const symbols = await resolveGroupSymbols({
    groupKind: config.groupKind,
    watchlistId: config.watchlistId,
    watchlistSymbols: config.watchlistSymbols,
  });

  if (symbols.length === 0) {
    throw new Error(
      `No companies found for "${config.watchlistName}" right now.`,
    );
  }

  const universe = await getCseUniverse();
  const matched = universe.filter((row) =>
    symbols.some((sym) => symbolMatches(row.symbol, sym)),
  );

  // Preserve CSE ordering for presets (gainers/losers rank).
  const bySymbol = new Map(matched.map((row) => [row.symbol, row]));
  const ordered =
    config.groupKind === "watchlist"
      ? matched
      : symbols
          .map((sym) =>
            matched.find((row) => symbolMatches(row.symbol, sym)),
          )
          .filter((row): row is CseLiveRow => !!row);

  const rows = ordered.map((row) => {
    const out: Record<string, string | number | null> = {};
    for (const col of columns) {
      out[col.id] = pickColumnValue(col.id, row);
    }
    return out;
  });

  const asOf =
    ordered.map((r) => r.asOf).find((t) => t) ?? new Date().toISOString();

  return {
    watchlistName: config.watchlistName,
    columns: columns.map((c) => ({ id: c.id, label: c.label })),
    rows,
    asOf,
  };
}

export function buildTradeSummaryCsv(snapshot: {
  watchlistName: string;
  columns: Array<{ id: string; label: string }>;
  rows: Array<Record<string, string | number | null>>;
}): string {
  const headers = snapshot.columns.map((c) => c.label);
  const lines = [
    headers.join(","),
    ...snapshot.rows.map((row) =>
      snapshot.columns
        .map((c) => {
          const v = row[c.id];
          if (v == null) return "";
          const s = String(v);
          return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
        })
        .join(","),
    ),
  ];
  return lines.join("\r\n");
}
