import { FinancialTable } from "../models/FinancialTable";
import { Company } from "../models/Company";
import type { PeriodLabel } from "./extractedService";

function reportTypeFromPeriod(period: PeriodLabel): "annual" | "quarterly" {
  return period === "Quarterly" ? "quarterly" : "annual";
}

export const DEFAULT_SUMMARY_YEARS = [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025];

export type YearlySummaryRow = {
  label: string;
  style: string;
  values: Record<string, string | null>;
  /** True when this line exists only in older years (not in the newest reference year). */
  priorYearsOnly?: boolean;
};

export type YearlySummaryPayload = {
  company: string;
  displayName: string;
  statementKey: string;
  statementTitle: string | null;
  period: PeriodLabel;
  years: number[];
  rows: YearlySummaryRow[];
  yearsWithData: number[];
  /** Newest year whose row list defines the main (non-prior) block. */
  referenceYear?: number | null;
};

type TableDoc = {
  statement_key: string;
  statement_title?: string | null;
  header_rows?: string[][];
  rows?: Array<{ cells?: string[]; style?: string }>;
};

function parseNumber(val: unknown): number | null {
  if (val == null) return null;
  if (typeof val === "number") return Number.isFinite(val) ? val : null;
  let s = String(val).trim();
  if (!s || s === "-" || s === "—" || s === "N/A") return null;
  const neg = s.startsWith("(") && s.endsWith(")");
  if (neg) s = s.slice(1, -1);
  s = s.replace(/,/g, "").replace(/\s/g, "");
  const n = Number(s);
  if (!Number.isFinite(n)) return null;
  return neg ? -n : n;
}

function normLabel(label: string): string {
  return label
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[:\-–—]+/g, " ")
    .trim();
}

/** Stronger key used to align near-identical line items across years. */
function canonicalizeLabel(label: string): string {
  return label
    .toLowerCase()
    .replace(/\([^)]*\)/g, " ")
    .replace(/[/\\|]/g, " ")
    // Treat common presentation variants as the same line.
    .replace(/\breversals?\b/g, "expense")
    .replace(/\bexpenses\b/g, "expense")
    .replace(/\blosses\b/g, "loss")
    .replace(/\b(lkr|rs\.?|of|the|a|an|for|from|to|and|&|at|in|on|with)\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function labelTokens(label: string): Set<string> {
  const raw = canonicalizeLabel(label)
    .split(" ")
    .filter((t) => t.length > 1);
  return new Set(raw);
}

function tokenJaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  let inter = 0;
  for (const t of a) if (b.has(t)) inter += 1;
  const union = a.size + b.size - inter;
  return union === 0 ? 0 : inter / union;
}

type YearEntry = { label: string; value: string; style: string };

/**
 * Map an older-year label onto a newest-year row key when wording differs
 * but the line is clearly the same (e.g. "Operating Profit" vs "Operating Profit/(Loss)").
 */
function matchToReferenceKey(
  label: string,
  referenceKeys: string[],
  referenceMeta: Map<string, YearEntry>,
  alreadyMapped: Set<string>,
): string | null {
  const exact = normLabel(label);
  if (referenceMeta.has(exact) && !alreadyMapped.has(exact)) return exact;

  const canon = canonicalizeLabel(label);
  if (canon) {
    for (const key of referenceKeys) {
      if (alreadyMapped.has(key)) continue;
      if (canonicalizeLabel(referenceMeta.get(key)!.label) === canon) return key;
    }
  }

  const tokens = labelTokens(label);
  let bestKey: string | null = null;
  let bestScore = 0;
  for (const key of referenceKeys) {
    if (alreadyMapped.has(key)) continue;
    const score = tokenJaccard(tokens, labelTokens(referenceMeta.get(key)!.label));
    if (score > bestScore) {
      bestScore = score;
      bestKey = key;
    }
  }
  // High enough to merge "Income Tax Expense" ↔ "Income Tax Reversal/(Expenses)"
  // but avoid merging unrelated lines.
  if (bestKey != null && bestScore >= 0.72) return bestKey;
  return null;
}

