import { runDueSchedules } from "./reportScheduleService";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Report-schedule scheduler.
 *
 * A lightweight polling timer (no extra dependency) that wakes up every
 * TICK_MS, checks for any user report schedule (Robin/Tuck) that is due, and
 * generates + emails the report. Override the cadence with
 * REPORT_SCHEDULE_TICK_SECONDS (default 30s) — small so a report configured for
 * "now" fires promptly.
 * ────────────────────────────────────────────────────────────────────────── */

let timer: NodeJS.Timeout | null = null;
let running = false;

function tickMs(): number {
  const raw = process.env.REPORT_SCHEDULE_TICK_SECONDS;
  const n = raw ? Number(raw) : NaN;
  const seconds = Number.isFinite(n) && n >= 5 ? n : 30;
  return seconds * 1000;
}

async function tick(): Promise<void> {
  if (running) return; // never overlap runs
  running = true;
  try {
    const ran = await runDueSchedules();
    if (ran > 0) {
      logger.info(`[Schedule] Ran ${ran} due report schedule(s).`);
    }
  } catch (err) {
    logger.error("[Schedule] Tick failed", err);
  } finally {
    running = false;
  }
}

export function startReportScheduleScheduler(): void {
  if (timer) return;
  timer = setInterval(() => void tick(), tickMs());
  timer.unref?.();
  logger.info(
    `[Schedule] Report scheduler armed (checking every ${tickMs() / 1000}s).`,
  );
  // Run once shortly after boot so a just-saved "now" schedule isn't delayed a
  // full tick on a fresh start.
  setTimeout(() => void tick(), 3000).unref?.();
}

export function stopReportScheduleScheduler(): void {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}
