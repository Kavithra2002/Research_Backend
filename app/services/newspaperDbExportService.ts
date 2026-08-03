import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { Company } from "../models/Company";
import { FinancialTable } from "../models/FinancialTable";
import { lookupCseEquity } from "./cseMarketService";
import { pythonCommand } from "../utils/pythonRunner";

export const COMMERCIAL_BANK_SLUG = "Commercial_Bank_of_Ceylon_PLC";

const execFileAsync = promisify(execFile);

function scriptDir(): string {
  const fromEnv = process.env.SCRIPT_DIR?.trim();
  if (fromEnv) return path.resolve(fromEnv);
  return path.resolve(process.cwd(), "scripts");
}

function safeFileToken(value: string): string {
  const cleaned = value.replace(/[^a-z0-9-_]+/gi, "_").replace(/_+/g, "_");
  return cleaned.replace(/^_|_$/g, "") || "company";
}

async function resolveCompanyMeta(companySlug: string): Promise<{
  slug: string;
  displayName: string;
  ticker: string | null;
}> {
  const slug = companySlug.trim();
  if (!slug) {
    throw new Error("Company is required");
  }

  const registry = await Company.findOne({ slug }).select({ name: 1 }).lean();
  let displayName = registry?.name?.trim() ?? "";

  if (!displayName) {
    const sample = await FinancialTable.findOne({ company_slug: slug })
      .select({ company_name: 1 })
      .lean();
    displayName =
      sample?.company_name?.trim() ??
      slug.replace(/_+/g, " ").replace(/\s+/g, " ").trim();
  }

  let ticker: string | null = null;
  try {
    const lookup = await lookupCseEquity(displayName);
    if (lookup.matched?.symbol) ticker = lookup.matched.symbol;
  } catch {
    ticker = null;
  }

  return { slug, displayName, ticker };
}

export type DbGridRow = {
  label: string;
  kind: "section" | "subsection" | "data" | "check" | string;
  values: Record<string, number | null>;
};

export type DbFsPreview = {
  view: "fs";
  company_slug: string;
  company_name?: string;
  ticker?: string | null;
  years: number[];
  unit: string;
  period_label: string;
  rows: DbGridRow[];
  cells_filled: number;
  cells_missing: number;
};

export type DbDriversPreview = {
  view: "drivers";
  company_slug: string;
  company_name?: string;
  years: number[];
  unit: string;
  period_label: string;
  rows: DbGridRow[];
  cells_filled: number;
  cells_missing: number;
};

export type DbRatiosPreview = {
  view: "ratios";
  company_slug: string;
  company_name?: string;
  years: number[];
  unit: string;
  period_label: string;
  rows: DbGridRow[];
  cells_filled: number;
  cells_missing: number;
  value_format?: "ratio";
};

export type DbQuarterlyPreview = {
  view: "quarterly";
  company_slug: string;
  company_name: string;
  unit: string;
  period_label: string;
  columns: { key: string; label: string }[];
  rows: DbGridRow[];
  cells_filled?: number;
  cells_missing?: number;
  years?: number[];
};

export type DbNotesPreview = {
  view: "notes";
  company_slug: string;
  company_name?: string;
  years: number[];
  unit: string;
  period_label: string;
  rows: DbGridRow[];
  notes_extracted_years?: number[];
  note_line_items?: number;
  note_line_items_with_data?: number;
};

export type DbPreview =
  | DbFsPreview
  | DbDriversPreview
  | DbRatiosPreview
  | DbQuarterlyPreview
  | DbNotesPreview;

async function runPreviewScript(
  view: "fs" | "drivers" | "ratios" | "quarterly" | "notes",
  companySlug?: string,
): Promise<Record<string, unknown>> {
  const scriptPath = path.join(scriptDir(), "preview_db_workbook.py");
  const args = ["-u", scriptPath, "--view", view];
  if (companySlug) {
    args.push("--company-slug", companySlug);
  }

  const result = await execFileAsync(pythonCommand(), args, {
    cwd: scriptDir(),
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    maxBuffer: 20 * 1024 * 1024,
  });

  const stdout = String(result.stdout ?? "").trim();
  const line = stdout.split("\n").pop() ?? "{}";
  const payload = JSON.parse(line) as Record<string, unknown>;
  if (payload.ok === false || payload.error) {
    throw new Error(String(payload.error ?? "Preview failed"));
  }
  return payload;
}

export async function getDbFsPreview(companySlug: string): Promise<DbFsPreview> {
  const meta = await resolveCompanyMeta(companySlug);
  const tableCount = await FinancialTable.countDocuments({
    company_slug: meta.slug,
  });
  if (tableCount === 0) {
    throw new Error(
      `No financial tables found for "${meta.displayName}". Run extraction first.`,
    );
  }

  const payload = await runPreviewScript("fs", meta.slug);
  return {
    ...(payload as DbFsPreview),
    company_name: meta.displayName,
    ticker: meta.ticker,
  };
}

export async function getDbQuarterlyPreview(
  companySlug?: string,
): Promise<DbQuarterlyPreview> {
  const payload = await runPreviewScript(
    "quarterly",
    companySlug ?? COMMERCIAL_BANK_SLUG,
  );
  return payload as DbQuarterlyPreview;
}

export async function getDbDriversPreview(
  companySlug: string,
): Promise<DbDriversPreview> {
  const meta = await resolveCompanyMeta(companySlug);
  const payload = await runPreviewScript("drivers", meta.slug);
  return {
    ...(payload as DbDriversPreview),
    company_name: meta.displayName,
  };
}

