import { createHash } from "node:crypto";
import { DocumentChunk } from "../../models/DocumentChunk";
import { env } from "../../config/env";
import { embedText, embedTexts } from "./embeddings";

/* ────────────────────────────────────────────────────────────────────────── *
 * Chunking
 * ────────────────────────────────────────────────────────────────────────── */

const DEFAULT_MAX_CHARS = 1500;
const DEFAULT_OVERLAP = 200;

/**
 * Split long text into overlapping chunks on paragraph/sentence boundaries so
 * each chunk is a coherent passage small enough to embed and cite.
 */
export function chunkText(
  text: string,
  maxChars = DEFAULT_MAX_CHARS,
  overlap = DEFAULT_OVERLAP,
): string[] {
  const clean = text.replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  if (!clean) return [];
  if (clean.length <= maxChars) return [clean];

  // Prefer to break on blank lines, then single newlines, then spaces.
  const paragraphs = clean.split(/\n{2,}/);
  const chunks: string[] = [];
  let current = "";

  const flush = () => {
    const trimmed = current.trim();
    if (trimmed) chunks.push(trimmed);
    current = "";
  };

  for (const para of paragraphs) {
    if (para.length > maxChars) {
      // Hard-split an oversized paragraph by characters.
      flush();
      for (let i = 0; i < para.length; i += maxChars - overlap) {
        chunks.push(para.slice(i, i + maxChars).trim());
      }
      continue;
    }
    if ((current + "\n\n" + para).length > maxChars) {
      flush();
      current = para;
    } else {
      current = current ? `${current}\n\n${para}` : para;
    }
  }
  flush();

  return chunks.filter(Boolean);
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Similarity
 * ────────────────────────────────────────────────────────────────────────── */

/** Cosine similarity in [-1, 1]. Returns 0 for mismatched/empty vectors. */
export function cosine(a: number[], b: number[]): number {
  if (!a || !b || a.length !== b.length || a.length === 0) return 0;
  let dot = 0;
  let normA = 0;
  let normB = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  if (normA === 0 || normB === 0) return 0;
  return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Indexing (write)
 * ────────────────────────────────────────────────────────────────────────── */

export interface ChunkInput {
  company_slug: string;
  company_name: string | null;
  source: string;
  section_key: string | null;
  section_title: string | null;
  year: number | null;
  ref: string | null;
  text: string;
}

function hashChunk(input: ChunkInput, chunkIndex: number): string {
  return createHash("sha256")
    .update(
      [
        input.company_slug,
        input.source,
        input.section_key ?? "",
        input.year ?? "",
        chunkIndex,
        input.text,
      ].join("\u0000"),
    )
    .digest("hex");
}

export interface IndexResult {
  inserted: number;
  skipped: number;
  deleted: number;
}

/**
 * Index all chunks for a single (company_slug, source) pair. Embeds only chunks
 * whose content is new (hash-based), then prunes any stale chunks that are no
 * longer present. Safe to call repeatedly (idempotent reindex).
 */
export async function indexChunks(
  companySlug: string,
  source: string,
  inputs: ChunkInput[],
): Promise<IndexResult> {
  // Expand each input into its sub-chunks with stable hashes.
  const expanded: Array<{ input: ChunkInput; chunkIndex: number; hash: string }> =
    [];
  for (const input of inputs) {
    const pieces = chunkText(input.text);
    pieces.forEach((piece, idx) => {
      const withPiece = { ...input, text: piece };
      expanded.push({
        input: withPiece,
        chunkIndex: idx,
        hash: hashChunk(withPiece, idx),
      });
    });
  }

  const desiredHashes = new Set(expanded.map((e) => e.hash));

  const existing = await DocumentChunk.find({
    company_slug: companySlug,
    source,
  })
    .select({ content_hash: 1 })
    .lean();
  const existingHashes = new Set(existing.map((d) => d.content_hash));

  const toInsert = expanded.filter((e) => !existingHashes.has(e.hash));

  let inserted = 0;
  if (toInsert.length > 0) {
    const vectors = await embedTexts(toInsert.map((e) => e.input.text));
    const now = new Date().toISOString();
    const docs = toInsert.map((e, i) => ({
      company_slug: e.input.company_slug,
      company_name: e.input.company_name,
      source: e.input.source,
      section_key: e.input.section_key,
      section_title: e.input.section_title,
      year: e.input.year,
      ref: e.input.ref,
      chunk_index: e.chunkIndex,
      text: e.input.text,
      embedding: vectors[i],
      dim: vectors[i]?.length ?? 0,
      embed_model: env.openai.embedModel,
      content_hash: e.hash,
      created_at: now,
    }));
    const res = await DocumentChunk.insertMany(docs, { ordered: false });
    inserted = res.length;
  }

  // Prune chunks that are no longer part of the desired set.
  const staleHashes = [...existingHashes].filter((h) => !desiredHashes.has(h));
  let deleted = 0;
  if (staleHashes.length > 0) {
    const res = await DocumentChunk.deleteMany({
      company_slug: companySlug,
      source,
      content_hash: { $in: staleHashes },
    });
    deleted = res.deletedCount ?? 0;
  }

  return {
    inserted,
    skipped: expanded.length - toInsert.length,
    deleted,
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Search (read)
 * ────────────────────────────────────────────────────────────────────────── */

export interface SearchOptions {
  query: string;
  companySlug?: string;
  source?: string;
  topK?: number;
  /** Cap how many candidate chunks are loaded into memory for scoring. */
  maxCandidates?: number;
}

export interface SearchMatch {
  score: number;
  company_slug: string;
  company_name: string | null;
  source: string;
  section_title: string | null;
  year: number | null;
  ref: string | null;
  text: string;
}

/**
 * Vector search: embed the query, load candidate chunks from MongoDB and rank
 * them by cosine similarity in-app. Filtering by company keeps the candidate
 * set small, which is what makes the in-app approach fast enough.
 */
export async function searchChunks(
  opts: SearchOptions,
): Promise<SearchMatch[]> {
  const topK = opts.topK ?? 5;
  const maxCandidates = opts.maxCandidates ?? 5000;

  const filter: Record<string, unknown> = {};
  if (opts.companySlug) filter.company_slug = opts.companySlug;
  if (opts.source) filter.source = opts.source;

  const [queryVec, candidates] = await Promise.all([
    embedText(opts.query),
    DocumentChunk.find(filter).limit(maxCandidates).lean(),
  ]);

  if (queryVec.length === 0 || candidates.length === 0) return [];

  const scored = candidates
    .map((doc) => ({
      score: cosine(queryVec, doc.embedding),
      company_slug: doc.company_slug,
      company_name: doc.company_name ?? null,
      source: doc.source,
      section_title: doc.section_title ?? null,
      year: doc.year ?? null,
      ref: doc.ref ?? null,
      text: doc.text,
    }))
    .sort((a, b) => b.score - a.score);

  return scored.slice(0, topK);
}