function entityHeaderPositions(
  headerRows: string[][],
): { groupCi: number | null; bankCi: number | null } {
  let groupCi: number | null = null;
  let bankCi: number | null = null;
  for (const hrow of headerRows) {
    for (let ci = 0; ci < hrow.length; ci += 1) {
      const cu = String(hrow[ci] ?? "").trim().toUpperCase();
      if (cu === "GROUP") groupCi = ci;
      else if (cu === "BANK" || cu === "COMPANY") bankCi = ci;
    }
  }
  return { groupCi, bankCi };
}

function yearColForEntity(
  headerRows: string[][],
  year: number,
  entityColumn: "group" | "company" = "group",
): number | null {
  const yearS = String(year);
  const { groupCi, bankCi } = entityHeaderPositions(headerRows);

  if (groupCi != null || bankCi != null) {
    for (const hrow of headerRows) {
      for (let ci = 0; ci < hrow.length; ci += 1) {
        if (String(hrow[ci] ?? "").trim() !== yearS) continue;
        if (entityColumn === "group") {
          if (bankCi == null || ci < bankCi) return ci;
        } else if (bankCi != null && ci >= bankCi - 1) {
          return ci;
        }
      }
    }
  }

  const yearByCol = new Map<number, string>();
  for (const hrow of headerRows) {
    for (let ci = 1; ci < hrow.length; ci += 1) {
      const cell = String(hrow[ci] ?? "").trim();
      const m = /\b(20\d{2})\b/.exec(cell);
      if (m) yearByCol.set(ci, m[1]);
    }
  }

  const entityCols: number[] = [];
  for (const hrow of headerRows) {
    for (let ci = 0; ci < hrow.length; ci += 1) {
      const cu = String(hrow[ci] ?? "").trim().toUpperCase();
      if (entityColumn === "group" && cu === "GROUP") entityCols.push(ci);
      if (entityColumn !== "group" && (cu === "BANK" || cu === "COMPANY")) {
        entityCols.push(ci);
      }
    }
  }

  for (const ci of entityCols.sort((a, b) => a - b)) {
    if (yearByCol.get(ci) === yearS) return ci;
    for (const offset of [0, 1, -1]) {
      const target = ci + offset;
      if (yearByCol.get(target) === yearS) return target;
    }
  }

  const flatYears: Array<{ ci: number; y: string }> = [];
  for (const hrow of headerRows) {
    for (let ci = 1; ci < hrow.length; ci += 1) {
      const cell = String(hrow[ci] ?? "").trim();
      if (/^20\d{2}$/.test(cell)) flatYears.push({ ci, y: cell });
    }
  }
  if (flatYears.length) {
    const matches = flatYears.filter((f) => f.y === yearS).map((f) => f.ci);
    if (matches.length) {
      return entityColumn === "group"
        ? matches[0]
        : matches[1] ?? matches[0];
    }
  }

  return null;
}

function findValueCol(doc: TableDoc, year: number): number | null {
  const headers = doc.header_rows ?? [];
  const rows = doc.rows ?? [];
  if (!rows.length) return null;

  const bodyRows = rows.map((r) => (r.cells ?? []).map((c) => String(c ?? "")));

  const entityCol = yearColForEntity(headers, year, "group");
  if (entityCol != null) return entityCol;

  const yearS = String(year);
  const maxCol = Math.max(
    ...rows.map((r) => (r.cells ?? []).length),
    ...headers.map((h) => h.length),
    1,
  );

  const exactCols = new Set<number>();
  for (let ci = 1; ci < maxCol; ci += 1) {
    for (const hrow of headers) {
      if (ci < hrow.length && String(hrow[ci] ?? "").trim() === yearS) {
        exactCols.add(ci);
      }
    }
  }
  const searchCols = exactCols.size
    ? [...exactCols].sort((a, b) => a - b)
    : Array.from({ length: maxCol - 1 }, (_, i) => i + 1);

  let bestCol: number | null = null;
  let bestScore = -1;
  for (const ci of searchCols) {
    const magnitudes: number[] = [];
    let bigCount = 0;
    for (const row of rows.slice(0, 40)) {
      const cells = row.cells ?? [];
      if (ci >= cells.length) continue;
      const v = parseNumber(cells[ci]);
      if (v == null) continue;
      const av = Math.abs(v);
      magnitudes.push(av);
      if (av >= 100_000) bigCount += 1;
    }
    if (!magnitudes.length) continue;

    const sorted = [...magnitudes].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)] ?? 0;
    let score = bigCount * 10;
    if (median >= 100_000) score += 20;
    else if (median >= 1_000) score += 8;
    else if (median < 500) score += 1;

    if (score > bestScore) {
      bestScore = score;
      bestCol = ci;
    }
  }

  return bestCol;
}

