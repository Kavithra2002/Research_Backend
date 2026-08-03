import { env } from "../config/env";
import { HttpError } from "../utils/httpError";
import { SEARCH_REPORT_TEXT_TOOL, searchReportText } from "./rag/ragTool";
import {
  COMMON_TOOLS,
  currentDateContext,
  dispatchCommonTool,
} from "./agentTools";
import {
  createGroup,
  deleteGroup,
  getGroup,
  listGroups,
  updateGroup,
  type CompanyGroupPublic,
  type OwnerMeta,
} from "./companyGroupService";
import { listCseCompanies as listCseCatalogCompanies } from "./cseCompanyCatalogService";
import { RESPONSE_STYLE_GUIDE } from "./responseStyle";
import {
  SECTOR_DB_TOOLS,
  SECTOR_QUERY_GUIDANCE,
  dispatchSectorTool,
} from "./sectorAgentTools";
import {
  BUY_RECOMMENDATION_GUIDANCE,
  INVESTMENT_SCREENING_TOOLS,
  dispatchInvestmentTool,
} from "./investmentAgentTools";
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
import { runCseChatPreflight } from "./cseSharePriceGuard";

/* ────────────────────────────────────────────────────────────────────────── *
 * Types
 * ────────────────────────────────────────────────────────────────────────── */

export type ChatRole = "system" | "user" | "assistant" | "tool";

export interface ChatMessage {
  role: ChatRole;
  content: string | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
}

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ToolEvent {
  tool: string;
  arguments: unknown;
  result: unknown;
  ok: boolean;
  error?: string;
}

export interface SageChatInput {
  messages: ChatMessage[];
  user: {
    first_name: string;
    last_name: string;
    email: string;
    user_id: string;
    role: string;
  };
  owner: OwnerMeta;
}

