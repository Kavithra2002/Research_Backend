import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  addSelection,
  clearSelections,
  listSelections,
  removeSelection,
  replaceSelections,
  type SelectorMeta,
} from "../services/reportSelectionService";

const itemSchema = z.object({
  company: z.string().trim().min(1),
  report_type: z.string().trim().min(1),
  file_name: z.string().trim().min(1),
});

const replaceSchema = z.object({
  items: z.array(itemSchema),
});

function unwrap<T>(parsed: z.SafeParseReturnType<unknown, T>): T {
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  return parsed.data;
}

function readMeta(req: Request): SelectorMeta {
  const user = req.user;
  return {
    userObjectId: user?._id ?? null,
    userBusinessId: user?.user_id ?? null,
  };
}

export async function listSelectionsHandler(_req: Request, res: Response) {
  const items = await listSelections();
  res.json({ items });
}

export async function addSelectionHandler(req: Request, res: Response) {
  const body = unwrap(itemSchema.safeParse(req.body));
  const created = await addSelection(body, readMeta(req));
  res.status(201).json(created);
}

export async function removeSelectionHandler(req: Request, res: Response) {
  const body = unwrap(itemSchema.safeParse(req.body));
  const result = await removeSelection(body);
  res.json(result);
}

export async function replaceSelectionsHandler(req: Request, res: Response) {
  const body = unwrap(replaceSchema.safeParse(req.body));
  const result = await replaceSelections(body.items, readMeta(req));
  res.json(result);
}

export async function clearSelectionsHandler(_req: Request, res: Response) {
  const result = await clearSelections();
  res.json(result);
}
