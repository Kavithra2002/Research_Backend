import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import { reindexAll, resolveCompanySlug } from "../services/rag/ingest";
import { searchChunks } from "../services/rag/vectorStore";

const reindexSchema = z.object({
  company: z.string().trim().min(1).optional(),
});

/** POST /ai/rag/reindex — (re)build the vector index. Admin only. */
export async function ragReindexHandler(req: Request, res: Response) {
  const parsed = reindexSchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  let companySlug: string | undefined;
  if (parsed.data.company) {
    companySlug = (await resolveCompanySlug(parsed.data.company)) ?? undefined;
  }

  const result = await reindexAll(companySlug);
  res.json({ ok: true, company_slug: companySlug ?? null, result });
}

const searchSchema = z.object({
  query: z.string().trim().min(1, "query is required"),
  company: z.string().trim().min(1).optional(),
  source: z.string().trim().min(1).optional(),
  top_k: z.number().int().min(1).max(20).optional(),
});

/** POST /ai/rag/search — direct vector search (handy for testing/debugging). */
export async function ragSearchHandler(req: Request, res: Response) {
  const parsed = searchSchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  let companySlug: string | undefined;
  if (parsed.data.company) {
    companySlug = (await resolveCompanySlug(parsed.data.company)) ?? undefined;
  }

  const matches = await searchChunks({
    query: parsed.data.query,
    companySlug,
    source: parsed.data.source,
    topK: parsed.data.top_k ?? 5,
  });

  res.json({ company_slug: companySlug ?? null, match_count: matches.length, matches });
}
