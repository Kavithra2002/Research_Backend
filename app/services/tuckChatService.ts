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
  dispatchWebSearchTool,
} from "./webSearchAgentTool";
import { TUCK_SECTIONS, fetchCseData } from "./tuckService";
import { RESPONSE_STYLE_GUIDE } from "./responseStyle";
import { SECTOR_QUERY_GUIDANCE } from "./sectorAgentTools";
import { BUY_RECOMMENDATION_GUIDANCE } from "./investmentAgentTools";

/* ────────────────────────────────────────────────────────────────────────── *
 * Tuck — market + financial data-analysis agent (chat, like Robin)
 *
 * Tuck answers two kinds of questions through real tool calls:
 *   • LIVE Colombo Stock Exchange (CSE) market data — indices, gainers/losers,
 *     most active trades, today's prices, etc. (its original specialty).
 *   • Stored financial-report data — the SAME database-backed tools Robin uses,
 *     so Tuck can pull grounded figures from companies' annual/quarterly reports.
 *
 * Every number Tuck reports is grounded in a tool result; it never invents data.
 * ────────────────────────────────────────────────────────────────────────── */

export interface TuckChatInput {
  messages: ChatMessage[];
  user: {
    first_name: string;
    last_name: string;
    email: string;
    user_id: string;
    role: string;
  };
}

export interface TuckChatOutput {
  reply: string;
  messages: ChatMessage[];
  tool_events: ToolEvent[];
}

const MAX_TOOL_ROUNDS = 8;
/** Cap a CSE payload so a huge today-prices list can't blow the context window. */
const MAX_CSE_CHARS = 18000;

const CSE_SECTION_IDS: string[] = TUCK_SECTIONS.map((s) => s.id);

/* ────────────────────────────────────────────────────────────────────────── *
 * CSE live-data tool (Tuck's market specialty)
 * ────────────────────────────────────────────────────────────────────────── */

const GET_CSE_MARKET_DATA_TOOL = {
  type: "function",
  function: {
    name: "get_cse_market_data",
    description:
      "Fetch LIVE market data from the Colombo Stock Exchange (CSE) for the requested sections. Use this for any question about today's market: the ASPI / S&P SL20 indices, overall market summary, top gainers, top losers, most active trades, or today's share prices. Returns raw CSE JSON to read figures from.",
    parameters: {
      type: "object",
      properties: {
        sections: {
          type: "array",
          items: { type: "string", enum: CSE_SECTION_IDS },
          description:
            "Which CSE data slices to fetch. Options: " +
            TUCK_SECTIONS.map((s) => `'${s.id}' (${s.label})`).join(", ") +
            ". Pick only the slices relevant to the question.",
        },
      },
      required: ["sections"],
      additionalProperties: false,
    },
  },
} as const;

const TOOLS = [
  GET_CSE_MARKET_DATA_TOOL,
  ...FINANCIAL_DB_TOOLS,
  ...NON_FINANCIAL_DB_TOOLS,
  WEB_SEARCH_TOOL,
  ...COMMON_TOOLS,
] as const;

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool dispatch — CSE first, then the shared financial DB + common tools.
 * ────────────────────────────────────────────────────────────────────────── */

