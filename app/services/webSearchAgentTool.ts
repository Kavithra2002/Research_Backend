import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Web-search tool — shared by every data agent (Robin, Tuck, …).
 *
 * Uses the OpenAI Responses API with the built-in `web_search` tool so the
 * model can pull fresh information from the internet. This is for questions
 * that CANNOT be answered from the stored database alone — e.g. how a global
 * event (war, pandemic, policy change) might affect a company, current market
 * news, macroeconomic trends, competitor moves, etc.
 *
 * The agent should ALWAYS try the database tools first; only call this when
 * external / current / forward-looking context is genuinely needed.
 * ────────────────────────────────────────────────────────────────────────── */

const WEB_SEARCH_MODEL =
  process.env.OPENAI_WEB_SEARCH_MODEL?.trim() || "gpt-4o";

export const WEB_SEARCH_TOOL = {
  type: "function" as const,
  function: {
    name: "web_search",
    description:
      "Search the INTERNET for current, external, or forward-looking information that is NOT in the company's stored reports or database. Use this when the user asks how external events (wars, geopolitical crises, pandemics, policy/regulatory changes, global economic trends, commodity prices, competitor news) might affect a company, OR when you need up-to-date facts that stored data cannot provide. ALWAYS fetch the company's own profile from the database first (non_financial_overview / get_non_financial_metric / financial tools) so you know its sector, geography and business model, THEN call this tool with that context so the analysis is specific to the company. Clearly label which parts of your answer come from web search vs stored data.",
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description:
            "The search question — be specific and include the company name, sector, and what external factor you are researching.",
        },
        company_context: {
          type: "string",
          description:
            "Brief context about the company gathered from the database (sector, business lines, geography, key exposures) so the web search analysis is tailored.",
        },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
} as const;

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

async function chatFallback(
  instructions: string,
  input: string,
): Promise<string> {
  if (!env.openai.apiKey) {
    throw HttpError.badRequest("OPENAI_API_KEY is not configured.");
  }
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.openai.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: env.openai.model,
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
 * Run a web search via OpenAI and return a concise, analysis-ready answer.
 */
export async function runWebSearch(args: {
  query: string;
  company_context?: string;
}): Promise<unknown> {
  const query = (args.query || "").trim();
  if (!query) return { error: "query is required for web_search" };

  if (!env.openai.apiKey) {
    return {
      error:
        "OPENAI_API_KEY is not configured on the server. Web search is unavailable.",
    };
  }

  const instructions = [
    "You are a research assistant helping a financial analyst.",
    "Search the web for current, factual information relevant to the user's question.",
    "Be concise but thorough. Focus on facts, recent developments, and credible sources.",
    "If the question is about how an external event affects a specific company, tailor your analysis to that company's sector, geography and business model using the company context provided.",
    "Structure your answer with short paragraphs and bullet points.",
    "At the end, note the general timeframe of the information (e.g. 'as of early 2026').",
    "If you cannot find reliable information, say so honestly.",
  ].join(" ");

  let input = query;
  if (args.company_context?.trim()) {
    input = `Company context (from our database):\n${args.company_context.trim()}\n\nResearch question:\n${query}`;
  }

  const attempts: Array<{ label: string; body: Record<string, unknown> }> = [
    {
      label: "web_search (required)",
      body: {
        model: WEB_SEARCH_MODEL,
        tools: [{ type: "web_search" }],
        tool_choice: "required",
        instructions,
        input,
      },
    },
    {
      label: "web_search_preview (auto)",
      body: {
        model: WEB_SEARCH_MODEL,
        tools: [{ type: "web_search_preview" }],
        instructions,
        input,
      },
    },
  ];

  for (const attempt of attempts) {
    try {
      const res = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.openai.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(attempt.body),
      });

      if (res.ok) {
        const data = (await res.json()) as ResponsesApiResponse;
        const answer = extractResponsesText(data);
        const searched = didPerformSearch(data);
        if (answer) {
          logger.info(
            `[WebSearch] Answered via ${attempt.label} (searched=${searched}).`,
          );
          return {
            query,
            answer,
            source: searched ? "web_search" : "model_knowledge",
            note: searched
              ? "This answer was gathered from a live web search. Clearly tell the user which parts come from web research vs stored database data."
              : "Live web search was unavailable; this answer is based on the model's general knowledge and may be out of date. Flag any uncertainty.",
          };
        }
      } else {
        const errorText = await res.text().catch(() => "");
        logger.warn(
          `[WebSearch] ${attempt.label} failed (${res.status}): ${errorText.slice(0, 300)}`,
        );
      }
    } catch (err) {
      logger.warn(
        `[WebSearch] ${attempt.label} error: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  // Graceful fallback — model knowledge only.
  logger.warn("[WebSearch] Falling back to chat completion (no live search).");
  try {
    const answer = await chatFallback(
      `${instructions}\n\nNote: live web search is unavailable, so use your best general knowledge and clearly flag any figure that may be out of date.`,
      input,
    );
    return {
      query,
      answer,
      source: "model_knowledge_fallback",
      note:
        "Live web search was unavailable. This answer is based on the model's general knowledge — clearly flag that it may not reflect the very latest developments.",
    };
  } catch (err) {
    return {
      error:
        err instanceof Error ? err.message : "Web search failed unexpectedly.",
    };
  }
}

/** Dispatch the web-search tool by name. */
export async function dispatchWebSearchTool(
  name: string,
  args: unknown,
): Promise<unknown | undefined> {
  if (name !== "web_search") return undefined;
  const a = (args ?? {}) as Record<string, unknown>;
  return runWebSearch({
    query: String(a.query ?? ""),
    company_context:
      typeof a.company_context === "string" ? a.company_context : undefined,
  });
}
