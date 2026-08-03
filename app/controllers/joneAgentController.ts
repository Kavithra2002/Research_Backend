import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import { runJoneChat } from "../services/joneChatService";
import {
  buildJoneReportCsvFromPayload,
  buildJoneReportPdfFromConfig,
  buildJoneReportPdfFromPayload,
} from "../services/jonePdfService";
import {
  MARKET_SUMMARY_COLUMNS,
  deleteJoneConfig,
  getJoneConfig,
  saveJoneConfig,
} from "../services/joneService";
import {
  countPresetSymbols,
  JONE_PRESET_GROUPS,
} from "../services/joneMarketGroups";
import { normalizeGroupKind } from "../services/joneMarketGroups";
import type { ChatMessage } from "../services/robinAgentService";

const chatMessageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
});

const chatInputSchema = z.object({
  messages: z.array(chatMessageSchema).min(1, "At least one message required"),
});

export async function joneChatHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = chatInputSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  const messages: ChatMessage[] = parsed.data.messages.map((m) => ({
    role: m.role,
    content: m.content,
  }));

  const result = await runJoneChat({
    messages,
    user: {
      first_name: user.first_name ?? "",
      last_name: user.last_name ?? "",
      email: user.email ?? "",
      user_id: user.user_id ?? "",
      role: String(user.role ?? "User"),
    },
  });

  res.json({
    reply: result.reply,
    tool_events: result.tool_events,
  });
}

export async function joneColumnsHandler(_req: Request, res: Response) {
  res.json({
    columns: MARKET_SUMMARY_COLUMNS.map((c) => ({ id: c.id, label: c.label })),
  });
}

export async function joneGetConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const config = await getJoneConfig(user.user_id ?? "");
  res.json({ config });
}

const configSchema = z.object({
  columns: z.array(z.string()).default([]),
  groupKind: z
    .enum(["watchlist", "top_gainers", "top_losers"])
    .default("watchlist"),
  watchlistId: z.string().default(""),
  watchlistName: z.string().default(""),
  watchlistSymbols: z.array(z.string()).default([]),
  enabled: z.boolean().default(true),
});

export async function joneSaveConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = configSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    const config = await saveJoneConfig(
      user.user_id ?? "",
      parsed.data.columns,
      normalizeGroupKind(parsed.data.groupKind),
      parsed.data.watchlistId,
      parsed.data.watchlistName,
      parsed.data.watchlistSymbols,
      parsed.data.enabled,
    );
    res.json({ config });
  } catch (err) {
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to save configuration",
    );
  }
}

export async function joneDeleteConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  await deleteJoneConfig(user.user_id ?? "");
  res.json({ ok: true });
}

const reportPayloadSchema = z.object({
  groupKind: z
    .enum(["watchlist", "top_gainers", "top_losers"])
    .optional(),
  watchlistId: z.string().optional(),
  watchlistName: z.string().optional(),
  watchlistSymbols: z.array(z.string()).optional(),
});

function payloadToConfig(body: z.infer<typeof reportPayloadSchema>) {
  if (!body.watchlistName || !body.watchlistId) return null;
  const kind = normalizeGroupKind(body.groupKind ?? body.watchlistId);
  return {
    columns: [] as string[],
    groupKind: kind,
    watchlistId: body.watchlistId,
    watchlistName: body.watchlistName,
    watchlistSymbols: body.watchlistSymbols ?? [],
    enabled: true,
  };
}

export async function joneGroupsHandler(_req: Request, res: Response) {
  const presets = await Promise.all(
    JONE_PRESET_GROUPS.map(async (g) => ({
      id: g.id,
      kind: g.kind,
      name: g.name,
      count: await countPresetSymbols(g.kind),
    })),
  );
  res.json({ presets });
}

export async function joneReportPdfHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = reportPayloadSchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    let pdf: Buffer;
    const body = parsed.data;
    const fromPayload = payloadToConfig(body);
    if (fromPayload) {
      pdf = await buildJoneReportPdfFromPayload(fromPayload);
    } else {
      const config = await getJoneConfig(user.user_id ?? "");
      if (!config) {
        throw HttpError.badRequest(
          "Configure John with a group or market preset first.",
        );
      }
      pdf = await buildJoneReportPdfFromConfig(config);
    }

    const date = new Date().toISOString().slice(0, 10);
    const name =
      body.watchlistName?.replace(/[^a-z0-9-_]+/gi, "_") ?? "trade_summary";
    res.setHeader("Content-Type", "application/pdf");
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${name}_trade_summary_${date}.pdf"`,
    );
    res.send(pdf);
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to build report PDF",
    );
  }
}

export async function joneReportCsvHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = reportPayloadSchema.safeParse(req.body ?? {});
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    let csv: string;
    const body = parsed.data;
    const fromPayload = payloadToConfig(body);
    if (fromPayload) {
      csv = await buildJoneReportCsvFromPayload(fromPayload);
    } else {
      const config = await getJoneConfig(user.user_id ?? "");
      if (!config) {
        throw HttpError.badRequest(
          "Configure John with a group or market preset first.",
        );
      }
      const { fetchWatchlistMarketSnapshot, buildTradeSummaryCsv } =
        await import("../services/joneService");
      const snapshot = await fetchWatchlistMarketSnapshot(config, {
        allColumns: true,
      });
      csv = buildTradeSummaryCsv(snapshot);
    }

    const date = new Date().toISOString().slice(0, 10);
    const name =
      body.watchlistName?.replace(/[^a-z0-9-_]+/gi, "_") ?? "trade_summary";
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${name}_trade_summary_${date}.csv"`,
    );
    res.send(csv);
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to build report CSV",
    );
  }
}
