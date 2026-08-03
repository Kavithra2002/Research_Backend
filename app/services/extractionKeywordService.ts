import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import { Types } from "mongoose";

import {
  ExtractionKeyword,
  type ExtractionKeywordScope,
} from "../models/ExtractionKeyword";
import { pythonCommand } from "../utils/pythonRunner";

const execFileAsync = promisify(execFile);

export type OwnerMeta = {
  userObjectId: string | null;
  userBusinessId: string | null;
};

type BuiltinRow = {
  label: string;
  aliases: string[];
  alias_count: number;
};

function scriptDir(): string {
  const fromEnv = process.env.SCRIPT_DIR?.trim();
  if (fromEnv) return path.resolve(fromEnv);
  return path.resolve(process.cwd(), "scripts");
}

function userKeywordsPath(): string {
  return path.resolve(
    scriptDir(),
    "..",
    "New_Updates",
    "extraction_keywords_user.json",
  );
}

function manifestPath(): string {
  return path.resolve(scriptDir(), "..", "New_Updates", "comb_manifest.json");
}

type ManifestRow = { label: string; kind?: string };

function readManifest(): Record<string, unknown> {
  const filePath = manifestPath();
  if (!fs.existsSync(filePath)) return {};
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf-8")) as Record<
      string,
      unknown
    >;
  } catch {
    return {};
  }
}

function isTopicLabel(label: string): boolean {
  const n = label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return (
    n === "less expenses" ||
    n === "profit attributable to" ||
    n === "earnings per share" ||
    n === "income statement" ||
    n === "oci" ||
    n === "balance sheet" ||
    n === "cash flow statement" ||
    n === "assets" ||
    n === "liabilities" ||
    n === "equity" ||
    n === "memorandum information" ||
    n === "adjustments for"
  );
}

function manifestRowsForScope(scope: ExtractionKeywordScope): ManifestRow[] {
  const manifest = readManifest();
  if (scope === "fs") {
    const rows = (manifest.fs as { rows?: ManifestRow[] } | undefined)?.rows;
    return rows ?? [];
  }
  if (scope === "drivers") {
    const rows = (manifest.drivers as { rows?: ManifestRow[] } | undefined)
      ?.rows;
    return rows ?? [];
  }
  const labels =
    (manifest.quarterly as { labels?: string[] } | undefined)?.labels ?? [];
  return labels.map((label) => ({
    label,
    kind: isTopicLabel(label) ? "subsection" : "data",
  }));
}

function countManifestDataRows(scope: ExtractionKeywordScope): number {
  return manifestRowsForScope(scope).filter((row) => {
    const kind = row.kind ?? "data";
    return kind === "data";
  }).length;
}

function defaultLineOrder(scope: ExtractionKeywordScope): number {
  return countManifestDataRows(scope);
}

export type ExtractionKeywordDto = {
  _id: string;
  canonical_label: string;
  aliases: string[];
  user_aliases: string[];
  scope: ExtractionKeywordScope;
  is_new_keyword: boolean;
  is_builtin: boolean;
  line_order: number | null;
  created_by: string | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

export type SheetLineItem = {
  label: string;
  kind: "section" | "subsection" | "data" | "check" | string;
  source: "builtin" | "user";
  keyword_id: string | null;
  is_new_keyword: boolean;
  line_order: number | null;
  draggable: boolean;
};

export type CanonicalLabelOption = {
  label: string;
  scope: ExtractionKeywordScope;
  source: "builtin" | "user";
  alias_count: number;
  matched_alias?: string | null;
};

function resolveOwner(meta: OwnerMeta): {
  objectId: Types.ObjectId | null;
  businessId: string | null;
} {
  const businessId = meta.userBusinessId?.trim() || null;
  const objectId =
    meta.userObjectId && Types.ObjectId.isValid(meta.userObjectId)
      ? new Types.ObjectId(meta.userObjectId)
      : null;
  return { objectId, businessId };
}

function normalizeLabel(value: string): string {
  return value.trim().replace(/\s+/g, " ");
}

function normalizeAlias(value: string): string {
  return value.trim().replace(/\s+/g, " ");
}

function uniqueAliases(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of values) {
    const v = normalizeAlias(raw);
    if (!v) continue;
    const key = v.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(v);
  }
  return out;
}