function pickBestTableDoc(docs: TableDoc[], year: number): TableDoc | null {
  let best: { doc: TableDoc; score: number } | null = null;
  for (const doc of docs) {
    const col = findValueCol(doc, year);
    if (col == null) continue;
    const rows = doc.rows ?? [];
    let big = 0;
    for (const row of rows.slice(0, 40)) {
      const cells = row.cells ?? [];
      if (col < cells.length) {
        const v = parseNumber(cells[col]);
        if (v != null && Math.abs(v) >= 100_000) big += 1;
      }
    }
    const score = big + rows.length * 0.01;
    if (!best || score > best.score) best = { doc, score };
  }
  return best?.doc ?? null;
}

function extractYearValues(
  doc: TableDoc,
  year: number,
): Map<string, { label: string; value: string; style: string }> {
  const col = findValueCol(doc, year);
  const out = new Map<string, { label: string; value: string; style: string }>();
  if (col == null) return out;

  for (const row of doc.rows ?? []) {
    const style = row.style ?? "data";
    if (style === "blank") continue;
    const cells = row.cells ?? [];
    const label = String(cells[0] ?? "").trim();
    if (!label) continue;
    const value = col < cells.length ? String(cells[col] ?? "").trim() : "";
    if (!value && style === "data") continue;
    out.set(normLabel(label), { label, value: value || "—", style });
  }
  return out;
}

const STMT_PRIORITY: Record<string, number> = {
  income_statement: 10,
  oci: 9,
  sofp: 8,
  equity: 7,
  cash_flows: 6,
  ten_year_summary: 5,
  five_year_summary: 4,
};

