import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import { LOGIN_EVENTS } from "../models/LoginHistory";
import {
  getLastLoginForUser,
  listLoginHistory,
} from "../services/loginHistoryService";

const listQuerySchema = z.object({
  userId: z.string().trim().optional(),
  userBusinessId: z.string().trim().optional(),
  event: z.enum(LOGIN_EVENTS).optional(),
  limit: z.coerce.number().int().positive().max(500).optional(),
});

function unwrap<T>(parsed: z.SafeParseReturnType<unknown, T>): T {
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  return parsed.data;
}

export async function listLoginHistoryHandler(req: Request, res: Response) {
  const q = unwrap(listQuerySchema.safeParse(req.query));
  const result = await listLoginHistory(q);
  res.json(result);
}

export async function lastLoginHandler(req: Request, res: Response) {
  const { id } = req.params;
  const item = await getLastLoginForUser(id);
  res.json({ item });
}
