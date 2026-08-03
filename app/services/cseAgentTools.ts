import { lookupCseEquity, type CseSnapshot } from "./cseMarketService";
import { TUCK_SECTIONS, fetchCseData } from "./tuckService";

/* ────────────────────────────────────────────────────────────────────────── *
 * Shared CSE live-market tools for every conversational agent.
 *
 * • get_cse_share_price — resolves a company name/symbol to live tradeSummary
 *   figures (last price, change %, day range, market cap).
 * • get_cse_market_data — fetches market-wide slices (ASPI, gainers, losers…).
 * ────────────────────────────────────────────────────────────────────────── */

const MAX_CSE_CHARS = 18000;
const CSE_SECTION_IDS: string[] = TUCK_SECTIONS.map((s) => s.id);

/** System-prompt block — import into every agent's buildSystemPrompt(). */
export const CSE_MARKET_QUERY_GUIDANCE = [
  "CSE live market data (MANDATORY — always check BEFORE web search or saying 'not found'):",
  "  • In the Ambeon Console, short ticker codes (SCAP, DIAL, SAMP, JKH) are CSE-listed companies. ALWAYS call get_cse_share_price with the code/name FIRST — e.g. SCAP = Softlogic Capital PLC on the CSE. Do NOT assume a ticker is a foreign company from a prior web search.",
  "  • If CSE TICKER RESOLUTION or PRE-FETCHED CSE LIVE DATA appears in your instructions, that identity is authoritative — use it and do not contradict it.",
  "  • For ANY share-price / stock-price / 'trading at' / 'current price' question → call get_cse_share_price (or use pre-fetched CSE data).",
  "  • Follow-ups with no company in the message → infer the ticker/name from the USER's earlier messages (not a wrong assistant guess) and call get_cse_share_price.",
  "  • For market-wide data (ASPI, S&P SL20, gainers, losers, most active) → call get_cse_market_data.",
  "  • For company news/expansion/strategy about a CSE ticker → resolve via CSE first, THEN web_search using the full CSE company name + ticker in the query.",
  "  • NEVER say a company is not on the CSE or that you cannot find data without calling get_cse_share_price first.",
  "  • Only use web_search for prices if get_cse_share_price confirms the company is NOT on the CSE.",
].join("\n");

export const GET_CSE_SHARE_PRICE_TOOL = {
  type: "function" as const,
  function: {
    name: "get_cse_share_price",
    description:
      "Look up the LIVE share price and today's trading data for one or more CSE-listed companies. Uses the official CSE tradeSummary API (last traded price, change %, day high/low, previous close, market cap). ALWAYS call this for questions like 'Dialog share price today', 'what is XYZ trading at', or any current CSE equity price — before web search or saying data is unavailable.",
    parameters: {
      type: "object",
      properties: {
        companies: {
          type: "array",
          items: { type: "string" },
          description:
            "Company name(s) or CSE symbol(s) to look up, e.g. ['Dialog Axiata', 'Sampath Bank'] or ['DIAL.N0000'].",
        },
      },
      required: ["companies"],
      additionalProperties: false,
    },
  },
} as const;

export const GET_CSE_MARKET_DATA_TOOL = {
  type: "function" as const,
  function: {
    name: "get_cse_market_data",
    description:
      "Fetch LIVE market-wide data from the Colombo Stock Exchange for the requested sections: ASPI / S&P SL20 indices, market summary, top gainers, top losers, most active trades. For a SPECIFIC company's share price, use get_cse_share_price instead.",
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

export const CSE_AGENT_TOOLS = [
  GET_CSE_SHARE_PRICE_TOOL,
  GET_CSE_MARKET_DATA_TOOL,
] as const;

function formatSnapshot(s: CseSnapshot) {
  return {
    query: s.query,
    name: s.name,
    symbol: s.symbol,
    price_lkr: s.price,
    previous_close_lkr: s.previousClose,
    change_lkr: s.change,
    change_percent: s.changePercent,
    day_high_lkr: s.dayHigh,
    day_low_lkr: s.dayLow,
    market_cap_lkr: s.marketCap,
    as_of: s.asOf,
    match_score: s.matchScore,
    source: "CSE tradeSummary (live)",
  };
}

async function lookupSharePrices(
  companies: string[],
): Promise<Record<string, unknown>> {
  const queries = companies.map((c) => String(c ?? "").trim()).filter(Boolean);
  if (queries.length === 0) {
    return { error: "At least one company name or symbol is required." };
  }

  const found: ReturnType<typeof formatSnapshot>[] = [];
  const notFound: Array<{
    query: string;
    candidates: Array<{ name: string; symbol: string; score: number }>;
  }> = [];

  for (const query of queries) {
    const result = await lookupCseEquity(query);
    if (result.matched) {
      found.push(formatSnapshot(result.matched));
    } else {
      notFound.push({ query, candidates: result.candidates });
    }
  }

  return {
    found,
    not_found: notFound,
    note:
      found.length > 0
        ? "Report figures exactly as returned. State they are live CSE data and include the symbol and as_of timestamp."
        : notFound.length > 0
          ? "No confident CSE match. Check candidates for typos/ambiguity, try a shorter name (e.g. 'Dialog' for Dialog Axiata), or the CSE symbol. If the company is foreign-listed, use web_search for its home-exchange price."
          : undefined,
  };
}

async function fetchMarketData(
  sections: string[],
): Promise<Record<string, unknown>> {
  const requested = sections.filter((s) => CSE_SECTION_IDS.includes(s));
  const wanted = requested.length > 0 ? requested : CSE_SECTION_IDS;
  const data = await fetchCseData(wanted);
  const json = JSON.stringify(data);
  if (json.length > MAX_CSE_CHARS) {
    return {
      sections: wanted,
      truncated: true,
      data: `${json.slice(0, MAX_CSE_CHARS)}…(truncated)`,
      source: "CSE live API",
    };
  }
  return { sections: wanted, truncated: false, data, source: "CSE live API" };
}

/** Dispatch CSE tools by name. Returns undefined if the name is not a CSE tool. */
export async function dispatchCseTool(
  name: string,
  args: unknown,
): Promise<unknown | undefined> {
  const a = (args ?? {}) as Record<string, unknown>;

  if (name === "get_cse_share_price") {
    const companies = Array.isArray(a.companies)
      ? a.companies.map((c) => String(c))
      : typeof a.company === "string"
        ? [a.company]
        : [];
    return lookupSharePrices(companies);
  }

  if (name === "get_cse_market_data") {
    const sections = Array.isArray(a.sections)
      ? a.sections.map((s) => String(s))
      : [];
    return fetchMarketData(sections);
  }

  return undefined;
}