export async function getYearlyStatementSummary(
  companySlug: string,
  statementKey: string,
  period: PeriodLabel,
  years: number[] = DEFAULT_SUMMARY_YEARS,
): Promise<YearlySummaryPayload> {
  const reportType = reportTypeFromPeriod(period);
  const companyDoc = await Company.findOne({ slug: companySlug })
    .select({ name: 1 })
    .lean();
  const displayName = companyDoc?.name ?? companySlug.replace(/_+/g, " ");

  const docs = await FinancialTable.find({
    company_slug: companySlug,
    report_type: reportType,
    year: { $in: years },
    statement_key: statementKey,
  })
    .select({
      year: 1,
      statement_key: 1,
      statement_title: 1,
      header_rows: 1,
      rows: 1,
      table_index: 1,
    })
    .sort({ year: -1, table_index: 1 })
    .lean();

  const byYear = new Map<number, TableDoc[]>();
  for (const doc of docs) {
    const y = doc.year;
    if (typeof y !== "number") continue;
    const list = byYear.get(y) ?? [];
    list.push(doc as TableDoc);
    byYear.set(y, list);
  }

  const valuesByYear = new Map<number, Map<string, YearEntry>>();
  const yearsWithData: number[] = [];

  for (const year of years) {
    const yearDocs = byYear.get(year) ?? [];
    const best = pickBestTableDoc(yearDocs, year);
    if (!best) continue;
    const extracted = extractYearValues(best, year);
    if (extracted.size === 0) continue;
    valuesByYear.set(year, extracted);
    yearsWithData.push(year);
  }

  // Newest year with data defines the main row list (same shape as Detail).
  const yearsDesc = [...years].sort((a, b) => b - a);
  const referenceYear =
    yearsDesc.find((y) => valuesByYear.has(y)) ?? null;

  const refMap =
    referenceYear != null ? valuesByYear.get(referenceYear)! : new Map();
  const referenceKeys = [...refMap.keys()];
  const mainRows: Array<{ key: string; label: string; style: string }> = [];
  for (const [key, entry] of refMap) {
    mainRows.push({ key, label: entry.label, style: entry.style });
  }

  // Remap every year's values onto reference keys when labels are near-matches.
  const alignedByYear = new Map<number, Map<string, YearEntry>>();
  const priorOnlyOrder: Array<{ key: string; label: string; style: string }> =
    [];
  const priorOnlySeen = new Set<string>();

  for (const year of yearsDesc) {
    const yearMap = valuesByYear.get(year);
    if (!yearMap) continue;

    const aligned = new Map<string, YearEntry>();
    const claimedRefKeys = new Set<string>();

    for (const [rawKey, entry] of yearMap) {
      if (year === referenceYear) {
        aligned.set(rawKey, entry);
        continue;
      }

      const matched = matchToReferenceKey(
        entry.label,
        referenceKeys,
        refMap,
        claimedRefKeys,
      );

      if (matched != null) {
        claimedRefKeys.add(matched);
        // Prefer first (newest-year wording already on the row); keep value.
        if (!aligned.has(matched)) aligned.set(matched, { ...entry, label: refMap.get(matched)!.label });
        continue;
      }

      // Truly absent from newest year — fold into an existing prior-only
      // row when wording is a near-match, else append at the bottom.
      let priorKey: string | null = null;
      for (const existing of priorOnlyOrder) {
        const existingLabel = existing.label;
        const existingCanon = canonicalizeLabel(existingLabel);
        const entryCanon = canonicalizeLabel(entry.label);
        if (
          existingCanon &&
          entryCanon &&
          (existingCanon === entryCanon ||
            tokenJaccard(labelTokens(existingLabel), labelTokens(entry.label)) >=
              0.72)
        ) {
          priorKey = existing.key;
          break;
        }
      }
      if (priorKey == null) {
        priorKey = `prior:${rawKey}`;
        if (!priorOnlySeen.has(priorKey)) {
          priorOnlySeen.add(priorKey);
          priorOnlyOrder.push({
            key: priorKey,
            label: entry.label,
            style: entry.style,
          });
        }
      }
      aligned.set(priorKey, entry);
    }

    alignedByYear.set(year, aligned);
  }

  if (referenceYear != null) {
    alignedByYear.set(referenceYear, refMap);
  }

  const buildValues = (key: string): Record<string, string | null> => {
    const values: Record<string, string | null> = {};
    for (const year of years) {
      values[String(year)] = alignedByYear.get(year)?.get(key)?.value ?? null;
    }
    return values;
  };

  const summaryRows: YearlySummaryRow[] = [
    ...mainRows.map(({ key, label, style }) => ({
      label,
      style,
      values: buildValues(key),
      priorYearsOnly: false,
    })),
    ...priorOnlyOrder.map(({ key, label, style }) => ({
      label,
      style,
      values: buildValues(key),
      priorYearsOnly: true,
    })),
  ];

  const statementTitle =
    (docs[0] as { statement_title?: string | null } | undefined)
      ?.statement_title ?? null;

  return {
    company: companySlug,
    displayName,
    statementKey,
    statementTitle,
    period,
    years,
    rows: summaryRows,
    yearsWithData,
    referenceYear,
  };
}

export function listSummaryStatementKeys(
  availableStatements: string[],
): string[] {
  return [...availableStatements].sort((a, b) => {
    const pa = STMT_PRIORITY[a] ?? 0;
    const pb = STMT_PRIORITY[b] ?? 0;
    if (pa !== pb) return pb - pa;
    return a.localeCompare(b);
  });
}
