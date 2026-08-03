import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import {
  FINANCIAL_DB_TOOLS,
  dispatchFinancialTool,
  type ChatMessage,
  type ToolCall,
  type ToolEvent,
} from "./robinAgentService";
import {
  COMMON_TOOLS,
  currentDateContext,
  dispatchCommonTool,
} from "./agentTools";
import {
  NON_FINANCIAL_DB_TOOLS,
  dispatchNonFinancialTool,
} from "./nonFinancialAgentTools";
import {
  WEB_SEARCH_TOOL,
  WEB_SEARCH_GUIDANCE,
  dispatchWebSearchTool,
} from "./webSearchAgentTool";
import {
  CSE_AGENT_TOOLS,
  CSE_MARKET_QUERY_GUIDANCE,
  dispatchCseTool,
} from "./cseAgentTools";
import { RESPONSE_STYLE_GUIDE } from "./responseStyle";
import { SECTOR_QUERY_GUIDANCE } from "./sectorAgentTools";
import { BUY_RECOMMENDATION_GUIDANCE } from "./investmentAgentTools";
import { runCseChatPreflight } from "./cseSharePriceGuard";
import {
  fetchWatchlistMarketSnapshot,
  getJoneConfig,
  MARKET_SUMMARY_COLUMNS,
  type JoneConfigPublic,
} from "./joneService";

/* ────────────────────────────────────────────────────────────────────────── *
 * Jone — Analytics / market-summary agent for My List watchlists.
 * ────────────────────────────────────────────────────────────────────────── */

export interface JoneChatInput {
  messages: ChatMessage[];
  user: {
    first_name: string;
    last_name: string;
    email: string;
    user_id: string;
    role: string;
  };
}

export interface JoneChatOutput {
  reply: string;
  messages: ChatMessage[];
  tool_events: ToolEvent[];
}

const MAX_TOOL_ROUNDS = 8;

const TOOLS = [
  ...CSE_AGENT_TOOLS,
  ...FINANCIAL_DB_TOOLS,
  ...NON_FINANCIAL_DB_TOOLS,
  WEB_SEARCH_TOOL,
  ...COMMON_TOOLS,
] as const;

async function dispatchTool(name: string, args: unknown): Promise<unknown> {
  const a = (args ?? {}) as Record<string, unknown>;

  const cse = await dispatchCseTool(name, a);
  if (cse !== undefined) return cse;

  const financial = await dispatchFinancialTool(name, a);
  if (financial !== undefined) return financial;

  const nonFinancial = await dispatchNonFinancialTool(name, a);
  if (nonFinancial !== undefined) return nonFinancial;

  const web = await dispatchWebSearchTool(name, a);
  if (web !== undefined) return web;

  const common = dispatchCommonTool(name, a);
  if (common !== undefined) return common;

  throw new Error(`Unknown tool: ${name}`);
}

function formatConfigBlock(config: JoneConfigPublic | null): string {
  if (!config || !config.enabled) {
    return [
      "",
      "USER CONFIGURATION:",
      "  The user has not finished configuring John yet. If they ask about their watchlist or market summary table, remind them to open Configuration → John and pick columns plus a group (My List, Top Gainers, or Top Losers).",
    ].join("\n");
  }

  const columnLabels = config.columns
    .map((id) => MARKET_SUMMARY_COLUMNS.find((c) => c.id === id)?.label ?? id)
    .join(", ");

  const groupLabel =
    config.groupKind === "top_gainers"
      ? "Top Gainers (live CSE list)"
      : config.groupKind === "top_losers"
        ? "Top Losers (live CSE list)"
        : `My List group: "${config.watchlistName}"`;

  return [
    "",
    "USER CONFIGURATION (authoritative — use for watchlist / market-summary questions):",
    `  • Selected group: ${groupLabel}`,
    config.groupKind === "watchlist"
      ? `  • Symbols: ${config.watchlistSymbols.join(", ")}`
      : "  • Symbols are resolved live from today's CSE top gainers/losers list.",
    `  • Trade summary columns to emphasise in tables: ${columnLabels}`,
    "  • When the user asks about their list, today's prices, movers, or a market summary for their picks, prefer the PRE-FETCHED GROUP LIVE DATA below and format results using their selected columns.",
    "  • You may still use get_cse_share_price / get_cse_market_data for companies outside the list or broader market questions.",
  ].join("\n");
}

async function buildConfigAppendix(
  userId: string,
): Promise<{ configBlock: string; prefetchBlock: string }> {
  const config = await getJoneConfig(userId);
  const configBlock = formatConfigBlock(config);

  if (!config?.enabled) {
    return { configBlock, prefetchBlock: "" };
  }

  try {
    const snapshot = await fetchWatchlistMarketSnapshot(config);
    const prefetchBlock = [
      "",
      "PRE-FETCHED GROUP LIVE DATA (CSE tradeSummary — use these figures; do not invent):",
      JSON.stringify(snapshot, null, 2),
    ].join("\n");
    return { configBlock, prefetchBlock };
  } catch {
    return { configBlock, prefetchBlock: "" };
  }
}