function toDto(doc: {
  _id: unknown;
  canonical_label: string;
  aliases?: string[];
  user_aliases?: string[];
  scope: string;
  is_new_keyword?: boolean;
  is_builtin?: boolean;
  line_order?: number | null;
  created_by?: unknown;
  created_by_user_id?: string | null;
  created_at?: Date | string;
  updated_at?: Date | string;
}): ExtractionKeywordDto {
  return {
    _id: String(doc._id),
    canonical_label: doc.canonical_label,
    aliases: doc.aliases ?? [],
    user_aliases: doc.user_aliases ?? [],
    scope: doc.scope as ExtractionKeywordScope,
    is_new_keyword: Boolean(doc.is_new_keyword),
    is_builtin: Boolean(doc.is_builtin),
    line_order:
      doc.line_order === null || doc.line_order === undefined
        ? null
        : Number(doc.line_order),
    created_by: doc.created_by ? String(doc.created_by) : null,
    created_by_user_id: doc.created_by_user_id ?? null,
    created_at:
      doc.created_at instanceof Date
        ? doc.created_at.toISOString()
        : String(doc.created_at ?? ""),
    updated_at:
      doc.updated_at instanceof Date
        ? doc.updated_at.toISOString()
        : String(doc.updated_at ?? ""),
  };
}

async function fetchBuiltinCatalog(): Promise<
  Record<ExtractionKeywordScope, BuiltinRow[]>
> {
  const scriptPath = path.join(
    scriptDir(),
    "list_extraction_canonical_labels.py",
  );
  const result = await execFileAsync(pythonCommand(), ["-u", scriptPath], {
    cwd: scriptDir(),
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    maxBuffer: 4 * 1024 * 1024,
  });
  const stdout = String(result.stdout ?? "").trim();
  const line = stdout.split("\n").pop() ?? "{}";
  const payload = JSON.parse(line) as Record<string, BuiltinRow[]>;

  return {
    fs: payload.fs ?? [],
    drivers: payload.drivers ?? [],
    quarterly: payload.quarterly ?? [],
  };
}

let seedPromise: Promise<{ seeded: number; total: number }> | null = null;

/** Seed script keywords into MongoDB so autosuggest reads from the DB catalog. */
export async function ensureBuiltinKeywordsSeeded(): Promise<{
  seeded: number;
  total: number;
}> {
  if (seedPromise) return seedPromise;

  seedPromise = (async () => {
    const catalog = await fetchBuiltinCatalog();
    let seeded = 0;
    let total = 0;

    for (const scope of ["fs", "drivers", "quarterly"] as const) {
      for (const row of catalog[scope]) {
        total += 1;
        const label = normalizeLabel(row.label);
        const builtinAliases = uniqueAliases(row.aliases ?? []);
        const existing = await ExtractionKeyword.findOne({
          canonical_label: label,
          scope,
        });

        if (!existing) {
          await ExtractionKeyword.create({
            canonical_label: label,
            aliases: builtinAliases,
            user_aliases: [],
            scope,
            is_new_keyword: false,
            is_builtin: true,
            created_by: null,
            created_by_user_id: null,
          });
          seeded += 1;
          continue;
        }

        if (!existing.is_builtin) continue;

        const userAliases = existing.user_aliases ?? [];
        existing.aliases = uniqueAliases([...builtinAliases, ...userAliases]);
        existing.is_builtin = true;
        await existing.save();
      }
    }

    return { seeded, total };
  })();

  try {
    return await seedPromise;
  } catch (err) {
    seedPromise = null;
    throw err;
  }
}

