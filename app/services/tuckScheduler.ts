import { runDailyReportsForAllUsers } from "./tuckService";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Tuck daily scheduler.
 *
 * A lightweight self-rescheduling timer (no extra dependency) that fires once a
 * day at a fixed local hour and generates a market summary report for every
 * user who has the Tuck agent enabled. Override the hour with TUCK_DAILY_HOUR.
 * ────────────────────────────────────────────────────────────────────────── */

let timer: NodeJS.Timeout | null = null;

function dailyHour(): number {
  const raw = process.env.TUCK_DAILY_HOUR;
  const n = raw ? Number(raw) : NaN;
  return Number.isInteger(n) && n >= 0 && n <= 23 ? n : 7; // default 07:00
}

function msUntilNextRun(): number {
  const now = new Date();
  const next = new Date(now);
  next.setHours(dailyHour(), 0, 0, 0);
  if (next.getTime() <= now.getTime()) {
    next.setDate(next.getDate() + 1);
  }
  return next.getTime() - now.getTime();
}

async function runOnce(): Promise<void> {
  try {
    const count = await runDailyReportsForAllUsers();
    if (count > 0) {
      logger.info(`[Tuck] Generated ${count} daily report(s).`);
    }
  } catch (err) {
    logger.error("[Tuck] Daily report run failed", err);
  }
}

function scheduleNext(): void {
  const delay = msUntilNextRun();
  timer = setTimeout(() => {
    void runOnce().finally(scheduleNext);
  }, delay);
  // Don't keep the event loop alive solely for this timer.
  timer.unref?.();
}

export function startTuckScheduler(): void {
  if (timer) return;
  scheduleNext();
  logger.info(`[Tuck] Daily scheduler armed (runs at ${dailyHour()}:00 local).`);
}

export function stopTuckScheduler(): void {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
}