export interface SageChatOutput {
  reply: string;
  messages: ChatMessage[];
  tool_events: ToolEvent[];
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool schema exposed to the model
 * ────────────────────────────────────────────────────────────────────────── */

const TOOLS = [
  {
    type: "function",
    function: {
      name: "list_groups",
      description:
        "List every existing company group with its id, name, description, and member symbols. Call this before editing or deleting a group so you can confirm the right one with the user.",
      parameters: { type: "object", properties: {}, additionalProperties: false },
    },
  },
  {
    type: "function",
    function: {
      name: "list_cse_companies",
      description:
        "List all CSE-listed companies (name + ticker symbol). Use when the user wants to add or remove companies from a group, or when they describe companies by name and you need to resolve them to symbols.",
      parameters: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description:
              "Optional case-insensitive substring filter on company name or symbol.",
          },
        },
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "create_group",
      description:
        "Create a new company group. Always confirm the name and the list of company symbols with the user before calling this.",
      parameters: {
        type: "object",
        properties: {
          name: { type: "string", description: "Group name." },
          description: { type: "string", description: "Optional short description." },
          symbols: {
            type: "array",
            items: { type: "string" },
            description: "CSE ticker symbols belonging to the group.",
          },
        },
        required: ["name", "symbols"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "update_group",
      description:
        "Update an existing group. You MUST pass the group id from list_groups. Pass the full desired symbol set (this replaces, it does not append). Confirm changes with the user first.",
      parameters: {
        type: "object",
        properties: {
          id: { type: "string", description: "Mongo ObjectId of the group." },
          name: { type: "string" },
          description: { type: "string" },
          symbols: { type: "array", items: { type: "string" } },
        },
        required: ["id", "name", "symbols"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "delete_group",
      description:
        "Delete a group by id. Ask the user to confirm the exact group name before calling.",
      parameters: {
        type: "object",
        properties: {
          id: { type: "string", description: "Mongo ObjectId of the group." },
        },
        required: ["id"],
        additionalProperties: false,
      },
    },
  },
  SEARCH_REPORT_TEXT_TOOL,
  ...SECTOR_DB_TOOLS,
  ...INVESTMENT_SCREENING_TOOLS,
  ...CSE_AGENT_TOOLS,
  WEB_SEARCH_TOOL,
  ...COMMON_TOOLS,
] as const;

/* ────────────────────────────────────────────────────────────────────────── *
 * Tool implementations
 * ────────────────────────────────────────────────────────────────────────── */

type CseCompany = { name: string; symbol: string };

async function fetchCseCompanies(): Promise<CseCompany[]> {
  const result = await listCseCatalogCompanies();
  return result.companies.map((c) => ({ name: c.name, symbol: c.symbol }));
}

async function dispatchTool(
  name: string,
  args: unknown,
  owner: OwnerMeta,
): Promise<unknown> {
  const a = (args ?? {}) as Record<string, unknown>;

  switch (name) {
    case "list_groups": {
      const groups = await listGroups();
      return { groups: groups.map(slimGroup) };
    }

    case "list_cse_companies": {
      const all = await fetchCseCompanies();
      const q =
        typeof a.query === "string" ? a.query.trim().toLowerCase() : "";
      const filtered = q
        ? all.filter(
            (c) =>
              c.name.toLowerCase().includes(q) ||
              c.symbol.toLowerCase().includes(q),
          )
        : all;
      // Cap so we don't blow context: 80 is plenty for the model to reason on.
      const capped = filtered.slice(0, 80);
      return {
        total: filtered.length,
        returned: capped.length,
        truncated: filtered.length > capped.length,
        companies: capped,
      };
    }

    case "create_group": {
      const name = String(a.name ?? "").trim();
      const symbols = toStringArray(a.symbols);
      const description = String(a.description ?? "").trim();
      if (!name) throw new Error("Group name is required");
      if (symbols.length === 0)
        throw new Error("At least one symbol is required");
      const companies = await resolveCompanyNames(symbols);
      const created = await createGroup(
        {
          name,
          description,
          symbols: symbols.map((s) => s.toUpperCase()),
          companies,
        },
        owner,
      );
      return { group: slimGroup(created) };
    }

    case "update_group": {
      const id = String(a.id ?? "").trim();
      const name = String(a.name ?? "").trim();
      const symbols = toStringArray(a.symbols);
      const description = String(a.description ?? "").trim();
      if (!id) throw new Error("Group id is required");
      if (!name) throw new Error("Group name is required");
      if (symbols.length === 0)
        throw new Error("At least one symbol is required");
      const companies = await resolveCompanyNames(symbols);
      const updated = await updateGroup(id, {
        name,
        description,
        symbols: symbols.map((s) => s.toUpperCase()),
        companies,
      });
      return { group: slimGroup(updated) };
    }

    case "delete_group": {
      const id = String(a.id ?? "").trim();
      if (!id) throw new Error("Group id is required");
      const existing = await getGroup(id);
      if (!existing) throw new Error("Group not found");
      const res = await deleteGroup(id);
      return { removed: res.removed, group: slimGroup(existing) };
    }

    case "search_report_text":
      return searchReportText(a);

    default: {
      const sector = await dispatchSectorTool(name, a);
      if (sector !== undefined) return sector;

      const investment = await dispatchInvestmentTool(name, a);
      if (investment !== undefined) return investment;

      const cse = await dispatchCseTool(name, a);
      if (cse !== undefined) return cse;

      const web = await dispatchWebSearchTool(name, a);
      if (web !== undefined) return web;

      const common = dispatchCommonTool(name, a);
      if (common !== undefined) return common;
      throw new Error(`Unknown tool: ${name}`);
    }
  }
}

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const v of value) {
    if (typeof v !== "string") continue;
    const t = v.trim();
    if (!t) continue;
    const key = t.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(t);
  }
  return out;
}

async function resolveCompanyNames(symbols: string[]): Promise<string[]> {
  try {
    const cse = await fetchCseCompanies();
    const map = new Map(cse.map((c) => [c.symbol.toUpperCase(), c.name]));
    return symbols.map((s) => map.get(s.toUpperCase()) ?? s);
  } catch {
    return symbols;
  }
}

function slimGroup(g: CompanyGroupPublic) {
  return {
    id: g._id,
    name: g.name,
    description: g.description,
    symbols: g.symbols,
    companies: g.companies,
  };
}

/* ────────────────────────────────────────────────────────────────────────── *
 * OpenAI chat loop
 * ────────────────────────────────────────────────────────────────────────── */

const MAX_TOOL_ROUNDS = 6;

function buildSystemPrompt(user: SageChatInput["user"]): string {
  const displayName =
    [user.first_name, user.last_name].filter(Boolean).join(" ") || user.email;
  return [
    `You are "Sage", a friendly configuration agent inside the Ambeon Console.`,
    `Your main job is to help the user manage Company Groups (collections of CSE-listed companies that scope extraction runs).`,
    `The signed-in user is ${displayName} (role: ${user.role}, user_id: ${user.user_id}).`,
    currentDateContext(),
    "",
    "Understand the user first:",
    "  • Figure out what the user means even if their wording is short, informal, or has typos. If genuinely unclear, ask ONE short clarifying question.",
    "  • Be warm, concise, and match the user's tone and language.",
    "",
    "Capabilities (via tools):",
    "  • list_groups → see what exists",
    "  • list_cse_companies → discover or resolve companies",
    "  • create_group / update_group / delete_group → write actions",
    "  • search_report_text → semantic/vector search over company report text (About Us, strategy, risks, governance, sustainability, …). Use this if the user asks what a company's report says about something; ground the answer in the returned passages and cite the section.",
    "  • list_sectors / list_companies_by_sector → discover which companies exist in the database by sector (for sector-wise questions).",
    "  • screen_available_companies → screen all database companies for buy recommendations.",
    SECTOR_QUERY_GUIDANCE,
    BUY_RECOMMENDATION_GUIDANCE,
    CSE_MARKET_QUERY_GUIDANCE,
    WEB_SEARCH_GUIDANCE,
    "",
    "Handling any kind of question:",
    "  • You can also help with general questions — small talk (\"how are you\"), greetings, definitions, explaining hard words or technical concepts.",
    "  • For factual questions needing current data (commodity prices, forex, world news, macro trends) — call web_search; never say you lack access without searching first.",
    "  • Date/time questions → use the get_current_time tool (never guess the date).",
    "  • Any calculation → use the calculate tool for the exact result instead of doing maths yourself.",
    "  • After helping with a general question, gently steer back to what you do best (managing company groups) if it fits.",
    "",
    "Conversational rules:",
    "  • Always greet the user by their first name on the very first turn.",
    "  • At the start, mention your three main actions: (1) create a group, (2) edit a group, (3) delete a group — but still help with whatever the user actually asks.",
    "  • Before any write action (create/update/delete), restate the plan and ask the user to confirm with a short yes/no.",
    "  • When editing, fetch the existing group first so you know its current symbols.",
    "  • Keep replies short and action-oriented. Use bullet lists for choices, not walls of text.",
    "  • Never invent group ids or company symbols — always look them up via tools.",
    "",
    RESPONSE_STYLE_GUIDE,
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
      "OPENAI_API_KEY is not configured on the server. Add it to backend/.env to enable Sage.",
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
      temperature: 0.3,
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
  if (!choice) {
    throw HttpError.internal("OpenAI returned no choices");
  }
  return choice.message;
}

export async function runSageChat(input: SageChatInput): Promise<SageChatOutput> {
  const preflight = await runCseChatPreflight(input.messages);
  const systemPrompt =
    buildSystemPrompt(input.user) + (preflight?.systemAppendix ?? "");

  // Strip any prior tool_calls without matching tool replies the client might
  // have sent — we always rebuild the loop from system + history.
  const history: ChatMessage[] = [
    { role: "system", content: systemPrompt },
    ...input.messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({
        role: m.role,
        content: m.content ?? "",
      })),
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

    // Append assistant turn (may include tool_calls)
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
        result = await dispatchTool(call.function.name, parsedArgs, input.owner);
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

  // Safety: ran out of tool rounds, ask the model for a final natural-language wrap-up
  history.push({
    role: "user",
    content:
      "Please summarize what just happened in one short sentence and ask what to do next.",
  });
  const final = await callOpenAI(history);
  history.push({ role: "assistant", content: final.content ?? null });
  return {
    reply: final.content ?? "",
    messages: history,
    tool_events: events,
  };
}