export async function listCanonicalLabelOptions(
  scope?: ExtractionKeywordScope,
  query?: string,
): Promise<CanonicalLabelOption[]> {
  await ensureBuiltinKeywordsSeeded();

  const filter: Record<string, unknown> = {};
  if (scope) filter.scope = scope;

  const q = query?.trim();
  let regex: RegExp | null = null;
  if (q) {
    regex = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i");
    filter.$or = [
      { canonical_label: { $regex: regex } },
      { aliases: { $elemMatch: { $regex: regex } } },
      { user_aliases: { $elemMatch: { $regex: regex } } },
    ];
  }

  const docs = await ExtractionKeyword.find(filter)
    .select({
      canonical_label: 1,
      scope: 1,
      aliases: 1,
      user_aliases: 1,
      is_builtin: 1,
      is_new_keyword: 1,
    })
    .sort({ canonical_label: 1 })
    .limit(50)
    .lean();

  return docs.map((doc) => {
    const aliases = doc.aliases ?? [];
    const labelMatches = regex ? regex.test(doc.canonical_label) : true;
    const matchedAlias =
      regex && !labelMatches
        ? aliases.find((a) => regex!.test(a)) ?? null
        : null;
    return {
      label: doc.canonical_label,
      scope: doc.scope as ExtractionKeywordScope,
      source: doc.is_builtin && !doc.is_new_keyword ? "builtin" : "user",
      alias_count: aliases.length,
      matched_alias: matchedAlias,
    };
  });
}

export async function listUserKeywords(
  scope?: ExtractionKeywordScope,
): Promise<ExtractionKeywordDto[]> {
  await ensureBuiltinKeywordsSeeded();

  const filter: Record<string, unknown> = {
    $or: [
      { is_new_keyword: true },
      {
        $expr: {
          $gt: [{ $size: { $ifNull: ["$user_aliases", []] } }, 0],
        },
      },
    ],
  };
  if (scope) filter.scope = scope;

  const docs = await ExtractionKeyword.find(filter)
    .sort({ updated_at: -1 })
    .lean();

  return docs.map((doc) => toDto(doc));
}

export async function listSheetLines(
  scope: ExtractionKeywordScope,
): Promise<SheetLineItem[]> {
  await ensureBuiltinKeywordsSeeded();

  const newKeywords = await ExtractionKeyword.find({
    scope,
    is_new_keyword: true,
  })
    .sort({ line_order: 1, canonical_label: 1 })
    .lean();

  const inserts = new Map<number, typeof newKeywords>();
  const trailing: typeof newKeywords = [];
  for (const doc of newKeywords) {
    const order =
      doc.line_order === null || doc.line_order === undefined
        ? defaultLineOrder(scope)
        : Math.max(0, Number(doc.line_order));
    const maxOrder = defaultLineOrder(scope);
    if (order >= maxOrder) {
      trailing.push(doc);
      continue;
    }
    const list = inserts.get(order) ?? [];
    list.push(doc);
    inserts.set(order, list);
  }

  const lines: SheetLineItem[] = [];
  const emittedIds = new Set<string>();
  let dataIndex = 0;

  const pushUserLine = (doc: (typeof newKeywords)[number]) => {
    const id = String(doc._id);
    if (emittedIds.has(id)) return;
    emittedIds.add(id);
    lines.push({
      label: doc.canonical_label,
      kind: "data",
      source: "user",
      keyword_id: id,
      is_new_keyword: true,
      line_order:
        doc.line_order === null || doc.line_order === undefined
          ? null
          : Number(doc.line_order),
      draggable: true,
    });
  };

  for (const row of manifestRowsForScope(scope)) {
    const kind = row.kind ?? "data";

    if (kind === "data") {
      const pending = inserts.get(dataIndex) ?? [];
      for (const doc of pending) {
        pushUserLine(doc);
      }
    }

    lines.push({
      label: row.label,
      kind,
      source: "builtin",
      keyword_id: null,
      is_new_keyword: false,
      line_order: null,
      draggable: false,
    });
    if (kind === "data") dataIndex += 1;
  }

  for (const doc of trailing) {
    pushUserLine(doc);
  }

  return lines;
}

function clampLineOrder(
  scope: ExtractionKeywordScope,
  lineOrder: number | null | undefined,
): number {
  const max = defaultLineOrder(scope);
  if (lineOrder === null || lineOrder === undefined || Number.isNaN(lineOrder)) {
    return max;
  }
  return Math.min(Math.max(0, Math.floor(lineOrder)), max);
}

export async function reorderNewKeyword(
  id: string,
  lineOrder: number,
): Promise<ExtractionKeywordDto> {
  const doc = await ExtractionKeyword.findById(id);
  if (!doc) throw new Error("Keyword not found");
  if (!doc.is_new_keyword) {
    throw new Error("Only new keywords can be repositioned in the sheet");
  }

  doc.line_order = clampLineOrder(
    doc.scope as ExtractionKeywordScope,
    lineOrder,
  );
  await doc.save();
  await syncKeywordsToScript();
  return toDto(doc.toObject());
}