function buildSystemPrompt(
  user: JoneChatInput["user"],
  configAppendix: string,
): string {
  const displayName =
    [user.first_name, user.last_name].filter(Boolean).join(" ") || user.email;
  return [
    `You are "John", an analytics assistant inside the Ambeon Console.`,
    `The signed-in user is ${displayName} (role: ${user.role}).`,
    `Your specialty is the Analytics market summary for the user's **My List** watchlist — live CSE prices and the column set they configured (price, change, volume, turnover, market cap, etc.).`,
    `You can also answer broader market questions and company financial / non-financial questions like Robin and Tuck.`,
    currentDateContext(),
    configAppendix,
    "",
    "Understand the user first:",
    "  • Work out what the user actually wants, even if their wording is short, informal, or has typos.",
    "  • If the request is genuinely ambiguous, ask ONE short clarifying question instead of guessing.",
    "",
    "Choosing the right tools:",
    CSE_MARKET_QUERY_GUIDANCE,
    "  • For the user's configured My List → use PRE-FETCHED WATCHLIST LIVE DATA when it covers the question; refresh with get_cse_share_price for a specific ticker if needed.",
    "  • For reported FINANCIAL figures → database tools (list_companies, company_overview, search_line_items, get_statement).",
    "  • For NON-FINANCIAL data → list_non_financial_companies, non_financial_overview, get_non_financial_metric.",
    SECTOR_QUERY_GUIDANCE,
    BUY_RECOMMENDATION_GUIDANCE,
    WEB_SEARCH_GUIDANCE,
    "",
    "Grounding (IMPORTANT):",
    "  • Never fabricate market figures. Use pre-fetched data or CSE tools first.",
    "  • When showing the user's watchlist, format as a Markdown table with their configured columns.",
    "",
    "Formatting:",
    "  • Prefer short paragraphs and bullet points. Use **bold** for key figures.",
    "  • Do NOT use Markdown heading marks (#). Use **bold** labels instead.",
    "  • Tabular answers → GitHub-style Markdown tables.",
    "",
    RESPONSE_STYLE_GUIDE,
    "",
    "Greet the user by their first name on the very first turn. Keep replies clear and concise.",
  ].join("\n");
}

interface OpenAIChoiceMessage {
  role: "assistant";
  content: string | null;
  tool_calls?: ToolCall[];
}

interface OpenAIResponse {
  choices: Array<{ message: OpenAIChoiceMessage; finish_reason: string }>;
}

async function callOpenAI(messages: ChatMessage[]): Promise<OpenAIChoiceMessage> {
  if (!env.openai.apiKey) {
    throw HttpError.badRequest(
      "OPENAI_API_KEY is not configured on the server. Add it to backend/.env to enable John.",
    );
  }
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.openai.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: env.openai.model,
      temperature: 0.2,
      messages,
      tools: TOOLS,
      tool_choice: "auto",
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

  const data = (await res.json()) as OpenAIResponse;
  const choice = data.choices?.[0];
  if (!choice) throw HttpError.internal("OpenAI returned no choices");
  return choice.message;
}

export async function runJoneChat(
  input: JoneChatInput,
): Promise<JoneChatOutput> {
  const { configBlock, prefetchBlock } = await buildConfigAppendix(
    input.user.user_id,
  );
  const preflight = await runCseChatPreflight(input.messages);
  const systemPrompt =
    buildSystemPrompt(input.user, configBlock + prefetchBlock) +
    (preflight?.systemAppendix ?? "");

  const history: ChatMessage[] = [
    { role: "system", content: systemPrompt },
    ...input.messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role, content: m.content ?? "" })),
  ];

  const events: ToolEvent[] = [
    ...(preflight?.toolEvents.map((e) => ({
      tool: e.tool,
      arguments: e.arguments,
      result: e.result,
      ok: e.ok,
    })) ?? []),
  ];

  for (let round = 0; round < MAX_TOOL_ROUNDS; round += 1) {
    const assistant = await callOpenAI(history);

    history.push({
      role: "assistant",
      content: assistant.content ?? null,
      tool_calls: assistant.tool_calls,
    });

    if (!assistant.tool_calls || assistant.tool_calls.length === 0) {
      return {
        reply: assistant.content ?? "",
        messages: history,
        tool_events: events,
      };
    }

    for (const call of assistant.tool_calls) {
      let parsedArgs: unknown = {};
      try {
        parsedArgs = call.function.arguments
          ? JSON.parse(call.function.arguments)
          : {};
      } catch {
        parsedArgs = {};
      }

      let result: unknown;
      let ok = true;
      let errorMessage: string | undefined;
      try {
        result = await dispatchTool(call.function.name, parsedArgs);
        if (result && typeof result === "object" && "error" in result) {
          ok = false;
          errorMessage = String((result as { error: unknown }).error);
        }
      } catch (err) {
        ok = false;
        errorMessage = err instanceof Error ? err.message : String(err);
        result = { error: errorMessage };
      }

      events.push({
        tool: call.function.name,
        arguments: parsedArgs,
        result,
        ok,
        error: errorMessage,
      });

      history.push({
        role: "tool",
        tool_call_id: call.id,
        name: call.function.name,
        content: JSON.stringify(result),
      });
    }
  }

  history.push({
    role: "user",
    content: "Please give your best final answer now based on what you found.",
  });
  const final = await callOpenAI(history);
  history.push({ role: "assistant", content: final.content ?? null });
  return {
    reply: final.content ?? "",
    messages: history,
    tool_events: events,
  };
}
