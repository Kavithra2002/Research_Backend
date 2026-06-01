import nodemailer, { type Transporter } from "nodemailer";
import { env } from "../config/env";
import { logger } from "./logger";

let cachedTransporter: Transporter | null = null;
let verifiedOnce = false;

function isSmtpConfigured(): boolean {
  return Boolean(env.smtp.host && env.smtp.user && env.smtp.pass);
}

function stripEnvQuotes(value: string): string {
  const trimmed = value.trim();
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function getTransporter(): Transporter | null {
  if (!isSmtpConfigured()) return null;
  if (cachedTransporter) return cachedTransporter;

  const user = env.smtp.user ?? "";
  const pass = env.smtp.pass ?? "";
  const host = (env.smtp.host ?? "").toLowerCase();

  // Gmail preset works more reliably from cloud hosts (e.g. Render) than raw SMTP.
  if (host === "smtp.gmail.com" || host === "gmail") {
    cachedTransporter = nodemailer.createTransport({
      service: "gmail",
      auth: { user, pass },
    });
  } else {
    cachedTransporter = nodemailer.createTransport({
      host: env.smtp.host ?? undefined,
      port: env.smtp.port,
      secure: env.smtp.secure,
      requireTLS: env.smtp.port === 587 && !env.smtp.secure,
      auth: { user, pass },
      connectionTimeout: 20_000,
      greetingTimeout: 15_000,
      socketTimeout: 30_000,
    });
  }

  void cachedTransporter
    .verify()
    .then(() => {
      if (!verifiedOnce) {
        verifiedOnce = true;
        logger.info(
          `[mailer] SMTP transport ready (host=${env.smtp.host}, port=${env.smtp.port})`,
        );
      }
    })
    .catch((err) => {
      logger.error("[mailer] SMTP verification failed", err);
    });

  return cachedTransporter;
}

function getFromAddress(): string {
  const raw = env.smtp.from || env.smtp.user || "no-reply@ambeon.local";
  return stripEnvQuotes(raw);
}

function buildResetEmail(resetUrl: string) {
  const text = [
    "We received a request to reset your Ambeon Console password.",
    "",
    "Click the link below to choose a new password. This link expires in 15 minutes and can only be used once.",
    "",
    resetUrl,
    "",
    "If you didn't request this, you can safely ignore this email — your password will stay the same.",
  ].join("\n");

  const html = `<!doctype html>
<html>
  <body style="font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#f6f7f9; padding:24px;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:520px; margin:0 auto; background:#ffffff; border-radius:12px; padding:32px; border:1px solid #e5e7eb;">
      <tr>
        <td>
          <h1 style="margin:0 0 12px; font-size:20px; color:#111827;">Reset your password</h1>
          <p style="margin:0 0 16px; color:#374151; font-size:14px; line-height:1.55;">
            We received a request to reset your <strong>Ambeon Console</strong> password.
            Click the button below to choose a new one. This link expires in
            <strong>15 minutes</strong> and can only be used once.
          </p>
          <p style="margin:24px 0;">
            <a href="${resetUrl}"
               style="display:inline-block; background:#111827; color:#ffffff; text-decoration:none; padding:10px 18px; border-radius:8px; font-size:14px; font-weight:600;">
              Reset password
            </a>
          </p>
          <p style="margin:0 0 8px; color:#6b7280; font-size:12px;">Or copy and paste this link into your browser:</p>
          <p style="margin:0 0 24px; word-break:break-all;">
            <a href="${resetUrl}" style="color:#2563eb; font-size:12px;">${resetUrl}</a>
          </p>
          <hr style="border:none; border-top:1px solid #e5e7eb; margin:24px 0;" />
          <p style="margin:0; color:#6b7280; font-size:12px; line-height:1.5;">
            If you didn't request this, you can safely ignore this email — your password will stay the same.
          </p>
        </td>
      </tr>
    </table>
  </body>
</html>`;

  return { text, html };
}

export async function sendPasswordResetEmail(
  email: string,
  resetUrl: string,
): Promise<void> {
  const transporter = getTransporter();

  if (!transporter) {
    logger.warn(
      "[mailer] SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASS missing). Falling back to console log.",
    );
    logger.info(
      `[password-reset] Reset link for ${email} (expires in 15 min):\n${resetUrl}`,
    );
    return;
  }

  const { text, html } = buildResetEmail(resetUrl);

  try {
    const info = await transporter.sendMail({
      from: getFromAddress(),
      to: email,
      subject: "Reset your Ambeon Console password",
      text,
      html,
    });
    logger.info(
      `[mailer] Password reset email sent to ${email} (messageId=${info.messageId})`,
    );
  } catch (err) {
    logger.error(`[mailer] Failed to send password reset email to ${email}`, err);
    throw err;
  }
}