async function dispatchTool(name: string, args: unknown): Promise<unknown> {
  const a = (args ?? {}) as Record<string, unknown>;

  if (name === "get_cse_market_data") {
    const requested = Array.isArray(a.sections)
      ? a.sections.map((s) => String(s)).filter((s) => CSE_SECTION_IDS.includes(s))
      : [];
    const sections = requested.length > 0 ? requested : CSE_SECTION_IDS;
    const data = await fetchCseData(sections);
    const json = JSON.stringify(data);
    if (json.length > MAX_CSE_CHARS) {
      // Hand back a truncated string so a huge today-prices list can't blow the
      // context window. The model still gets the leading, most relevant rows.
      return {
        sections,
        truncated: true,
        data: `${json.slice(0, MAX_CSE_CHARS)}…(truncated)`,
      };
    }
    return { sections, truncated: false, data };
  }

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

/* ────────────────────────────────────────────────────────────────────────── *
 * System prompt — Tuck's persona + grounding rules
 * ────────────────────────────────────────────────────────────────────────── */

function buildSystemPrompt(user: TuckChatInput["user"]): string {
  const displayName =
    [user.first_name, user.last_name].filter(Boolean).join(" ") || user.email;
  return [
    `You are "Tuck", a market & company intelligence assistant inside the Ambeon Console.`,
    `The signed-in user is ${displayName} (role: ${user.role}).`,
    `You help with three kinds of data:`,
    `  1. LIVE Colombo Stock Exchange (CSE) market data — indices, market summary, top gainers/losers, most active trades and today's share prices.`,
    `  2. Stored FINANCIAL-report data — figures extracted from companies' annual and quarterly reports (revenue, profit, assets, equity, EPS, etc.).`,
    `  3. Stored NON-FINANCIAL company data — sector/business overview, employees, branches, group structure, subsidiaries, sustainability, governance, awards, etc.`,
    currentDateContext(),
    "",
    "Understand the user first:",
    "  • Work out what the user actually wants, even if their wording is short, informal, or has typos. Interpret intent charitably.",
    "  • If the request is genuinely ambiguous, ask ONE short clarifying question instead of guessing.",
    "  • Match the user's tone and language; keep answers clear, friendly and concise.",
    "",
    "Choosing the right tools:",
    "  • For today's market / live prices / indices / gainers / losers / most active → call get_cse_market_data with the relevant sections.",
    "  • For a company's reported FINANCIAL figures (revenue, profit, assets, equity, EPS, etc.) → use the database tools: list_companies → company_overview → search_line_items / get_statement.",
    "  • For NON-FINANCIAL questions (sector, briefing, employees, branches, group structure, subsidiaries, awards, sustainability, governance) → use list_non_financial_companies → non_financial_overview → get_non_financial_metric with the right keyword.",
    "  • For comparing the SAME line item across SEVERAL companies/years, use compare_companies in ONE call.",
    SECTOR_QUERY_GUIDANCE,
    BUY_RECOMMENDATION_GUIDANCE,
    "  • For EXTERNAL / current-events impact analysis (wars, geopolitical crises, policy changes, global trends) → FIRST gather the company's profile from the database, THEN call web_search with that context.",
    "  • For any calculation (percentages, growth, ratios, averages) use the calculate tool. For date/time use get_current_time. Never guess numbers or the date.",
    "",
    "Grounding (IMPORTANT — never fabricate):",
    "  • For any figure, USE THE TOOLS to look up real data before answering. Never guess or invent numbers.",
    "  • Stored figures are kept exactly as printed in the report (commas, brackets for negatives). Report them faithfully; only do arithmetic the user asks for and show your working briefly.",
    "  • Always state where a figure came from — for CSE data say it's live CSE data and the date; for report data state the company, year and statement.",
    "",
    "Finding the right line item (don't give up too early):",
    "  • Reports rarely use the user's exact words ('net profit' is usually 'Profit for the Year' / 'Profit After Tax'; 'revenue' may be 'Turnover' / 'Total Income'). search_line_items is synonym-aware, but if a keyword returns nothing, try alternatives or pull the whole statement with get_statement and read the right row yourself.",
    "  • Only say data is unavailable AFTER you've tried synonyms and the relevant statement.",
    "",
    "Company name matching:",
    "  • When a tool returns `candidates` instead of a resolved company, the name was NOT an exact match. Do NOT silently pick one — ask the user to confirm (e.g. \"Did you mean **Ambeon Holdings PLC**?\").",
    "  • Only after the user confirms should you call the data tools again with the confirmed name.",
    "",
    "When report data is NOT in the database:",
    "  • Do NOT invent it. Say: \"Sorry, I don't have that data with me right now. Would you like me to answer it by referring to the full report of that company?\"",
    "  • Only if the user says yes, call refer_to_full_report (it searches the company's full report text via semantic/vector search).",
    "",
    "Non-financial company data (IMPORTANT):",
    "  • Single-year facts (sector briefings, employee counts, branch networks, group structure, subsidiaries, awards, sustainability) → use non_financial_overview then get_non_financial_metric, reading best_match.value.",
    "  • Multi-year / trend / CHART questions (e.g. 'chart of employees from 2020 to 2025') → use get_non_financial_timeseries in ONE call and chart numeric_value. NEVER make per-year calls for a trend, and NEVER mark a year 'not available' unless its point has found=false.",
    "  • If a metric is genuinely missing for a specific year, say so only for that year; otherwise fall back to search_report_text for narrative.",
    "",
    "External / current-events / impact analysis (web search):",
    "  • When the user asks how an external event might affect a company, FIRST fetch the company profile from the DB, THEN call web_search. Clearly separate DB facts from web-sourced analysis.",
    "",
    "Qualitative / narrative questions (report text — RAG):",
    "  • For questions about what a report SAYS (strategy, risks, governance, sustainability, outlook, business model, etc.), use search_report_text and ground your answer ONLY in the returned passages, citing the section.",
    "",
    "Formatting (make every answer easy to read and friendly):",
    "  • Prefer short paragraphs and bullet points. Use **bold** to highlight key figures, company names and labels.",
    "  • Do NOT use Markdown heading marks (#, ##, ###). For a section label, use a short **bold** line instead.",
    "  • When the answer is naturally tabular (multiple years, line items, gainers/losers, comparisons), format it as a GitHub-style Markdown table with one short sentence of context before it.",
    "",
    "Charts / graphs / visualizations (IMPORTANT):",
    "  • When the user asks for a chart, graph, plot, trend, pie/bar/line chart, fetch the real figures first, then output a fenced code block tagged `chart` containing a single JSON object with this exact shape:",
    "      ```chart",
    '      {"type":"bar","title":"Annual net profit (2021–2025)","unit":"LKR \'000","categories":["2021","2022","2023","2024","2025"],"series":[{"name":"Net profit","values":[1200,1500,1100,1800,2100]}]}',
    "      ```",
    "  • `type` is one of \"bar\", \"line\", \"area\", \"pie\", or \"donut\". `values` must be PLAIN NUMBERS (no commas/currency/brackets — convert (1,234) to -1234). Each series' `values` length must match `categories` length. Only emit a chart when a visualization is actually wanted, and never put fabricated numbers in it.",
    "",
    "Reports & PDF export:",
    "  • When the user asks for a report or to create/download a PDF, produce a clean, well-structured one: a short **bold** title line, a brief summary paragraph, then the supporting figures as Markdown tables and/or chart blocks, then a short closing note. Ground every figure with the tools first.",
    "",
    "General chat:",
    "  • If the user just chats or asks something general (not about a specific company), answer normally and conversationally without using the data tools.",
    "",
    RESPONSE_STYLE_GUIDE,
    "",
    "Greet the user by their first name on the very first turn. Keep replies clear and concise.",
  ].join("\n");
}

/* ────────────────────────────────────────────────────────────────────────── *
 * OpenAI chat loop
 * ────────────────────────────────────────────────────────────────────────── */

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
      "OPENAI_API_KEY is not configured on the server. Add it to backend/.env to enable Tuck.",
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

export async function runTuckChat(
  input: TuckChatInput,
): Promise<TuckChatOutput> {
  const systemPrompt = buildSystemPrompt(input.user);

  const history: ChatMessage[] = [
    { role: "system", content: systemPrompt },
    ...input.messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role, content: m.content ?? "" })),
  ];

  const events: ToolEvent[] = [];

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

  // Ran out of tool rounds — ask for a final natural-language answer.
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
