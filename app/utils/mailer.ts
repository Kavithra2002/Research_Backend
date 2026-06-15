import nodemailer, { type Transporter } from "nodemailer";
import { env } from "../config/env";
import { logger } from "./logger";

let cachedTransporter: Transporter | null = null;
let verifiedOnce = false;

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

function normalizeAppPassword(pass: string): string {
  return pass.replace(/\s+/g, "");
}

function isSmtpConfigured(): boolean {
  return Boolean(env.smtp.host && env.smtp.user && env.smtp.pass);
}

function isResendConfigured(): boolean {
  return Boolean(env.resend.apiKey);
}

function buildTransporter(options: {
  host?: string;
  port: number;
  secure: boolean;
  service?: string;
}): Transporter {
  const user = env.smtp.user ?? "";
  const pass = normalizeAppPassword(env.smtp.pass ?? "");

  if (options.service === "gmail") {
    return nodemailer.createTransport({
      service: "gmail",
      auth: { user, pass },
    });
  }

  return nodemailer.createTransport({
    host: options.host ?? env.smtp.host ?? undefined,
    port: options.port,
    secure: options.secure,
    requireTLS: options.port === 587 && !options.secure,
    auth: { user, pass },
    connectionTimeout: 20_000,
    greetingTimeout: 15_000,
    socketTimeout: 30_000,
  });
}

function getPrimarySmtpTransporter(): Transporter | null {
  if (!isSmtpConfigured()) return null;
  if (cachedTransporter) return cachedTransporter;

  const host = (env.smtp.host ?? "").toLowerCase();
  if (host === "smtp.gmail.com" || host === "gmail") {
    cachedTransporter = buildTransporter({
      service: "gmail",
      port: 465,
      secure: true,
    });
  } else {
    cachedTransporter = buildTransporter({
      port: env.smtp.port,
      secure: env.smtp.secure,
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

function getResendFromAddress(): string {
  return (
    stripEnvQuotes(env.resend.from ?? "") ||
    "Ambeon Console <onboarding@resend.dev>"
  );
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

  return { text, html, subject: "Reset your Ambeon Console password" };
}

export interface MailAttachment {
  filename: string;
  content: Buffer;
  contentType?: string;
}

async function sendViaResend(
  to: string,
  subject: string,
  text: string,
  html: string,
  attachments?: MailAttachment[],
): Promise<void> {
  const apiKey = env.resend.apiKey;
  if (!apiKey) throw new Error("RESEND_API_KEY is not set");

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: getResendFromAddress(),
      to: [to],
      subject,
      html,
      text,
      ...(attachments && attachments.length
        ? {
            attachments: attachments.map((a) => ({
              filename: a.filename,
              content: a.content.toString("base64"),
            })),
          }
        : {}),
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Resend API ${res.status}: ${body}`);
  }

  const payload = (await res.json()) as { id?: string };
  logger.info(`[mailer] Email sent via Resend to ${to} (id=${payload.id ?? "?"})`);
}

async function sendViaSmtp(
  to: string,
  subject: string,
  text: string,
  html: string,
  attachments?: MailAttachment[],
): Promise<void> {
  const host = (env.smtp.host ?? "").toLowerCase();
  const isGmail = host === "smtp.gmail.com" || host === "gmail";

  const attempts: Array<{ label: string; transporter: Transporter }> = [];

  if (isGmail) {
    attempts.push({
      label: "gmail-service",
      transporter: buildTransporter({ service: "gmail", port: 465, secure: true }),
    });
    attempts.push({
      label: "gmail-465",
      transporter: buildTransporter({
        host: "smtp.gmail.com",
        port: 465,
        secure: true,
      }),
    });
    attempts.push({
      label: "gmail-587",
      transporter: buildTransporter({
        host: "smtp.gmail.com",
        port: 587,
        secure: false,
      }),
    });
  } else {
    const primary = getPrimarySmtpTransporter();
    if (primary) {
      attempts.push({ label: "primary", transporter: primary });
    }
  }

  if (attempts.length === 0) {
    throw new Error("SMTP is not configured");
  }

  let lastError: unknown;
  for (const attempt of attempts) {
    try {
      const info = await attempt.transporter.sendMail({
        from: getFromAddress(),
        to,
        subject,
        text,
        html,
        ...(attachments && attachments.length
          ? {
              attachments: attachments.map((a) => ({
                filename: a.filename,
                content: a.content,
                contentType: a.contentType ?? "application/octet-stream",
              })),
            }
          : {}),
      });
      logger.info(
        `[mailer] Email sent via SMTP (${attempt.label}) to ${to} (messageId=${info.messageId})`,
      );
      return;
    } catch (err) {
      lastError = err;
      logger.warn(`[mailer] SMTP attempt "${attempt.label}" failed for ${to}`, err);
    }
  }

  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

/**
 * Deliver an email through whichever provider is configured (Resend preferred,
 * then SMTP). Returns false when no provider is configured.
 */
async function deliver(
  to: string,
  subject: string,
  text: string,
  html: string,
  attachments?: MailAttachment[],
): Promise<boolean> {
  if (isResendConfigured()) {
    await sendViaResend(to, subject, text, html, attachments);
    return true;
  }
  if (isSmtpConfigured()) {
    await sendViaSmtp(to, subject, text, html, attachments);
    return true;
  }
  return false;
}

export async function sendPasswordResetEmail(
  email: string,
  resetUrl: string,
): Promise<void> {
  const { text, html, subject } = buildResetEmail(resetUrl);

  const sent = await deliver(email, subject, text, html);
  if (!sent) {
    logger.warn(
      "[mailer] No email provider configured (set RESEND_API_KEY or SMTP_*). Logging reset link only.",
    );
    logger.info(
      `[password-reset] Reset link for ${email} (expires in 15 min):\n${resetUrl}`,
    );
  }
}

/** True when at least one email provider (Resend or SMTP) is configured. */
export function isMailConfigured(): boolean {
  return isResendConfigured() || isSmtpConfigured();
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Inline Markdown → HTML for **bold** and `code` within a single line. */
function renderInline(line: string): string {
  return escapeHtml(line)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

/**
 * Minimal Markdown → HTML renderer good enough for the agent reports
 * (bold lines, bullet lists and GitHub-style pipe tables).
 */
function renderReportBody(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let i = 0;

  const isTableRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
  const isDivider = (l: string) => /^\s*\|?[\s:|-]+\|?\s*$/.test(l) && l.includes("-");
  const cells = (l: string) =>
    l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  while (i < lines.length) {
    const line = lines[i];

    if (isTableRow(line) && i + 1 < lines.length && isDivider(lines[i + 1])) {
      const header = cells(line);
      i += 2;
      const body: string[][] = [];
      while (i < lines.length && isTableRow(lines[i])) {
        body.push(cells(lines[i]));
        i += 1;
      }
      const th = header.map((c) => `<th align="left">${renderInline(c)}</th>`).join("");
      const rows = body
        .map(
          (r) =>
            `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join("")}</tr>`,
        )
        .join("");
      out.push(
        `<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:13px;">` +
          `<thead><tr style="background:#f3f4f6;">${th}</tr></thead>` +
          `<tbody>${rows}</tbody></table>`,
      );
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*[-*]\s+/, ""))}</li>`);
        i += 1;
      }
      out.push(`<ul style="margin:6px 0 6px 18px;padding:0;">${items.join("")}</ul>`);
      continue;
    }

    if (line.trim() === "") {
      out.push("");
    } else {
      out.push(`<p style="margin:6px 0;">${renderInline(line)}</p>`);
    }
    i += 1;
  }

  return out.join("\n");
}

function buildReportEmail(title: string, markdown: string) {
  const body = renderReportBody(markdown);
  const html = `<!doctype html>
