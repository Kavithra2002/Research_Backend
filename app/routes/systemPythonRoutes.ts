import { Router, type Request, type Response } from "express";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { asyncHandler } from "../utils/asyncHandler";
import { resolveLocalPdfPath } from "../utils/r2Client";
import {
  broadcast,
  createPythonState,
  finalize,
  killPythonChild,
  spawnPythonProcess,
  streamNdjson,
  writeFilterFile,
} from "../utils/pythonRunner";

const router = Router();

const extractState = createPythonState();
const updateState = createPythonState();
const nonFinancialState = createPythonState();

function toStringArray(v: unknown): string[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: string[] = [];
  for (const x of v) {
    if (typeof x === "string") {
      const t = x.trim();
      if (t) out.push(t);
    }
  }
  return out.length > 0 ? out : undefined;
}

// --- /api/system/extract ---
function startExtractScan(body: Record<string, unknown>) {
  if (extractState.active) return;

  const args: string[] = [];
  const date = typeof body.date === "string" ? body.date : undefined;
  if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
    args.push("--date", date);
  }
  const limit = typeof body.limit === "number" ? body.limit : undefined;
  if (typeof limit === "number" && Number.isFinite(limit) && limit > 0) {
    args.push("--limit", String(Math.floor(limit)));
  }

  const symbols = toStringArray(body.symbols) ?? [];
  const companies = toStringArray(body.companies) ?? [];
  let filterFile: string | null = null;

  if (symbols.length > 0 || companies.length > 0) {
    filterFile = writeFilterFile({
      symbols,
      companies,
      groupId: typeof body.groupId === "string" ? body.groupId : null,
      groupName: typeof body.groupName === "string" ? body.groupName : null,
    });
    if (filterFile) {
      args.push("--filter-file", filterFile);
    } else if (symbols.length > 0) {
      args.push("--symbols", symbols.join(","));
    } else if (companies.length > 0) {
      args.push("--companies", companies.join("||"));
    }
  }

  const groupName = typeof body.groupName === "string" ? body.groupName : undefined;
  if (groupName) {
    broadcast(
      extractState,
      JSON.stringify({
        type: "log",
        level: "info",
        message: `Scope: group "${groupName}"`,
      }),
    );
  }

  spawnPythonProcess(extractState, "Extract_newly_updated.py", args, () => {
    if (filterFile) {
      fs.unlink(filterFile).catch(() => undefined);
    }
  });
}

router.post(
  "/system/extract",
  asyncHandler(async (req: Request, res: Response) => {
    startExtractScan((req.body ?? {}) as Record<string, unknown>);
    streamNdjson(extractState, res);
  }),
);

router.get(
  "/system/extract",
  asyncHandler((_req: Request, res: Response) => {
    streamNdjson(extractState, res);
  }),
);

router.delete(
  "/system/extract",
  asyncHandler((_req: Request, res: Response) => {
    killPythonChild(extractState);
    res.json({ ok: true });
  }),
);

// --- /api/system/update ---
interface SelectionItem {
  company: string;
  report_type: string;
  file_name: string;
}

function sanitizeItems(input: unknown): SelectionItem[] {
  if (!Array.isArray(input)) return [];
  const out: SelectionItem[] = [];
  for (const raw of input) {
    if (!raw || typeof raw !== "object") continue;
    const o = raw as Record<string, unknown>;
    const company = typeof o.company === "string" ? o.company.trim() : "";
    const report_type =
      typeof o.report_type === "string" ? o.report_type.trim() : "";
    const file_name =
      typeof o.file_name === "string" ? o.file_name.trim() : "";
    if (!company || !report_type || !file_name) continue;
    out.push({ company, report_type, file_name });
  }
  return out;
}

async function startUpdateRun(
  items: SelectionItem[],
  opts: { dryRun?: boolean; option?: "1" | "2"; model?: string },
) {
  if (updateState.active) return;

  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "ambeon-update-"));
  const itemsFile = path.join(dir, "items.json");
  await fs.writeFile(itemsFile, JSON.stringify({ items }, null, 2), "utf-8");

  const args = ["--items", itemsFile];
  if (opts.option) args.push("--option", opts.option);
  if (opts.model) args.push("--model", opts.model);
  if (opts.dryRun) args.push("--dry-run");

  spawnPythonProcess(updateState, "Extract_selected_reports.py", args, () => {
    fs.rm(dir, { recursive: true, force: true }).catch(() => undefined);
  });
}

router.post(
  "/system/update",
  asyncHandler(async (req: Request, res: Response) => {
    const body = (req.body ?? {}) as Record<string, unknown>;
    const items = sanitizeItems(body.items);
    await startUpdateRun(items, {
      dryRun: body.dryRun === true,
      option:
        body.option === "1" || body.option === "2" ? body.option : undefined,
      model: typeof body.model === "string" ? body.model : undefined,
    });
    streamNdjson(updateState, res);
  }),
);

router.get(
  "/system/update",
  asyncHandler((_req: Request, res: Response) => {
    streamNdjson(updateState, res);
  }),
);

router.delete(
  "/system/update",
  asyncHandler((_req: Request, res: Response) => {
    killPythonChild(updateState);
    res.json({ ok: true });
  }),
);

// --- /api/ai/non-financial ---
async function startNonFinancialRun(body: Record<string, unknown>) {
  if (nonFinancialState.active) return;

  const company = typeof body.company === "string" ? body.company.trim() : "";
  if (!company) {
    broadcast(
      nonFinancialState,
      JSON.stringify({ type: "error", message: "company is required" }),
    );
    finalize(nonFinancialState, 1);
    return;
  }

  const args = ["--company", company];
  if (typeof body.model === "string" && body.model.trim()) {
    args.push("--model", body.model.trim());
  }
  if (body.dryRun === true) {
    args.push("--dry-run");
  }

  let pdf = typeof body.pdf === "string" ? body.pdf.trim() : "";
  if (pdf) {
    try {
      pdf = await resolveLocalPdfPath(pdf);
      args.push("--pdf", pdf);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      broadcast(
        nonFinancialState,
        JSON.stringify({ type: "error", message: `PDF resolve failed: ${msg}` }),
      );
      finalize(nonFinancialState, 1);
      return;
    }
  }

  spawnPythonProcess(nonFinancialState, "non_financial_script.py", args);
}

router.post(
  "/ai/non-financial",
  asyncHandler(async (req: Request, res: Response) => {
    await startNonFinancialRun((req.body ?? {}) as Record<string, unknown>);
    streamNdjson(nonFinancialState, res);
  }),
);

router.get(
  "/ai/non-financial",
  asyncHandler((_req: Request, res: Response) => {
    streamNdjson(nonFinancialState, res);
  }),
);

router.delete(
  "/ai/non-financial",
  asyncHandler((_req: Request, res: Response) => {
    killPythonChild(nonFinancialState);
    res.json({ ok: true });
  }),
);

export default router;