async function syncKeywordsToScript(): Promise<void> {
  const docs = await ExtractionKeyword.find({
    $or: [
      { is_new_keyword: true },
      {
        $expr: {
          $gt: [{ $size: { $ifNull: ["$user_aliases", []] } }, 0],
        },
      },
    ],
  }).lean();

  const payload = {
    synced_at: new Date().toISOString(),
    entries: docs.map((doc) => ({
      canonical_label: doc.canonical_label,
      aliases: doc.user_aliases ?? [],
      scope: doc.scope,
      is_new_keyword: Boolean(doc.is_new_keyword),
      line_order:
        doc.line_order === null || doc.line_order === undefined
          ? null
          : Number(doc.line_order),
    })),
  };

  const outPath = userKeywordsPath();
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(payload, null, 2), "utf-8");

  const scriptPath = path.join(scriptDir(), "sync_extraction_keywords.py");
  try {
    await execFileAsync(pythonCommand(), ["-u", scriptPath], {
      cwd: scriptDir(),
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      maxBuffer: 2 * 1024 * 1024,
    });
  } catch {
    /* best-effort */
  }
}

async function aliasExistsInScope(
  scope: ExtractionKeywordScope,
  alias: string,
  excludeCanonical?: string,
): Promise<boolean> {
  const needle = normalizeAlias(alias).toLowerCase();
  if (!needle) return false;
  const docs = await ExtractionKeyword.find({ scope }).select({
    canonical_label: 1,
    aliases: 1,
    user_aliases: 1,
  });
  for (const doc of docs) {
    if (
      excludeCanonical &&
      doc.canonical_label.toLowerCase() === excludeCanonical.toLowerCase()
    ) {
      continue;
    }
    const all = [...(doc.aliases ?? []), ...(doc.user_aliases ?? [])];
    if (all.some((a) => normalizeAlias(a).toLowerCase() === needle)) {
      return true;
    }
    if (doc.canonical_label.toLowerCase() === needle) return true;
  }
  return false;
}

export async function addNewKeyword(
  input: {
    canonical_label: string;
    alias: string;
    scope: ExtractionKeywordScope;
    line_order?: number | null;
  },
  meta: OwnerMeta,
): Promise<ExtractionKeywordDto> {
  await ensureBuiltinKeywordsSeeded();

  const canonical_label = normalizeLabel(input.canonical_label);
  const alias = normalizeAlias(input.alias);
  if (!canonical_label) throw new Error("Description keyword is required");
  if (!alias) throw new Error("Wording in report is required");

  const existing = await ExtractionKeyword.findOne({
    canonical_label,
    scope: input.scope,
  });

  if (existing) {
    throw new Error(
      `Keyword "${canonical_label}" already exists in the ${input.scope} database keywords.`,
    );
  }

  if (await aliasExistsInScope(input.scope, alias)) {
    throw new Error(
      `Wording "${alias}" already exists in the ${input.scope} keyword catalog.`,
    );
  }

  const catalog = await fetchBuiltinCatalog();
  const builtinHit = catalog[input.scope]?.some(
    (row) => row.label.toLowerCase() === canonical_label.toLowerCase(),
  );
  if (builtinHit) {
    throw new Error(
      `Keyword "${canonical_label}" already exists in the script keyword catalog.`,
    );
  }

  const { objectId, businessId } = resolveOwner(meta);
  const created = await ExtractionKeyword.create({
    canonical_label,
    aliases: [alias],
    user_aliases: [alias],
    scope: input.scope,
    is_new_keyword: true,
    is_builtin: false,
    line_order: clampLineOrder(input.scope, input.line_order),
    created_by: objectId,
    created_by_user_id: businessId,
  });

  await syncKeywordsToScript();
  return toDto(created.toObject());
}

