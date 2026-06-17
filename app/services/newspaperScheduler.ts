import { ensureNewspaperFresh } from "./newspaperService";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Newspaper RSS refresh scheduler — keeps the feed warm without user action.
 * ────────────────────────────────────────────────────────────────────────── */

const REFRESH_INTERVAL_MS =
  Number(process.env.NEWSPAPER_REFRESH_MINUTES ?? 60) * 60 * 1000 || 60 * 60 * 1000;

let timer: NodeJS.Timeout | null = null;

async function runOnce(): Promise<void> {
  try {
    await ensureNewspaperFresh();
  } catch (err) {
    logger.error("[Newspaper] Scheduled refresh failed", err);
  }
}

export function startNewspaperScheduler(): void {
  if (timer) return;
  void runOnce();
  timer = setInterval(() => void runOnce(), REFRESH_INTERVAL_MS);
  timer.unref?.();
  logger.info(
    `[Newspaper] Scheduler armed (refresh every ${Math.round(REFRESH_INTERVAL_MS / 60000)} min).`,
  );
}

export function stopNewspaperScheduler(): void {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}
