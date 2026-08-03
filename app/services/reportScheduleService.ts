import {
  ReportSchedule,
  type ReportAgent,
  type ReportFrequency,
  type ReportScheduleAttrs,
} from "../models/ReportSchedule";
import { generateRobinReport } from "./robinReportService";
import { generateTuckReport, saveTuckReport } from "./tuckService";
import {
  buildMarianDataset,
  normalizeSections,
} from "./marianMarketService";
import { buildDailyMarketWrapPdfBuffer } from "./marianPdfService";
import { buildJoneReportPdfFromPayload } from "./jonePdfService";
import { getUserByUserId } from "./userService";
import { sendReportEmail, sendReportPdfEmail } from "../utils/mailer";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Report schedules — persist when (time/date/frequency) and to whom (emails) a
 * Robin/Tuck report should be generated and emailed. A lightweight scheduler
 * (reportScheduleScheduler) ticks periodically and runs the ones that are due.
 * ────────────────────────────────────────────────────────────────────────── */

const FREQUENCIES = new Set<ReportFrequency>([
  "once",
  "daily",
  "weekly",
  "monthly",
]);

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export interface SaveSchedulePayload {
  time: string;
  date: string;
  frequency: ReportFrequency;
  emails: string[];
  config: Record<string, unknown>;
  enabled?: boolean;
}

function normalizeTime(time: unknown): string {
  const value = String(time ?? "").trim();
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value) ? value : "09:00";
}

function normalizeDate(date: unknown): string {
  const value = String(date ?? "").trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "";
}

function normalizeFrequency(freq: unknown): ReportFrequency {
  const value = String(freq ?? "") as ReportFrequency;
  return FREQUENCIES.has(value) ? value : "once";
}

function normalizeEmails(emails: unknown): string[] {
  if (!Array.isArray(emails)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of emails) {
    const email = String(raw ?? "").trim().toLowerCase();
    if (EMAIL_RE.test(email) && !seen.has(email)) {
      seen.add(email);
      out.push(email);
    }
  }
  return out;
}

function toPublic(doc: ReportScheduleAttrs) {
  return {
    agent: doc.agent,
    time: doc.time,
    date: doc.date,
    frequency: doc.frequency,
    emails: doc.emails ?? [],
    config: doc.config ?? {},
    enabled: doc.enabled ?? true,
    lastRunAt: doc.lastRunAt ?? null,
  };
}

export async function getReportSchedule(userId: string, agent: ReportAgent) {
  const doc = await ReportSchedule.findOne({ user_id: userId, agent }).lean();
  return doc ? toPublic(doc) : null;
}

export async function saveReportSchedule(
  userId: string,
  agent: ReportAgent,
  payload: SaveSchedulePayload,
) {
  const existing = await ReportSchedule.findOne({ user_id: userId, agent }).lean();

  const time = normalizeTime(payload.time);
  const date = normalizeDate(payload.date);
  const frequency = normalizeFrequency(payload.frequency);

  // A schedule's *timing* (time/date/frequency) defines a distinct occurrence.
  // Only re-arm (clear the last-run marker) when that timing actually changes,
  // so the report fires for the newly chosen time only. Re-saving the same
  // timing — e.g. toggling the agent Run/Stop switch, or changing recipients —
  // must NOT resend a report that already went out for a previously configured
  // time. The single stored doc per (user, agent) means setting a new time also
  // replaces the old one, so no report is ever produced at a previous time.
  const timingChanged =
    !existing ||
    existing.time !== time ||
    existing.date !== date ||
    existing.frequency !== frequency;

  const update: Record<string, unknown> = {
    $set: {
      time,
      date,
      frequency,
      emails: normalizeEmails(payload.emails),
      config: payload.config ?? {},
      enabled: payload.enabled ?? true,
    },
  };
  if (timingChanged) {
    update.$unset = { lastRunAt: "" };
  }

  const doc = await ReportSchedule.findOneAndUpdate(
    { user_id: userId, agent },
    update,
    { new: true, upsert: true },
  ).lean();
  return toPublic(doc);
}

export async function deleteReportSchedule(userId: string, agent: ReportAgent) {
  await ReportSchedule.deleteOne({ user_id: userId, agent });
}

