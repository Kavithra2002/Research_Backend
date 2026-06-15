import fs from "node:fs";
import path from "node:path";
import { FinancialTable } from "../../models/FinancialTable";
import { Company } from "../../models/Company";
import { indexChunks, type ChunkInput, type IndexResult } from "./vectorStore";

/* ────────────────────────────────────────────────────────────────────────── *
 * Helpers
 * ────────────────────────────────────────────────────────────────────────── */

/** Same slug rule the Python pipeline uses (`Q_data_extraction._slug`). */
export function slugify(name: string): string {
  const s = name
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .replace(/[\s_-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return s || "company";
}

function extractedJsonRoot(): string {
  return (
    process.env.EXTRACTED_JSON_DIR?.trim() ||
    path.resolve(process.cwd(), "Extracted_json")
  );
}

function pagesLabel(pages: unknown): string {
  if (!Array.isArray(pages) || pages.length === 0) return "";
  const nums = pages.filter((p): p is number => typeof p === "number");
  if (nums.length === 0) return "";
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  return min === max ? ` (page ${min})` : ` (pages ${min}–${max})`;
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Source 1 — financial tables (from MongoDB)
 * ────────────────────────────────────────────────────────────────────────── */

function flattenTable(doc: {
  statement_title: string;
  caption: string | null;
  preamble: string;
  footnotes: string;
  header_rows: string[][];
  rows: Array<{ cells?: string[] }> | unknown;
}): string {
  const parts: string[] = [doc.statement_title];
  if (doc.caption) parts.push(doc.caption);
  if (doc.preamble) parts.push(doc.preamble);
  for (const header of doc.header_rows ?? []) {
    parts.push(header.join(" | "));
  }
  const rows = Array.isArray(doc.rows) ? doc.rows : [];
  for (const row of rows as Array<{ cells?: string[] }>) {
    if (row && Array.isArray(row.cells)) parts.push(row.cells.join(" | "));
  }
  if (doc.footnotes) parts.push(doc.footnotes);
  return parts.filter(Boolean).join("\n");
}

export async function ingestFinancialTables(
  companySlug?: string,
): Promise<IndexResult> {
  const filter = companySlug ? { company_slug: companySlug } : {};
  const slugs: string[] = companySlug
    ? [companySlug]
    : await FinancialTable.distinct("company_slug", filter);

  const total: IndexResult = { inserted: 0, skipped: 0, deleted: 0 };

  for (const slug of slugs) {
    const docs = await FinancialTable.find({ company_slug: slug }).lean();
    const inputs: ChunkInput[] = docs.map((doc) => ({
      company_slug: slug,
      company_name: doc.company_name ?? null,
      source: "financial_table",
      section_key: doc.statement_key,
      section_title: doc.statement_title,
      year: doc.year ?? null,
      ref:
        `${doc.statement_title}` +
        (doc.year ? ` ${doc.year}` : "") +
        (doc.report_type ? ` (${doc.report_type})` : ""),
      text: flattenTable(doc),
    }));
    const res = await indexChunks(slug, "financial_table", inputs);
    total.inserted += res.inserted;
    total.skipped += res.skipped;
    total.deleted += res.deleted;
  }

  return total;
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Source 2 — non-financial digests (from disk JSON)
 * ────────────────────────────────────────────────────────────────────────── */

interface DigestSection {
  key?: string;
  title?: string;
  pages?: number[];
  text?: string;
}

interface Digest {
  company?: string;
  sections?: DigestSection[];
}

function findDigestFiles(root: string): string[] {
  if (!fs.existsSync(root)) return [];
  const out: string[] = [];
  for (const companyDir of fs.readdirSync(root)) {
    const file = path.join(
      root,
      companyDir,
      "non_financial",
      "non_financial_digest.json",
    );
    if (fs.existsSync(file)) out.push(file);
  }
  return out;
}

export async function ingestNonFinancialDigests(
  companySlug?: string,
): Promise<IndexResult> {
  const root = extractedJsonRoot();
  const files = findDigestFiles(root);

  const total: IndexResult = { inserted: 0, skipped: 0, deleted: 0 };

  for (const file of files) {
    let digest: Digest;
    try {
      digest = JSON.parse(fs.readFileSync(file, "utf8")) as Digest;
    } catch {
      continue;
    }
    const companyName = digest.company ?? path.basename(path.dirname(path.dirname(file)));
    const slug = slugify(companyName);
    if (companySlug && slug !== companySlug) continue;

    const sections = Array.isArray(digest.sections) ? digest.sections : [];
    const inputs: ChunkInput[] = sections
      .filter((s) => typeof s.text === "string" && s.text.trim().length > 0)
      .map((s) => ({
        company_slug: slug,
        company_name: companyName,
        source: "non_financial",
        section_key: s.key ?? null,
        section_title: s.title ?? s.key ?? null,
        year: null,
        ref: `${s.title ?? s.key ?? "Section"}${pagesLabel(s.pages)}`,
        text: s.text as string,
      }));

    const res = await indexChunks(slug, "non_financial", inputs);
    total.inserted += res.inserted;
    total.skipped += res.skipped;
    total.deleted += res.deleted;
  }

  return total;
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Orchestration
 * ────────────────────────────────────────────────────────────────────────── */

export interface ReindexResult {
  financial_table: IndexResult;
  non_financial: IndexResult;
}

/** Re-index every retrievable source (optionally scoped to one company). */
export async function reindexAll(companySlug?: string): Promise<ReindexResult> {
  const [financialTable, nonFinancial] = await Promise.all([
    ingestFinancialTables(companySlug),
    ingestNonFinancialDigests(companySlug),
  ]);
  return { financial_table: financialTable, non_financial: nonFinancial };
}

/** Resolve a company name/slug to a slug that exists in the chunk store. */
export async function resolveCompanySlug(
  nameOrSlug: string,
): Promise<string | null> {
  const term = nameOrSlug.trim();
  if (!term) return null;

  const direct = await Company.findOne({ slug: term }).select({ slug: 1 }).lean();
  if (direct) return direct.slug;

  const slug = slugify(term);
  const bySlug = await FinancialTable.findOne({ company_slug: slug })
    .select({ company_slug: 1 })
    .lean();
  if (bySlug) return bySlug.company_slug;

  return slug;
}
