import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  TUCK_SECTIONS,
  deleteTuckConfig,
  generateTuckReport,
  getTuckConfig,
  listTuckReports,
  saveTuckConfig,
  saveTuckReport,
} from "../services/tuckService";
import { runTuckChat } from "../services/tuckChatService";
import type { ChatMessage } from "../services/robinAgentService";

const chatMessageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
});

const chatInputSchema = z.object({
  messages: z.array(chatMessageSchema).min(1, "At least one message required"),
});

export async function tuckChatHandler(req: Request, res: Response) {
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

  const result = await runTuckChat({
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

export async function tuckSectionsHandler(_req: Request, res: Response) {
  res.json({
    sections: TUCK_SECTIONS.map((s) => ({ id: s.id, label: s.label })),
  });
}

export async function tuckGetConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const config = await getTuckConfig(user.user_id ?? "");
  res.json({ config });
}

const configSchema = z.object({
  sections: z.array(z.string()).default([]),
  companies: z.array(z.string()).default([]),
  enabled: z.boolean().default(true),
});

export async function tuckSaveConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = configSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  if (parsed.data.sections.length === 0) {
    throw HttpError.badRequest("Select at least one market data section.");
  }

  const config = await saveTuckConfig(
    user.user_id ?? "",
    parsed.data.sections,
    parsed.data.companies,
    parsed.data.enabled,
  );
  res.json({ config });
}

export async function tuckDeleteConfigHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  await deleteTuckConfig(user.user_id ?? "");
  res.json({ ok: true });
}

const reportSchema = z.object({
  sections: z.array(z.string()).min(1, "Select at least one section"),
  companies: z.array(z.string()).optional(),
  save: z.boolean().optional(),
});

export async function tuckReportHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = reportSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    const { report, sections } = await generateTuckReport(
      parsed.data.sections,
      parsed.data.companies ?? [],
    );
    if (parsed.data.save && report.trim()) {
      await saveTuckReport(user.user_id ?? "", report, sections, "manual");
    }
    res.json({ report, sections });
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to generate report",
    );
  }
}

export async function tuckReportsListHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const reports = await listTuckReports(user.user_id ?? "");
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
