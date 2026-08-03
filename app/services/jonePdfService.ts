import { jsPDF } from "jspdf";
import autoTable from "jspdf-autotable";

import type { JoneGroupKind } from "./joneMarketGroups";
import type { JoneConfigPublic } from "./joneService";
import {
  ALL_TRADE_SUMMARY_COLUMN_IDS,
  buildTradeSummaryCsv,
  fetchWatchlistMarketSnapshot,
} from "./joneService";

/* ────────────────────────────────────────────────────────────────────────── *
 * Jone — My List trade summary PDF/CSV (server-side, for download + email).
 * ────────────────────────────────────────────────────────────────────────── */

const DASH = "-";

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(v);
}

function fmtInt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(v);
}

function fmtPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtChange(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return `${v > 0 ? "+" : ""}${fmtNum(v)}`;
}

function cellValue(
  colId: string,
  row: Record<string, string | number | null>,
): string {
  const v = row[colId];
  switch (colId) {
    case "name":
    case "symbol":
      return String(v ?? DASH);
    case "shareVolume":
    case "tradeVolume":
      return fmtInt(typeof v === "number" ? v : null);
    case "previousClose":
    case "open":
    case "high":
    case "low":
    case "price":
      return fmtNum(typeof v === "number" ? v : null);
    case "change":
      return fmtChange(typeof v === "number" ? v : null);
    case "changePct":
      return fmtPct(typeof v === "number" ? v : null);
    default:
      return v == null ? DASH : String(v);
  }
}

export function buildJoneWatchlistPdfBuffer(input: {
  watchlistName: string;
  columns: Array<{ id: string; label: string }>;
  rows: Array<Record<string, string | number | null>>;
  asOf: string | null;
}): Buffer {
  const doc = new jsPDF({ orientation: "landscape", unit: "pt", format: "a4" });
  const pageW = doc.internal.pageSize.getWidth();
  const margin = 28;
  const stamp = (input.asOf ? new Date(input.asOf) : new Date()).toLocaleString(
    "en-LK",
    { dateStyle: "medium", timeStyle: "short" },
  );

  doc.setFont("helvetica", "bold");
  doc.setFontSize(13);
  doc.text(`${input.watchlistName} — Trade Summary`, margin, 34);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(8);
  doc.setTextColor(110, 116, 128);
  doc.text(`John · Ambeon Console · Live CSE data · ${stamp}`, margin, 48);
  doc.setTextColor(0, 0, 0);

  const headers = input.columns.map((c) => c.label);
  const body = input.rows.map((row) =>
    input.columns.map((c) => cellValue(c.id, row)),
  );

  autoTable(doc, {
    startY: 58,
    head: [headers],
    body,
    margin: { left: margin, right: margin },
    styles: { fontSize: 7, cellPadding: 3, overflow: "linebreak" },
    headStyles: {
      fillColor: [109, 40, 217],
      textColor: 255,
      fontStyle: "bold",
      fontSize: 7,
    },
    alternateRowStyles: { fillColor: [248, 250, 252] },
    tableWidth: pageW - margin * 2,
  });

  const arrayBuf = doc.output("arraybuffer");
  return Buffer.from(arrayBuf);
}

export async function buildJoneReportPdfFromConfig(
  config: JoneConfigPublic,
): Promise<Buffer> {
  const snapshot = await fetchWatchlistMarketSnapshot(config, {
    allColumns: true,
  });
  return buildJoneWatchlistPdfBuffer(snapshot);
}

export async function buildJoneReportPdfFromPayload(input: {
  groupKind: JoneGroupKind;
  watchlistId: string;
  watchlistName: string;
  watchlistSymbols: string[];
}): Promise<Buffer> {
  const config: JoneConfigPublic = {
    columns: ALL_TRADE_SUMMARY_COLUMN_IDS,
    groupKind: input.groupKind,
    watchlistId: input.watchlistId,
    watchlistName: input.watchlistName,
    watchlistSymbols: input.watchlistSymbols,
    enabled: true,
  };
  return buildJoneReportPdfFromConfig(config);
}

export async function buildJoneReportCsvFromPayload(input: {
  groupKind: JoneGroupKind;
  watchlistId: string;
  watchlistName: string;
  watchlistSymbols: string[];
}): Promise<string> {
  const config: JoneConfigPublic = {
    columns: ALL_TRADE_SUMMARY_COLUMN_IDS,
    groupKind: input.groupKind,
    watchlistId: input.watchlistId,
    watchlistName: input.watchlistName,
    watchlistSymbols: input.watchlistSymbols,
    enabled: true,
  };
  const snapshot = await fetchWatchlistMarketSnapshot(config, {
    allColumns: true,
  });
  return buildTradeSummaryCsv(snapshot);
}
