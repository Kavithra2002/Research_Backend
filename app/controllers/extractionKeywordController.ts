import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  addNewKeyword,
  addSimilarKeyword,
  deleteUserKeyword,
  ensureBuiltinKeywordsSeeded,
  listCanonicalLabelOptions,
  listSheetLines,
  listUserKeywords,
  removeUserAlias,
  reorderNewKeyword,
  type OwnerMeta,
} from "../services/extractionKeywordService";

const scopeSchema = z.enum(["fs", "drivers", "quarterly"]);

const newKeywordSchema = z.object({
  canonical_label: z.string().trim().min(1, "Description keyword is required"),
  alias: z.string().trim().min(1, "Report wording is required"),
  scope: scopeSchema.default("fs"),
  line_order: z.number().int().min(0).nullable().optional(),
});

const reorderKeywordSchema = z.object({
  line_order: z.number().int().min(0),
});

const removeAliasSchema = z.object({
  alias: z.string().trim().min(1, "Similar word is required"),
});

const similarKeywordSchema = z.object({
  canonical_label: z.string().trim().min(1, "Existing keyword is required"),
  similar_word: z.string().trim().min(1, "Similar word is required"),
  scope: scopeSchema.default("fs"),
});

function unwrap<T>(parsed: z.SafeParseReturnType<unknown, T>): T {
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  return parsed.data;
}

function readMeta(req: Request): OwnerMeta {
  const user = req.user;
  return {
    userObjectId: user?._id ? String(user._id) : null,
    userBusinessId: user?.user_id ?? null,
  };
}

export async function seedKeywordsHandler(_req: Request, res: Response) {
  const result = await ensureBuiltinKeywordsSeeded();
  res.json(result);
}

export async function listKeywordsHandler(req: Request, res: Response) {
  const scopeRaw = typeof req.query.scope === "string" ? req.query.scope : "";
  const scope = scopeSchema.safeParse(scopeRaw);
  const keywords = await listUserKeywords(
    scope.success ? scope.data : undefined,
  );
  res.json({ keywords });
}

export async function suggestCanonicalLabelsHandler(
  req: Request,
  res: Response,
) {
  const scopeRaw = typeof req.query.scope === "string" ? req.query.scope : "";
  const scope = scopeSchema.safeParse(scopeRaw);
  const q = typeof req.query.q === "string" ? req.query.q : "";
  const options = await listCanonicalLabelOptions(
    scope.success ? scope.data : undefined,
    q,
  );
  res.json({ options });
}

export async function listSheetLinesHandler(req: Request, res: Response) {
  const scopeRaw = typeof req.query.scope === "string" ? req.query.scope : "fs";
  const scope = unwrap(scopeSchema.safeParse(scopeRaw));
  const lines = await listSheetLines(scope);
  res.json({ lines });
}

export async function reorderKeywordHandler(req: Request, res: Response) {
  const body = unwrap(reorderKeywordSchema.safeParse(req.body));
  try {
    const updated = await reorderNewKeyword(req.params.id, body.line_order);
    res.json(updated);
  } catch (err) {
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to reorder keyword",
    );
  }
}

export async function addNewKeywordHandler(req: Request, res: Response) {
  const body = unwrap(newKeywordSchema.safeParse(req.body));
  try {
    const created = await addNewKeyword(body, readMeta(req));
    res.status(201).json(created);
  } catch (err) {
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to add keyword",
    );
  }
}

export async function addSimilarKeywordHandler(req: Request, res: Response) {
  const body = unwrap(similarKeywordSchema.safeParse(req.body));
  try {
    const updated = await addSimilarKeyword(body, readMeta(req));
    res.status(201).json(updated);
  } catch (err) {
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to add similar word",
    );
  }
}

export async function deleteKeywordHandler(req: Request, res: Response) {
  const result = await deleteUserKeyword(req.params.id);
  res.json(result);
}

export async function removeUserAliasHandler(req: Request, res: Response) {
  const body = unwrap(removeAliasSchema.safeParse(req.body));
  try {
    const result = await removeUserAlias(req.params.id, body.alias);
    res.json(result);
  } catch (err) {
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to remove similar word",
    );
  }
}
