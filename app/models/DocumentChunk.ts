import { Schema, model, type HydratedDocument } from "mongoose";

/**
 * A single embedded text chunk used for RAG (retrieval-augmented generation).
 *
 * The extraction pipeline produces two kinds of text we can retrieve over:
 *   • "financial_table"  — the narrative around a stored statement (title,
 *                          caption, preamble, footnotes, flattened rows).
 *   • "non_financial"    — qualitative report sections (About Us, Chairman's
 *                          review, Risk Management, …) from the digest JSON.
 *
 * Each chunk stores its OpenAI embedding inline so vector search can run
 * entirely against the local MongoDB (cosine similarity is computed in-app).
 */
export interface DocumentChunkAttrs {
  company_slug: string;
  company_name: string | null;
  source: string;
  section_key: string | null;
  section_title: string | null;
  year: number | null;
  /** Human-readable citation, e.g. "Risk Management Report (pages 75–76)". */
  ref: string | null;
  chunk_index: number;
  text: string;
  embedding: number[];
  dim: number;
  embed_model: string;
  /** sha256 of source+section+text, used to skip re-embedding unchanged text. */
  content_hash: string;
  created_at: string;
}

const documentChunkSchema = new Schema<DocumentChunkAttrs>(
  {
    company_slug: { type: String, required: true, index: true },
    company_name: { type: String, default: null },
    source: { type: String, required: true, index: true },
    section_key: { type: String, default: null },
    section_title: { type: String, default: null },
    year: { type: Number, default: null },
    ref: { type: String, default: null },
    chunk_index: { type: Number, required: true },
    text: { type: String, required: true },
    embedding: { type: [Number], required: true },
    dim: { type: Number, required: true },
    embed_model: { type: String, required: true },
    content_hash: { type: String, required: true, index: true },
    created_at: { type: String, required: true },
  },
  { collection: "document_chunks", versionKey: false },
);

documentChunkSchema.index(
  { company_slug: 1, source: 1, content_hash: 1 },
  { unique: true, name: "uniq_company_source_hash" },
);

export type DocumentChunkDocument = HydratedDocument<DocumentChunkAttrs>;

export const DocumentChunk = model<DocumentChunkAttrs>(
  "DocumentChunk",
  documentChunkSchema,
);
