import type { Request, Response } from "express";
import { z } from "zod";
import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import {
  SESSION_TTL_SECONDS,
  getUserBySession,
  login,
  logout,
  signup,
} from "../services/authService";
import {
  requestPasswordReset,
  resetPassword,
} from "../services/passwordResetService";

export const SESSION_COOKIE_NAME = "sid";

const loginSchema = z.object({
  email: z.string().trim().email(),
  password: z.string().min(1, "Password is required"),
});

const signupSchema = z.object({
  first_name: z.string().trim().min(1, "First name is required").max(80),
  last_name: z.string().trim().min(1, "Last name is required").max(80),
  email: z.string().trim().email(),
  password: z
    .string()
    .min(8, "Password must be at least 8 characters long")
    .max(128),
});

const forgotPasswordSchema = z.object({
  email: z.string().trim().email(),
});

const resetPasswordSchema = z.object({
  token: z.string().trim().min(1, "Reset token is required"),
  password: z
    .string()
    .min(8, "Password must be at least 8 characters long")
    .max(128),
});

function readClientMeta(req: Request) {
  const ip =
    (typeof req.headers["x-forwarded-for"] === "string"
      ? req.headers["x-forwarded-for"].split(",")[0]?.trim()
      : null) ||
    req.ip ||
    null;
  const userAgent =
    typeof req.headers["user-agent"] === "string"
      ? req.headers["user-agent"]
      : null;
  return { ip, userAgent };
}

function unwrap<T>(parsed: z.SafeParseReturnType<unknown, T>): T {
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  return parsed.data;
}

function cookieOptions(maxAgeSeconds: number) {
  const isProd = env.nodeEnv === "production";
  return {
    httpOnly: true,
    sameSite: (isProd ? "none" : "lax") as "none" | "lax",
    secure: isProd,
    path: "/",
    maxAge: maxAgeSeconds * 1000,
  };
}

function clearCookieOptions() {
  const isProd = env.nodeEnv === "production";
  return {
    httpOnly: true,
    sameSite: (isProd ? "none" : "lax") as "none" | "lax",
    secure: isProd,
    path: "/",
  };
}

export async function loginHandler(req: Request, res: Response) {
  const { email, password } = unwrap(loginSchema.safeParse(req.body));

  const meta = readClientMeta(req);
  const result = await login(email, password, meta);

  res.cookie(
    SESSION_COOKIE_NAME,
    result.sessionId,
    cookieOptions(SESSION_TTL_SECONDS),
  );

  res.json({
    user: result.user,
    session: { expires_at: result.expiresAt },
  });
}

export async function signupHandler(req: Request, res: Response) {
  const body = unwrap(signupSchema.safeParse(req.body));

  const meta = readClientMeta(req);
  const result = await signup(body, meta);

  res.cookie(
    SESSION_COOKIE_NAME,
    result.sessionId,
    cookieOptions(SESSION_TTL_SECONDS),
  );

  res.status(201).json({
    user: result.user,
    session: { expires_at: result.expiresAt },
  });
}

export async function logoutHandler(req: Request, res: Response) {
  const sid =
    (req.cookies as Record<string, string | undefined> | undefined)?.[
      SESSION_COOKIE_NAME
    ] ?? "";
  const meta = readClientMeta(req);
  await logout(sid, meta);
  res.clearCookie(SESSION_COOKIE_NAME, clearCookieOptions());
  res.json({ ok: true });
}

export async function forgotPasswordHandler(req: Request, res: Response) {
  const { email } = unwrap(forgotPasswordSchema.safeParse(req.body));
  const meta = readClientMeta(req);
  const result = await requestPasswordReset(email, meta);
  res.json(result);
}

export async function resetPasswordHandler(req: Request, res: Response) {
  const { token, password } = unwrap(resetPasswordSchema.safeParse(req.body));
  const meta = readClientMeta(req);
  const result = await resetPassword(token, password, meta);
  res.json(result);
}

export async function meHandler(req: Request, res: Response) {
  const sid =
    (req.cookies as Record<string, string | undefined> | undefined)?.[
      SESSION_COOKIE_NAME
    ] ?? "";

  const user = await getUserBySession(sid);
  if (!user) {
    res.status(401).json({ error: "Unauthorized", message: "Not signed in" });
    return;
  }
  res.json({ user });
}
