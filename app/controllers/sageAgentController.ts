import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import { runSageChat, type ChatMessage } from "../services/sageAgentService";

const chatMessageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
});

const chatInputSchema = z.object({
  messages: z.array(chatMessageSchema).min(1, "At least one message required"),
});

export async function sageChatHandler(req: Request, res: Response) {
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

  const result = await runSageChat({
    messages,
    user: {
      first_name: user.first_name ?? "",
      last_name: user.last_name ?? "",
      email: user.email ?? "",
      user_id: user.user_id ?? "",
      role: String(user.role ?? "User"),
    },
    owner: {
      userObjectId: user._id ?? null,
      userBusinessId: user.user_id ?? null,
    },
  });

  res.json({
    reply: result.reply,
    tool_events: result.tool_events,
  });
}