export async function addSimilarKeyword(
  input: {
    canonical_label: string;
    similar_word: string;
    scope: ExtractionKeywordScope;
  },
  meta: OwnerMeta,
): Promise<ExtractionKeywordDto> {
  await ensureBuiltinKeywordsSeeded();

  const canonical_label = normalizeLabel(input.canonical_label);
  const similar = normalizeAlias(input.similar_word);
  if (!canonical_label) throw new Error("Existing keyword is required");
  if (!similar) throw new Error("Similar word is required");

  let doc = await ExtractionKeyword.findOne({
    canonical_label,
    scope: input.scope,
  });

  if (!doc) {
    const catalog = await fetchBuiltinCatalog();
    const builtinRow = catalog[input.scope]?.find(
      (row) => row.label.toLowerCase() === canonical_label.toLowerCase(),
    );
    if (!builtinRow) {
      throw new Error(
        `Existing keyword "${canonical_label}" was not found in the ${input.scope} catalog.`,
      );
    }
    if (await aliasExistsInScope(input.scope, similar, canonical_label)) {
      throw new Error(
        `Similar word "${similar}" already exists in the ${input.scope} keyword catalog.`,
      );
    }
    const { objectId, businessId } = resolveOwner(meta);
    doc = await ExtractionKeyword.create({
      canonical_label,
      aliases: uniqueAliases([...(builtinRow.aliases ?? []), similar]),
      user_aliases: [similar],
      scope: input.scope,
      is_new_keyword: false,
      is_builtin: true,
      created_by: objectId,
      created_by_user_id: businessId,
    });
    await syncKeywordsToScript();
    return toDto(doc.toObject());
  }

  if (await aliasExistsInScope(input.scope, similar, canonical_label)) {
    throw new Error(
      `Similar word "${similar}" already exists in the ${input.scope} keyword catalog.`,
    );
  }

  const userAliases = new Set(doc.user_aliases ?? []);
  userAliases.add(similar);
  doc.user_aliases = Array.from(userAliases);
  doc.aliases = uniqueAliases([...(doc.aliases ?? []), similar]);
  await doc.save();
  await syncKeywordsToScript();
  return toDto(doc.toObject());
}

export async function deleteUserKeyword(id: string): Promise<{ removed: boolean }> {
  const doc = await ExtractionKeyword.findById(id);
  if (!doc) return { removed: false };

  if (doc.is_builtin && !doc.is_new_keyword) {
    const catalog = await fetchBuiltinCatalog();
    const row = catalog[doc.scope as ExtractionKeywordScope]?.find(
      (r) => r.label.toLowerCase() === doc.canonical_label.toLowerCase(),
    );
    doc.user_aliases = [];
    doc.aliases = uniqueAliases(row?.aliases ?? []);
    await doc.save();
    await syncKeywordsToScript();
    return { removed: true };
  }

  await ExtractionKeyword.findByIdAndDelete(id);
  await syncKeywordsToScript();
  return { removed: true };
}

export async function removeUserAlias(
  id: string,
  alias: string,
): Promise<ExtractionKeywordDto | { removed: boolean }> {
  const doc = await ExtractionKeyword.findById(id);
  if (!doc) return { removed: false };

  const target = normalizeAlias(alias).toLowerCase();
  if (!target) throw new Error("Similar word is required");

  const userAliases = (doc.user_aliases ?? []).filter(
    (a) => normalizeAlias(a).toLowerCase() !== target,
  );

  if (userAliases.length === (doc.user_aliases ?? []).length) {
    throw new Error("Similar word not found on this keyword");
  }

  doc.user_aliases = userAliases;

  if (doc.is_builtin) {
    const catalog = await fetchBuiltinCatalog();
    const row = catalog[doc.scope as ExtractionKeywordScope]?.find(
      (r) => r.label.toLowerCase() === doc.canonical_label.toLowerCase(),
    );
    const builtinAliases = uniqueAliases(row?.aliases ?? []);
    doc.aliases = uniqueAliases([...builtinAliases, ...userAliases]);
    doc.is_new_keyword = false;
    await doc.save();
    await syncKeywordsToScript();
    return toDto(doc.toObject());
  }

  if (userAliases.length === 0) {
    await ExtractionKeyword.findByIdAndDelete(id);
    await syncKeywordsToScript();
    return { removed: true };
  }

  doc.aliases = uniqueAliases(userAliases);
  await doc.save();
  await syncKeywordsToScript();
  return toDto(doc.toObject());
}
