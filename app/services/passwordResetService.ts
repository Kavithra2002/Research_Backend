import crypto from "node:crypto";
import { env } from "../config/env";
import { LoginHistory } from "../models/LoginHistory";
import { PasswordResetToken } from "../models/PasswordResetToken";
import { Session } from "../models/Session";
import { User } from "../models/User";
import { HttpError } from "../utils/httpError";
import { sendPasswordResetEmail } from "../utils/mailer";
import { hashPassword } from "../utils/password";
import { logger } from "../utils/logger";
import type { AuthMeta } from "./authService";

const RESET_TTL_MS = 15 * 60 * 1000;
const MIN_PASSWORD_LENGTH = 8;

const FORGOT_PASSWORD_MESSAGE =
  "If an account exists for that email, we've sent a password reset link. It expires in 15 minutes.";

function hashToken(raw: string): string {
  return crypto.createHash("sha256").update(raw).digest("hex");
}

function generateRawToken(): string {
  return crypto.randomBytes(32).toString("base64url");
}

async function recordPasswordResetEvent(args: {
  userId: import("mongoose").Types.ObjectId;
  userBusinessId: string;
  email: string;
  event: "password_reset_request" | "password_reset_success";
  success: boolean;
  meta: AuthMeta;
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
      session_id: null,
      logged_at: new Date(),
    });
  } catch (err) {
    logger.error("Failed to record password reset history", err);
  }
}

export async function requestPasswordReset(
  email: string,
  meta: AuthMeta = {},
): Promise<{ message: string }> {
  const normalizedEmail = email.trim().toLowerCase();
  const user = await User.findOne({ email: normalizedEmail });

  if (!user) {
    return { message: FORGOT_PASSWORD_MESSAGE };
  }

  if (user.user_status !== "active") {
    return { message: FORGOT_PASSWORD_MESSAGE };
  }

  await PasswordResetToken.updateMany(
    { user_id: user._id, used_at: null },
    { $set: { used_at: new Date() } },
  );

  const rawToken = generateRawToken();
  const expiresAt = new Date(Date.now() + RESET_TTL_MS);

  await PasswordResetToken.create({
    user_id: user._id,
    token_hash: hashToken(rawToken),
    expires_at: expiresAt,
    used_at: null,
    ip: meta.ip ?? null,
    user_agent: meta.userAgent ?? null,
    created_at: new Date(),
  });

  const baseUrl = env.frontendUrl.replace(/\/+$/, "");
  const resetUrl = `${baseUrl}/reset-password?token=${encodeURIComponent(rawToken)}`;

  // Do not block the HTTP response on SMTP (Gmail from Render can be slow or time out).
  void sendPasswordResetEmail(user.email, resetUrl).catch((err) => {
    const detail = err instanceof Error ? err.message : String(err);
    logger.error(
      `[password-reset] Email NOT delivered to ${user.email}. Token was saved. Error: ${detail}. ` +
        `Fix: add RESEND_API_KEY on Render (recommended) or refresh Gmail SMTP_PASS.`,
    );
    logger.error("[password-reset] Email delivery failure details", err);
  });

  await recordPasswordResetEvent({
    userId: user._id,
    userBusinessId: user.user_id,
    email: user.email,
    event: "password_reset_request",
    success: true,
    meta,
  });

  return { message: FORGOT_PASSWORD_MESSAGE };
}

export async function resetPassword(
  rawToken: string,
  newPassword: string,
  meta: AuthMeta = {},
): Promise<{ message: string }> {
  if (newPassword.length < MIN_PASSWORD_LENGTH) {
    throw HttpError.badRequest(
      `Password must be at least ${MIN_PASSWORD_LENGTH} characters long`,
    );
  }

  const tokenHash = hashToken(rawToken.trim());
  const now = new Date();

  const tokenDoc = await PasswordResetToken.findOne({
    token_hash: tokenHash,
    used_at: null,
    expires_at: { $gt: now },
  });

  if (!tokenDoc) {
    throw HttpError.badRequest("Invalid or expired reset link");
  }

  const user = await User.findById(tokenDoc.user_id);
  if (!user || user.user_status !== "active") {
    throw HttpError.badRequest("Invalid or expired reset link");
  }

  user.password = await hashPassword(newPassword);
  await user.save();

  tokenDoc.used_at = now;
  await tokenDoc.save();

  await Session.deleteMany({ user_id: user._id });

  await recordPasswordResetEvent({
    userId: user._id,
    userBusinessId: user.user_id,
    email: user.email,
    event: "password_reset_success",
    success: true,
    meta,
  });

  return { message: "Password updated successfully. You can sign in now." };
}
