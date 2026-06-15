import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Shared OpenAI helper for generating one-shot Markdown reports.
 *
 * Two strategies are exposed:
 *   • generateReportWithWebSearch — uses the Responses API with the built-in
 *     `web_search_preview` tool so the model can pull fresh figures off the
 *     internet (used by Robin to fetch live EPS / P/E / P/B etc.).
 *   • generateReportFromChat — a plain Chat Completions call over context the
 *     caller already gathered (used by Tuck to summarise CSE data).
 *
 * generateReportWithWebSearch falls back to a plain chat completion when the
 * Responses API / web-search tool is unavailable for the configured key/model,
 * so the feature degrades gracefully instead of failing outright.
 * ────────────────────────────────────────────────────────────────────────── */

function requireKey(): string {
  if (!env.openai.apiKey) {
    throw HttpError.badRequest(
      "OPENAI_API_KEY is not configured on the server. Add it to backend/.env to enable report generation.",
    );
  }
  return env.openai.apiKey;
}

/**
 * Model used for the web-search-backed report. Must support the Responses API
 * `web_search` tool. Override with OPENAI_WEB_SEARCH_MODEL.
 */
const WEB_SEARCH_MODEL =
  process.env.OPENAI_WEB_SEARCH_MODEL?.trim() || "gpt-4o";

interface ResponsesOutputContent {
  type: string;
  text?: string;
}

interface ResponsesOutputItem {
  type: string;
  content?: ResponsesOutputContent[];
}

interface ResponsesApiResponse {
  output_text?: string;
  output?: ResponsesOutputItem[];
}

function extractResponsesText(data: ResponsesApiResponse): string {
  if (typeof data.output_text === "string" && data.output_text.trim()) {
    return data.output_text;
  }
  const parts: string[] = [];
  for (const item of data.output ?? []) {
    if (item.type !== "message") continue;
    for (const c of item.content ?? []) {
      if (c.type === "output_text" && typeof c.text === "string") {
        parts.push(c.text);
      }
    }
  }
  return parts.join("\n").trim();
}

/** True if the model actually performed a web search in this response. */
function didPerformSearch(data: ResponsesApiResponse): boolean {
  for (const item of data.output ?? []) {
    if (typeof item.type === "string" && item.type.includes("web_search")) {
      return true;
    }
  }
  return false;
}

interface ChatResponse {
  choices?: Array<{ message?: { content?: string | null } }>;
}

async function chatCompletion(
  instructions: string,
  input: string,
  model: string,
): Promise<string> {
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${requireKey()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      temperature: 0.3,
      messages: [
        { role: "system", content: instructions },
        { role: "user", content: input },
      ],
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new HttpError(
      res.status === 401 ? 500 : 502,
      `OpenAI request failed (${res.status})`,
      text || undefined,
    );
  }

  const data = (await res.json()) as ChatResponse;
  return data.choices?.[0]?.message?.content?.trim() ?? "";
}

/**
 * Generate a report, forcing the model to browse the web for fresh figures.
 *
 * Strategy (in order):
 *   1. GA `web_search` tool with `tool_choice: "required"` so a live search
 *      MUST run before the model answers.
 *   2. Legacy `web_search_preview` tool (auto) for older API tiers.
 *   3. Plain chat completion fallback (model knowledge only) so the feature
 *      degrades instead of failing.
 */
export async function generateReportWithWebSearch(args: {
  instructions: string;
  input: string;
}): Promise<{ report: string; usedWebSearch: boolean }> {
  const key = requireKey();

  const attempts: Array<{ label: string; body: Record<string, unknown> }> = [
    {
      label: "web_search (required)",
      body: {
        model: WEB_SEARCH_MODEL,
        tools: [{ type: "web_search" }],
        tool_choice: "required",
        instructions: args.instructions,
        input: args.input,
      },
    },
    {
      label: "web_search_preview (auto)",
      body: {
        model: WEB_SEARCH_MODEL,
        tools: [{ type: "web_search_preview" }],
        instructions: args.instructions,
        input: args.input,
      },
    },
  ];

  for (const attempt of attempts) {
    try {
      const res = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(attempt.body),
      });

      if (res.ok) {
        const data = (await res.json()) as ResponsesApiResponse;
        const report = extractResponsesText(data);
        const searched = didPerformSearch(data);
        if (report) {
          logger.info(
            `[Report] Generated via ${attempt.label} (searched=${searched}, model=${WEB_SEARCH_MODEL}).`,
          );
          return { report, usedWebSearch: searched };
        }
        logger.warn(
          `[Report] ${attempt.label} returned empty text; trying next strategy.`,
        );
      } else {
        const errorText = await res.text().catch(() => "");
        logger.warn(
          `[Report] ${attempt.label} failed (${res.status}): ${errorText.slice(0, 300)}`,
        );
      }
    } catch (err) {
      logger.warn(
        `[Report] ${attempt.label} error: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  logger.warn(
    "[Report] Falling back to chat completion (no live web search performed).",
  );
  const report = await chatCompletion(
    `${args.instructions}\n\nNote: live web search is unavailable, so use your best general knowledge and clearly flag any figure that may be out of date.`,
    args.input,
    env.openai.model,
  );
  return { report, usedWebSearch: false };
}

/** Generate a report purely from context the caller already collected. */
export async function generateReportFromChat(args: {
  instructions: string;
  input: string;
  model?: string;
}): Promise<string> {
  return chatCompletion(
    args.instructions,
    args.input,
    args.model ?? env.openai.model,
  );
}