<html>
  <body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;padding:24px;color:#111827;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px;border:1px solid #e5e7eb;">
      <tr><td>
        <p style="margin:0 0 4px;font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:#6b7280;">Ambeon Console</p>
        <h1 style="margin:0 0 16px;font-size:20px;">${escapeHtml(title)}</h1>
        ${body}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 12px;" />
        <p style="margin:0;color:#9ca3af;font-size:11px;">This report was generated and sent automatically by the Ambeon Console.</p>
      </td></tr>
    </table>
  </body>
</html>`;
  return { html, text: markdown };
}

/**
 * Send a generated agent report (Markdown) to one or more recipients. Each
 * address is delivered independently so one bad address doesn't block the rest.
 * Returns the list of addresses that were sent successfully.
 */
export async function sendReportEmail(
  recipients: string | string[],
  subject: string,
  markdown: string,
  title?: string,
): Promise<string[]> {
  const list = (Array.isArray(recipients) ? recipients : [recipients])
    .map((e) => e.trim())
    .filter(Boolean);
  if (list.length === 0) return [];

  const { html, text } = buildReportEmail(title ?? subject, markdown);
  const delivered: string[] = [];

  for (const to of list) {
    try {
      const ok = await deliver(to, subject, text, html);
      if (ok) {
        delivered.push(to);
      } else {
        logger.warn(
          `[mailer] No email provider configured — cannot send report to ${to}.`,
        );
      }
    } catch (err) {
      logger.error(`[mailer] Failed to send report to ${to}`, err);
    }
  }
  return delivered;
}

function buildPdfEmail(title: string, intro: string) {
  const html = `<!doctype html>
<html>
  <body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;padding:24px;color:#111827;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px;border:1px solid #e5e7eb;">
      <tr><td>
        <p style="margin:0 0 4px;font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:#6b7280;">Ambeon Console</p>
        <h1 style="margin:0 0 14px;font-size:20px;">${escapeHtml(title)}</h1>
        <p style="margin:0 0 16px;color:#374151;font-size:14px;line-height:1.55;">${escapeHtml(intro)}</p>
        <p style="margin:0 0 16px;color:#374151;font-size:14px;">The full report — tables and charts — is attached as a PDF.</p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0 12px;" />
        <p style="margin:0;color:#9ca3af;font-size:11px;">This report was generated and sent automatically by the Ambeon Console.</p>
      </td></tr>
    </table>
  </body>
</html>`;
  const text = `${title}\n\n${intro}\n\nThe full report (tables and charts) is attached as a PDF.`;
  return { html, text };
}

/**
 * Send a report as a PDF attachment with a short covering note (no inline
 * Markdown body). Each recipient is delivered independently; returns the
 * addresses that succeeded.
 */
export async function sendReportPdfEmail(
  recipients: string | string[],
  subject: string,
  title: string,
  intro: string,
  pdf: Buffer,
  filename: string,
): Promise<string[]> {
  const list = (Array.isArray(recipients) ? recipients : [recipients])
    .map((e) => e.trim())
    .filter(Boolean);
  if (list.length === 0) return [];

  const { html, text } = buildPdfEmail(title, intro);
  const attachments: MailAttachment[] = [
    { filename, content: pdf, contentType: "application/pdf" },
  ];
  const delivered: string[] = [];

  for (const to of list) {
    try {
      const ok = await deliver(to, subject, text, html, attachments);
      if (ok) {
        delivered.push(to);
      } else {
        logger.warn(
          `[mailer] No email provider configured — cannot send PDF report to ${to}.`,
        );
      }
    } catch (err) {
      logger.error(`[mailer] Failed to send PDF report to ${to}`, err);
    }
  }
  return delivered;
}
