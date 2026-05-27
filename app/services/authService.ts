import crypto from "node:crypto";
import { User, type UserRole } from "../models/User";
import { Session } from "../models/Session";
import { LoginHistory, type LoginEvent } from "../models/LoginHistory";
import { HttpError } from "../utils/httpError";
import { hashPassword, verifyPassword } from "../utils/password";
import { logger } from "../utils/logger";

const SESSION_TTL_MS = 1000 * 60 * 60 * 24 * 7;
const GENERIC_AUTH_ERROR = "Invalid email or password";

export interface AuthMeta {
  ip?: string | null;
  userAgent?: string | null;
}

export interface AuthenticatedUser {
  _id: string;
  user_id: string;
  first_name: string;
  last_name: string;
  email: string;
  user_status: string;
  role: UserRole;
  last_login: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface LoginResult {
  sessionId: string;
  expiresAt: Date;
  user: AuthenticatedUser;
}

export interface SignupInput {
  first_name: string;
  last_name: string;
  email: string;
  password: string;
}

export function generateSessionId(): string {
  return crypto.randomBytes(32).toString("base64url");
}

async function recordLoginEvent(args: {
  userId: import("mongoose").Types.ObjectId;
  userBusinessId: string;
  email: string;
  event: LoginEvent;
  success: boolean;
  meta: AuthMeta;
  sessionId?: string | null;
}) {
  try {
    await LoginHistory.create({
      user_id: args.userId,
      user_business_id: args.userBusinessId,
      email: args.email,
      event: args.event,
      success: args.success,
      ip: args.meta.ip ?? null,
      user_agent: args.meta.userAgent ?? null,
      session_id: args.sessionId ?? null,
      logged_at: new Date(),
    });
  } catch (err) {
    logger.error("Failed to record login history", err);
  }
}

async function generateNextUserId(): Promise<string> {
  const last = await User.findOne({ user_id: /^USR_\d+$/ })
    .sort({ user_id: -1 })
    .lean();

  const lastId = last?.user_id;
  if (!lastId) return "USR_001";
  const match = /^USR_(\d+)$/.exec(lastId);
  if (!match) return "USR_001";
  const next = Number(match[1]) + 1;
  return `USR_${String(next).padStart(3, "0")}`;
}

export async function signup(
  input: SignupInput,
  meta: AuthMeta = {},
): Promise<LoginResult> {
  const normalizedEmail = input.email.trim().toLowerCase();
  const firstName = input.first_name.trim();
  const lastName = input.last_name.trim();

  if (!firstName) throw HttpError.badRequest("First name is required");
  if (!lastName) throw HttpError.badRequest("Last name is required");

  const existing = await User.findOne({ email: normalizedEmail }).lean();
  if (existing) {
    throw HttpError.conflict("An account with this email already exists");
  }

  const hashed = await hashPassword(input.password);
  const userBusinessId = await generateNextUserId();
  const now = new Date();

  const created = await User.create({
    user_id: userBusinessId,
    first_name: firstName,
    last_name: lastName,
    email: normalizedEmail,
    password: hashed,
    user_status: "active",
    role: "User",
    last_login: now,
  });

  const sessionId = generateSessionId();
  const expiresAt = new Date(Date.now() + SESSION_TTL_MS);
  await Session.create({
    _id: sessionId,
    user_id: created._id,
    user_business_id: created.user_id,
    expires_at: expiresAt,
    created_at: new Date(),
    ip: meta.ip ?? null,
    user_agent: meta.userAgent ?? null,
  });

  await recordLoginEvent({
    userId: created._id,
    userBusinessId: created.user_id,
    email: created.email,
    event: "signup",
    success: true,
    meta,
    sessionId,
  });

  return {
    sessionId,
    expiresAt,
    user: toAuthenticatedUser(
      created.toObject() as unknown as Record<string, unknown>,
    ),
  };
}

export async function login(
  email: string,
  password: string,
  meta: AuthMeta = {},
): Promise<LoginResult> {
  const normalizedEmail = email.trim().toLowerCase();

  const user = await User.findOne({ email: normalizedEmail });
  if (!user) throw HttpError.badRequest(GENERIC_AUTH_ERROR);

  if (user.user_status && user.user_status !== "active") {
    throw HttpError.badRequest("Account is not active");
  }

  const ok = await verifyPassword(password, user.password);
  if (!ok) throw HttpError.badRequest(GENERIC_AUTH_ERROR);

  user.last_login = new Date();
  await user.save();

  const sessionId = generateSessionId();
  const expiresAt = new Date(Date.now() + SESSION_TTL_MS);
  await Session.create({
    _id: sessionId,
    user_id: user._id,
    user_business_id: user.user_id,
    expires_at: expiresAt,
    created_at: new Date(),
    ip: meta.ip ?? null,
    user_agent: meta.userAgent ?? null,
  });

  await recordLoginEvent({
    userId: user._id,
    userBusinessId: user.user_id,
    email: user.email,
    event: "login",
    success: true,
    meta,
    sessionId,
  });

  return {
    sessionId,
    expiresAt,
    user: toAuthenticatedUser(user.toObject() as unknown as Record<string, unknown>),
  };
}

export async function logout(
  sessionId: string,
  meta: AuthMeta = {},
): Promise<void> {
  if (!sessionId) return;
  const session = await Session.findById(sessionId);
  await Session.deleteOne({ _id: sessionId });
  if (session) {
    const userForLogout = await User.findById(session.user_id).lean();
    await recordLoginEvent({
      userId: session.user_id,
      userBusinessId: session.user_business_id,
      email: userForLogout?.email ?? "",
      event: "logout",
      success: true,
      meta,
      sessionId,
    });
  }
}

export async function getUserBySession(
  sessionId: string,
): Promise<AuthenticatedUser | null> {
  if (!sessionId) return null;

  const session = await Session.findOne({
    _id: sessionId,
    expires_at: { $gt: new Date() },
  });
  if (!session) return null;

  const user = await User.findById(session.user_id);
  if (!user || user.user_status !== "active") return null;

  return toAuthenticatedUser(user.toObject() as unknown as Record<string, unknown>);
}

export const SESSION_TTL_SECONDS = SESSION_TTL_MS / 1000;

function toAuthenticatedUser(doc: Record<string, unknown>): AuthenticatedUser {
  const rawRole = doc.role;
  const role: UserRole =
    rawRole === "Admin" || rawRole === "User" ? rawRole : "User";
  return {
    _id: String(doc._id),
    user_id: String(doc.user_id ?? ""),
    first_name: String(doc.first_name ?? ""),
    last_name: String(doc.last_name ?? ""),
    email: String(doc.email ?? ""),
    user_status: String(doc.user_status ?? "active"),
    role,
    last_login: (doc.last_login as Date | null) ?? null,
    created_at: doc.created_at as Date,
    updated_at: doc.updated_at as Date,
  };
}
