import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  deleteSession,
  getSession,
  listSessions,
  saveSession,
} from "../services/chatLogService";

function requireUserId(req: Request): string {
  const user = req.user;
  if (!user || !user.user_id) {
    throw HttpError.badRequest("Missing authenticated user");
  }
  return user.user_id;
}

const messageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
  ts: z.number().optional(),
});

const saveSchema = z.object({
  session_id: z.string().optional().nullable(),
  title: z.string().optional(),
  messages: z.array(messageSchema),
});

export async function listChatSessionsHandler(req: Request, res: Response) {
  const userId = requireUserId(req);
  const sessions = await listSessions(req.params.agent, userId);
  res.json({ sessions });
}

export async function getChatSessionHandler(req: Request, res: Response) {
  const userId = requireUserId(req);
  const session = await getSession(req.params.agent, userId, req.params.id);
  res.json({ session });
}

export async function saveChatSessionHandler(req: Request, res: Response) {
  const userId = requireUserId(req);
  const parsed = saveSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  const session = await saveSession(
    req.params.agent,
    userId,
    parsed.data.session_id ?? null,
    { title: parsed.data.title, messages: parsed.data.messages },
  );
  res.json({ session });
}

export async function deleteChatSessionHandler(req: Request, res: Response) {
  const userId = requireUserId(req);
  const removed = await deleteSession(req.params.agent, userId, req.params.id);
  res.json({ removed });
}
