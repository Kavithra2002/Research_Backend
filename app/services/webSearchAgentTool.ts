import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import { logger } from "../utils/logger";

/* ────────────────────────────────────────────────────────────────────────── *
 * Web-search tool — shared by every data agent (Robin, Marian, Tuck, Sage).
 *
 * Uses the OpenAI Responses API with the built-in `web_search` tool so the
 * model can pull fresh information from the internet. Call this when domain
 * tools (CSE, database, report text) cannot answer, or for general current-
 * affairs / macro / commodity / forex questions outside our stored data.
 * ────────────────────────────────────────────────────────────────────────── */

/** System-prompt block — import into every agent's buildSystemPrompt(). */
export const WEB_SEARCH_GUIDANCE = [
  "Web search (MANDATORY fallback — use before saying you don't know):",
  "  • web_search queries the live internet for current facts, prices, news, macro data, and general knowledge NOT in our database or CSE feeds.",
  "  • ALWAYS try your domain-specific tools first (CSE live data, financial DB, non-financial DB, report text). If they cannot answer — or the question is clearly outside our data (e.g. crude oil / Brent / WTI prices, gold, USD/LKR, Fed rates, geopolitical news, global markets, commodity trends, definitions, current events) — call web_search immediately WITHOUT asking permission.",
  "  • NEVER reply with 'I don't have access', 'check external websites', or 'I can't provide real-time data' without calling web_search first.",
  "  • For company-related external-impact or explanatory questions, gather the company profile from the DB first, then call web_search with company_context.",
  "  • When a CSE ticker was resolved (e.g. SCAP = Softlogic Capital PLC), ALWAYS pass the full CSE company name + ticker in web_search queries — never search only the bare ticker.",
  "  • For general factual questions (commodity prices, forex, world news, macro trends), call web_search directly with a clear, specific query.",
  "  • Include source URLs as markdown links [title](url) when available. Add a short **From web sources:** label and note the timeframe (e.g. 'as of today').",
  "  • Do NOT use web_search when the answer is already in the database, CSE feed, or report text — those sources take priority.",
].join("\n");

const WEB_SEARCH_MODEL =
  process.env.OPENAI_WEB_SEARCH_MODEL?.trim() || "gpt-4o";

export const WEB_SEARCH_TOOL = {
  type: "function" as const,
  function: {
    name: "web_search",
    description:
      "Search the INTERNET for current, external, or general information NOT available from our database or CSE live feeds. MANDATORY fallback when domain tools cannot answer. Use for: commodity prices (crude oil/Brent/WTI, gold), forex rates, global macro news, Fed/OPEC/policy events, geopolitical developments, analyst views, buy recommendations (after screening), company-specific facts missing from reports, and any general factual question. Try database/CSE/report tools FIRST when relevant; then call this WITHOUT asking permission. Include markdown source links [title](url). Label web-sourced facts clearly.",
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description:
            "The search question — be specific (e.g. 'Brent crude oil price today June 2026', 'Dialog Axiata 2022 loss reason').",
        },
        company_context: {
          type: "string",
          description:
            "Optional: brief company context from the database (sector, business lines, geography) to tailor company-related searches.",
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
    "You are a research assistant helping a financial analyst at a Sri Lankan securities firm.",
    "Search the web for current, factual information relevant to the user's question.",
    "Be concise but thorough. Focus on facts, recent developments, and credible sources.",
    "For price/quote questions (crude oil, Brent, WTI, gold, forex, indices), lead with the latest figures and name the benchmark (e.g. Brent vs WTI).",
    "If the question is about how an external event affects a specific company, tailor your analysis to that company's sector, geography and business model using the company context provided.",
    "Structure your answer with short paragraphs and bullet points.",
    "When you find credible sources, include them as markdown links: [Source title](https://full-url) so the user can click and read.",
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
