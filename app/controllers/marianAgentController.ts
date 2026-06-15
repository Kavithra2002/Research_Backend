import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  MARIAN_SECTIONS,
  buildMarianDataset,
  deleteMarianConfig,
  generateMarianReport,
  getMarianConfig,
  listMarianReports,
  saveMarianConfig,
  saveMarianReport,
} from "../services/marianMarketService";
import { buildDailyMarketWrapPdfBuffer } from "../services/marianPdfService";
import { runMarianChat } from "../services/marianChatService";
import type { ChatMessage } from "../services/robinAgentService";

const chatMessageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
});

const chatInputSchema = z.object({
  messages: z.array(chatMessageSchema).min(1, "At least one message required"),
});

export async function marianChatHandler(req: Request, res: Response) {
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

  const result = await runMarianChat({
    messages,
    user: {
      first_name: user.first_name ?? "",
      last_name: user.last_name ?? "",
      email: user.email ?? "",
      user_id: user.user_id ?? "",
      role: String(user.role ?? "User"),
    },
  });

  res.json({ reply: result.reply, tool_events: result.tool_events });
}

export async function marianSectionsHandler(_req: Request, res: Response) {
  res.json({
    sections: MARIAN_SECTIONS.map((s) => ({
      id: s.id,
      label: s.label,
      kind: s.kind,
      coverage: s.coverage,
    })),
  });
}

export async function marianGetConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const config = await getMarianConfig(user.user_id ?? "");
  res.json({ config });
}

const configSchema = z.object({
  sections: z.array(z.string()).default([]),
  enabled: z.boolean().default(true),
});

export async function marianSaveConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = configSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  if (parsed.data.sections.length === 0) {
    throw HttpError.badRequest("Select at least one report component.");
  }

  const config = await saveMarianConfig(
    user.user_id ?? "",
    parsed.data.sections,
    parsed.data.enabled,
  );
  res.json({ config });
}

export async function marianDeleteConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  await deleteMarianConfig(user.user_id ?? "");
  res.json({ ok: true });
}

const reportSchema = z.object({
  sections: z.array(z.string()).min(1, "Select at least one component"),
  save: z.boolean().optional(),
});

export async function marianReportHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = reportSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    const { report, sections, unavailable } = await generateMarianReport(
      parsed.data.sections,
    );
    if (parsed.data.save && report.trim()) {
      await saveMarianReport(user.user_id ?? "", report, sections, "manual");
    }
    res.json({ report, sections, unavailable });
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to generate report",
    );
  }
}

const dataSchema = z.object({
  sections: z.array(z.string()).min(1, "Select at least one component"),
});

/**
 * Return the structured market dataset (no LLM) the client uses to render the
 * deterministic Daily Market Wrap replica PDF.
 */
export async function marianDataHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = dataSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    const dataset = await buildMarianDataset(parsed.data.sections);
    res.json(dataset);
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to fetch market data",
    );
  }
}

/**
 * Build the Daily Market Wrap PDF on the server and stream it back so the
 * browser download and the scheduled-email attachment share one code path.
 */
export async function marianReportPdfHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = dataSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    const dataset = await buildMarianDataset(parsed.data.sections);
    const pdf = buildDailyMarketWrapPdfBuffer(dataset);
    const date = new Date().toISOString().slice(0, 10);
    res.setHeader("Content-Type", "application/pdf");
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="daily-market-wrap-${date}.pdf"`,
    );
    res.send(pdf);
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to build report PDF",
    );
  }
}

export async function marianReportsListHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const reports = await listMarianReports(user.user_id ?? "");
  res.json({
    reports: reports.map((r) => ({
      id: String(r._id),
      content: r.content,
      sections: r.sections ?? [],
      source: r.source ?? "manual",
      created_at:
        (r as { createdAt?: Date }).createdAt?.toISOString() ??
        new Date().toISOString(),
    })),
  });
}
