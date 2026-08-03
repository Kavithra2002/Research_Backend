import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { env } from "../config/env";
import { HttpError } from "../utils/httpError";

/* ────────────────────────────────────────────────────────────────────────── *
 * Agent chat-log persistence
 *
 * Chat histories are stored as JSON files on disk so a user can close the
 * window and later re-open a past conversation (or start a new one):
 *
 *   Agent_chat_log/<agent>/<user_id>/<session_id>.json
 *
 * Each file is one chat session. The layout keeps every user's chats isolated
 * and lets us add more agents (e.g. "sage") later without code changes.
 * ────────────────────────────────────────────────────────────────────────── */

export const SUPPORTED_AGENTS = [
  "robin",
  "sage",
  "tuck",
  "marian",
  "jone",
] as const;
export type AgentId = (typeof SUPPORTED_AGENTS)[number];

export type StoredRole = "user" | "assistant";

export interface StoredMessage {
  role: StoredRole;
  content: string;
  ts: number;
}

export interface ChatSession {
  id: string;
  agent: AgentId;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: StoredMessage[];
}

export interface ChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;

function assertAgent(agent: string): AgentId {
  if ((SUPPORTED_AGENTS as readonly string[]).includes(agent)) {
    return agent as AgentId;
  }
  throw HttpError.badRequest(`Unknown agent: ${agent}`);
}

/** Make a filesystem-safe segment (no path traversal, no separators). */
function safeSegment(value: string, label: string): string {
  const cleaned = String(value ?? "").trim();
  if (!cleaned || !SAFE_SEGMENT.test(cleaned)) {
    throw HttpError.badRequest(`Invalid ${label}`);
  }
  return cleaned;
}

function userDir(agent: AgentId, userId: string): string {
  return path.join(env.chatLogDir, agent, safeSegment(userId, "user id"));
}

function sessionFile(agent: AgentId, userId: string, sessionId: string): string {
  return path.join(userDir(agent, userId), `${safeSegment(sessionId, "session id")}.json`);
}

async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

function deriveTitle(messages: StoredMessage[], fallback = "New chat"): string {
  const firstUser = messages.find((m) => m.role === "user" && m.content.trim());
  const raw = firstUser?.content.trim() || fallback;
  const oneLine = raw.replace(/\s+/g, " ");
  return oneLine.length > 60 ? `${oneLine.slice(0, 57)}…` : oneLine;
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Public API
 * ────────────────────────────────────────────────────────────────────────── */

export async function listSessions(
  agentRaw: string,
  userId: string,
): Promise<ChatSessionSummary[]> {
  const agent = assertAgent(agentRaw);
  const dir = userDir(agent, userId);

  let files: string[];
  try {
    files = await fs.readdir(dir);
  } catch {
    return []; // no chats yet
  }

  const summaries: ChatSessionSummary[] = [];
  for (const file of files) {
    if (!file.endsWith(".json")) continue;
    try {
      const raw = await fs.readFile(path.join(dir, file), "utf-8");
      const session = JSON.parse(raw) as ChatSession;
      summaries.push({
        id: session.id,
        title: session.title || deriveTitle(session.messages ?? []),
        created_at: session.created_at,
        updated_at: session.updated_at,
        message_count: Array.isArray(session.messages) ? session.messages.length : 0,
      });
    } catch {
      // ignore unreadable / malformed files
    }
  }

  summaries.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return summaries;
}

export async function getSession(
  agentRaw: string,
  userId: string,
  sessionId: string,
): Promise<ChatSession> {
  const agent = assertAgent(agentRaw);
  const file = sessionFile(agent, userId, sessionId);
  try {
    const raw = await fs.readFile(file, "utf-8");
    return JSON.parse(raw) as ChatSession;
  } catch {
    throw HttpError.notFound("Chat session not found");
  }
}

function sanitizeMessages(input: unknown): StoredMessage[] {
  if (!Array.isArray(input)) return [];
  const out: StoredMessage[] = [];
  for (const m of input) {
    if (!m || typeof m !== "object") continue;
    const role = (m as { role?: unknown }).role;
    const content = (m as { content?: unknown }).content;
    if (role !== "user" && role !== "assistant") continue;
    if (typeof content !== "string") continue;
    const ts = (m as { ts?: unknown }).ts;
    out.push({
      role,
      content,
      ts: typeof ts === "number" ? ts : Date.now(),
    });
  }
  return out;
}

export async function saveSession(
  agentRaw: string,
  userId: string,
  sessionId: string | null,
  payload: { title?: string; messages: unknown },
): Promise<ChatSession> {
  const agent = assertAgent(agentRaw);
  const id = sessionId ? safeSegment(sessionId, "session id") : randomUUID();
  const dir = userDir(agent, userId);
  await ensureDir(dir);

  const file = path.join(dir, `${id}.json`);
  const now = new Date().toISOString();
  const messages = sanitizeMessages(payload.messages);

  // Preserve created_at when updating an existing session.
  let createdAt = now;
  try {
    const existingRaw = await fs.readFile(file, "utf-8");
    const existing = JSON.parse(existingRaw) as ChatSession;
    if (existing.created_at) createdAt = existing.created_at;
  } catch {
    // new session
  }

  const session: ChatSession = {
    id,
    agent,
    user_id: safeSegment(userId, "user id"),
    title: (payload.title?.trim() || deriveTitle(messages)).slice(0, 80),
    created_at: createdAt,
    updated_at: now,
    messages,
  };

  await fs.writeFile(file, JSON.stringify(session, null, 2), "utf-8");
  return session;
}

export async function deleteSession(
  agentRaw: string,
  userId: string,
  sessionId: string,
): Promise<boolean> {
  const agent = assertAgent(agentRaw);
  const file = sessionFile(agent, userId, sessionId);
  try {
    await fs.unlink(file);
    return true;
  } catch {
    return false;
  }
}