/* ── Due-time computation ───────────────────────────────────────────────── */

function parseHM(time: string): { h: number; m: number } {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time);
  if (!match) return { h: 9, m: 0 };
  return { h: Number(match[1]), m: Number(match[2]) };
}

function parseLocalDate(date: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function daysInMonth(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
}

/**
 * The scheduled datetime for the current run window, or null if the schedule is
 * not due right now. For recurring schedules this is "today's" slot (once the
 * day constraint and start date are satisfied and the time has passed).
 */
function occurrenceFor(doc: ReportScheduleAttrs, now: Date): Date | null {
  const { h, m } = parseHM(doc.time);

  if (doc.frequency === "once") {
    // No date set → treat as "today" so a quick "send now" still fires.
    const day = parseLocalDate(doc.date) ?? new Date(now);
    const dt = new Date(day);
    dt.setHours(h, m, 0, 0);
    return now.getTime() >= dt.getTime() ? dt : null;
  }

  const slot = new Date(now);
  slot.setHours(h, m, 0, 0);
  if (now.getTime() < slot.getTime()) return null; // today's time not reached yet

  const start = parseLocalDate(doc.date);
  if (start) {
    const startMidnight = new Date(start);
    startMidnight.setHours(0, 0, 0, 0);
    if (slot.getTime() < startMidnight.getTime()) return null; // before start date
  }

  if (doc.frequency === "weekly") {
    const target = start ? start.getDay() : now.getDay();
    if (now.getDay() !== target) return null;
  }

  if (doc.frequency === "monthly") {
    const target = start ? start.getDate() : now.getDate();
    const effective = Math.min(target, daysInMonth(now));
    if (now.getDate() !== effective) return null;
  }

  return slot;
}

function isDue(doc: ReportScheduleAttrs, now: Date): Date | null {
  if (!doc.enabled) return null;
  const occ = occurrenceFor(doc, now);
  if (!occ) return null;
  const last = doc.lastRunAt ? new Date(doc.lastRunAt).getTime() : 0;
  return last < occ.getTime() ? occ : null;
}

/* ── Report generation per agent ────────────────────────────────────────── */

type ScheduleOutput =
  | { kind: "markdown"; report: string; subject: string; title: string }
  | {
      kind: "pdf";
      pdf: Buffer;
      filename: string;
      subject: string;
      title: string;
      intro: string;
    };

async function generateForSchedule(
  doc: ReportScheduleAttrs,
): Promise<ScheduleOutput | null> {
  const config = (doc.config ?? {}) as Record<string, unknown>;

  if (doc.agent === "robin") {
    const companies = Array.isArray(config.companies)
      ? (config.companies as unknown[]).map(String)
      : [];
    const metrics = Array.isArray(config.metrics)
      ? (config.metrics as unknown[]).map(String)
      : [];
    if (companies.length === 0 || metrics.length === 0) return null;

    let user = { first_name: "", last_name: "", email: "" };
    try {
      const u = await getUserByUserId(doc.user_id);
      user = { first_name: u.first_name, last_name: u.last_name, email: u.email };
    } catch {
      /* fall back to empty user info */
    }

    const { report } = await generateRobinReport({ companies, metrics, user });
    return {
      kind: "markdown",
      report,
      subject: "Robin — Financial Summary Report",
      title: "Robin — Financial Summary",
    };
  }

  if (doc.agent === "marian") {
    const sections = normalizeSections(
      Array.isArray(config.sections) ? (config.sections as unknown[]).map(String) : [],
    );
    if (sections.length === 0) return null;

    // Build the live dataset and render the Daily Market Wrap as a PDF — the
    // email carries the PDF attachment (with tables + charts), not a digital
    // Markdown body.
    const dataset = await buildMarianDataset(sections);
    const pdf = buildDailyMarketWrapPdfBuffer(dataset);
    const date = new Date().toISOString().slice(0, 10);
    return {
      kind: "pdf",
      pdf,
      filename: `daily-market-wrap-${date}.pdf`,
      subject: "Marian — Daily Market Wrap",
      title: "Daily Market Wrap",
      intro: "Please find today's Daily Market Wrap from Ambeon Securities, generated from live Colombo Stock Exchange data.",
    };
  }

  if (doc.agent === "jone") {
    const groupKind = String(config.groupKind ?? config.watchlistId ?? "watchlist");
    const watchlistName = String(config.watchlistName ?? "");
    const watchlistId = String(config.watchlistId ?? "");
    const watchlistSymbols = Array.isArray(config.watchlistSymbols)
      ? (config.watchlistSymbols as unknown[]).map(String)
      : [];
    if (!watchlistName || !watchlistId) {
      return null;
    }
    if (
      groupKind === "watchlist" &&
      watchlistSymbols.length === 0
    ) {
      return null;
    }

    const pdf = await buildJoneReportPdfFromPayload({
      groupKind:
        groupKind === "top_gainers" || groupKind === "top_losers"
          ? groupKind
          : "watchlist",
      watchlistId,
      watchlistName,
      watchlistSymbols,
    });
    const date = new Date().toISOString().slice(0, 10);
    const safeName = watchlistName.replace(/[^a-z0-9-_]+/gi, "_");
    return {
      kind: "pdf",
      pdf,
      filename: `${safeName}_market_summary_${date}.pdf`,
      subject: `John — ${watchlistName} Market Summary`,
      title: `${watchlistName} Market Summary`,
      intro: `Please find the live market summary for your My List group "${watchlistName}", generated from Colombo Stock Exchange data.`,
    };
  }

  // tuck
  const sections = Array.isArray(config.sections)
    ? (config.sections as unknown[]).map(String)
    : [];
  const companies = Array.isArray(config.companies)
    ? (config.companies as unknown[]).map(String)
    : [];
  if (sections.length === 0) return null;

  const { report, sections: used } = await generateTuckReport(sections, companies);
  if (report.trim()) {
    try {
      await saveTuckReport(doc.user_id, report, used, "scheduled");
    } catch {
      /* non-fatal */
    }
  }
  return {
    kind: "markdown",
    report,
    subject: "Tuck — Daily Market Summary",
    title: "Tuck — Daily Market Summary",
  };
}

async function runSchedule(doc: ReportScheduleAttrs, occurrence: Date) {
  const result = await generateForSchedule(doc);
  const empty =
    !result ||
    (result.kind === "markdown" && !result.report.trim()) ||
    (result.kind === "pdf" && result.pdf.length === 0);
  if (!result || empty) {
    logger.warn(
      `[Schedule] ${doc.agent} report for ${doc.user_id} produced no content; skipping.`,
    );
    return;
  }

  if (doc.emails.length > 0) {
    const delivered =
      result.kind === "pdf"
        ? await sendReportPdfEmail(
            doc.emails,
            result.subject,
            result.title,
            result.intro,
            result.pdf,
            result.filename,
          )
        : await sendReportEmail(
            doc.emails,
            result.subject,
            result.report,
            result.title,
          );
    logger.info(
      `[Schedule] ${doc.agent} report emailed to ${delivered.length}/${doc.emails.length} recipient(s) for ${doc.user_id}.`,
    );
  } else {
    logger.info(
      `[Schedule] ${doc.agent} report generated for ${doc.user_id} (no recipient emails configured).`,
    );
  }

  const update: Record<string, unknown> = { lastRunAt: occurrence };
  if (doc.frequency === "once") update.enabled = false;
  await ReportSchedule.updateOne(
    { user_id: doc.user_id, agent: doc.agent },
    { $set: update },
  );
}

/**
 * Find every enabled schedule that is due at `now` and run it. Returns the
 * number of reports generated. Safe to call frequently — gating on lastRunAt
 * prevents an occurrence from running twice.
 */
export async function runDueSchedules(now: Date = new Date()): Promise<number> {
  const docs = await ReportSchedule.find({ enabled: true }).lean();
  let ran = 0;
  for (const doc of docs) {
    const occurrence = isDue(doc, now);
    if (!occurrence) continue;
    try {
      await runSchedule(doc, occurrence);
      ran += 1;
    } catch (err) {
      logger.error(
        `[Schedule] Failed running ${doc.agent} schedule for ${doc.user_id}`,
        err,
      );
    }
  }
  return ran;
}