export async function getDbRatiosPreview(
  companySlug: string,
): Promise<DbRatiosPreview> {
  const meta = await resolveCompanyMeta(companySlug);
  const payload = await runPreviewScript("ratios", meta.slug);
  return {
    ...(payload as DbRatiosPreview),
    company_name: meta.displayName,
  };
}

export async function getDbNotesPreview(
  companySlug: string,
): Promise<DbNotesPreview> {
  const meta = await resolveCompanyMeta(companySlug);
  const payload = await runPreviewScript("notes", meta.slug);
  return {
    ...(payload as DbNotesPreview),
    company_name: meta.displayName,
  };
}

export type CombWorkbookExportResult = FsWorkbookExportResult;

export async function buildCombWorkbookExport(
  companySlug: string,
): Promise<CombWorkbookExportResult> {
  const meta = await resolveCompanyMeta(companySlug);
  const tableCount = await FinancialTable.countDocuments({
    company_slug: meta.slug,
  });
  if (tableCount === 0) {
    throw new Error(
      `No financial tables found for "${meta.displayName}". Run extraction first.`,
    );
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ambeon-comb-export-"));
  const outputPath = path.join(
    tmpDir,
    `${safeFileToken(meta.slug)}_COMB_2022.xlsx`,
  );

  const scriptPath = path.join(scriptDir(), "export_comb_workbook.py");
  const args = [
    "-u",
    scriptPath,
    "--company-slug",
    meta.slug,
    "--output",
    outputPath,
    "--company-name",
    meta.displayName,
  ];
  if (meta.ticker) {
    args.push("--ticker", meta.ticker);
  }

  let stdout = "";
  try {
    const result = await execFileAsync(pythonCommand(), args, {
      cwd: scriptDir(),
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      maxBuffer: 10 * 1024 * 1024,
    });
    stdout = String(result.stdout ?? "").trim();
  } catch (err) {
    const message =
      err instanceof Error
        ? err.message
        : "Failed to run export_comb_workbook.py";
    throw new Error(message);
  }

  let payload: {
    ok?: boolean;
    error?: string;
    cells_filled?: number;
    cells_missing?: number;
    company_name?: string;
  } = {};
  if (stdout) {
    try {
      payload = JSON.parse(stdout.split("\n").pop() ?? "{}") as typeof payload;
    } catch {
      /* ignore parse errors */
    }
  }

  if (!fs.existsSync(outputPath)) {
    throw new Error(payload.error ?? "Excel export failed");
  }

  const buffer = fs.readFileSync(outputPath);
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* ignore cleanup errors */
  }

  const filename = `${safeFileToken(meta.displayName)}_COMB_workbook_2022.xlsx`;
  return {
    buffer,
    filename,
    companySlug: meta.slug,
    companyName: payload.company_name ?? meta.displayName,
    cellsFilled: payload.cells_filled ?? 0,
    cellsMissing: payload.cells_missing ?? 0,
  };
}

export type FsWorkbookExportResult = {
  buffer: Buffer;
  filename: string;
  companySlug: string;
  companyName: string;
  cellsFilled: number;
  cellsMissing: number;
};

export async function buildFsWorkbookExport(
  companySlug: string,
): Promise<FsWorkbookExportResult> {
  const meta = await resolveCompanyMeta(companySlug);
  const tableCount = await FinancialTable.countDocuments({
    company_slug: meta.slug,
  });
  if (tableCount === 0) {
    throw new Error(
      `No financial tables found for "${meta.displayName}". Run extraction first.`,
    );
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ambeon-fs-export-"));
  const outputPath = path.join(tmpDir, `${safeFileToken(meta.slug)}_FS.xlsx`);

  const scriptPath = path.join(scriptDir(), "export_fs_workbook.py");
  const args = [
    "-u",
    scriptPath,
    "--company-slug",
    meta.slug,
    "--output",
    outputPath,
    "--company-name",
    meta.displayName,
  ];
  if (meta.ticker) {
    args.push("--ticker", meta.ticker);
  }

  let stdout = "";
  try {
    const result = await execFileAsync(pythonCommand(), args, {
      cwd: scriptDir(),
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      maxBuffer: 10 * 1024 * 1024,
    });
    stdout = String(result.stdout ?? "").trim();
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Failed to run export_fs_workbook.py";
    throw new Error(message);
  }

  let payload: {
    ok?: boolean;
    error?: string;
    cells_filled?: number;
    cells_missing?: number;
    company_name?: string;
  } = {};
  if (stdout) {
    try {
      payload = JSON.parse(stdout.split("\n").pop() ?? "{}") as typeof payload;
    } catch {
      /* ignore parse errors */
    }
  }

  if (!fs.existsSync(outputPath)) {
    throw new Error(payload.error ?? "Excel export failed");
  }

  const buffer = fs.readFileSync(outputPath);
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* ignore cleanup errors */
  }

  const filename = `${safeFileToken(meta.displayName)}_financial_summary_2017_2025.xlsx`;
  return {
    buffer,
    filename,
    companySlug: meta.slug,
    companyName: payload.company_name ?? meta.displayName,
    cellsFilled: payload.cells_filled ?? 0,
    cellsMissing: payload.cells_missing ?? 0,
  };
}

export function isQuarterlyDbAvailable(companySlug: string): boolean {
  return companySlug.trim() === COMMERCIAL_BANK_SLUG;
}

export function isCombPilotAvailable(companySlug: string): boolean {
  return companySlug.trim() === COMMERCIAL_BANK_SLUG;
}
