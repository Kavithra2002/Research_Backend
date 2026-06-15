import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import { runRobinChat, type ChatMessage } from "../services/robinAgentService";
import {
  generateRobinReport,
  listDemoCompanies,
  ROBIN_METRICS,
} from "../services/robinReportService";

const chatMessageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
});

const chatInputSchema = z.object({
  messages: z.array(chatMessageSchema).min(1, "At least one message required"),
});

export async function robinChatHandler(req: Request, res: Response) {
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

  const result = await runRobinChat({
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

export async function robinCompaniesHandler(_req: Request, res: Response) {
  const companies = await listDemoCompanies();
  res.json({ companies, metrics: ROBIN_METRICS });
}

const reportInputSchema = z.object({
  companies: z.array(z.string()).min(1, "Select at least one company"),
  metrics: z.array(z.string()).min(1, "Select at least one metric"),
});

export async function robinReportHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");

  const parsed = reportInputSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  try {
    const result = await generateRobinReport({
      companies: parsed.data.companies,
      metrics: parsed.data.metrics,
      user: {
        first_name: user.first_name ?? "",
        last_name: user.last_name ?? "",
        email: user.email ?? "",
      },
    });
    res.json(result);
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw HttpError.badRequest(
      err instanceof Error ? err.message : "Failed to generate report",
    );
  }
}
