import { lookupCseEquity, type CseSnapshot } from "./cseMarketService";

/* ────────────────────────────────────────────────────────────────────────── *
 * Server-side CSE preflight for every agent chat turn.
 *
 * 1. Ticker resolution — when the user mentions a code like SCAP, resolve it
 *    against the live CSE tradeSummary FIRST (→ SOFTLOGIC CAPITAL PLC), before
 *    the model can misread it as a foreign company from a prior web search.
 * 2. Share-price guard — auto-fetch live prices on price questions.
 * ────────────────────────────────────────────────────────────────────────── */

export interface CsePreflightEvent {
  tool: "get_cse_share_price" | "resolve_cse_ticker";
  arguments: Record<string, unknown>;
  result: unknown;
  ok: boolean;
}

export interface CsePreflight {
  systemAppendix: string;
  toolEvents: CsePreflightEvent[];
}

type SimpleMessage = { role: string; content: string | null | undefined };

const SHARE_PRICE_RE =
  /\b(share|stock)\s*price\b|\bprice\s*(today|now|current|latest|new)\b|\b(current|latest|new|today'?s?)\s+(share|stock)\s+price\b|\btrading\s+at\b|\bwhat\s+(?:is|are)\s+(?:the\s+)?(?:\w+\s+){0,4}(?:share|stock)\s+price\b|\bhow\s+much\s+(?:is|are)\b.*\b(trading|shares?)\b/i;

const CSE_SYMBOL_RE = /\b([A-Z]{2,6}\.N\d{4})\b/g;
const TICKER_PAREN_RE = /\(([A-Z]{2,6})\)/g;
const BOLD_NAME_RE = /\*\*([^*]+)\*\*/g;

/** Common English words that look like tickers but are not. */
const TICKER_STOPWORDS = new Set([
  "A",
  "AN",
  "AS",
  "AT",
  "BE",
  "BY",
  "DO",
  "GO",
  "IF",
  "IN",
  "IS",
  "IT",
  "ME",
  "MY",
  "NO",
  "OF",
  "ON",
  "OR",
  "SO",
  "TO",
  "UP",
  "US",
  "WE",
  "ALL",
  "AND",
  "ANY",
  "ARE",
  "CAN",
  "CSE",
  "DID",
  "FOR",
  "GET",
  "HAS",
  "HAD",
  "HER",
  "HIM",
  "HIS",
  "HOW",
  "ITS",
  "LKR",
  "LTD",
  "MAY",
  "NEW",
  "NOT",
  "NOW",
  "OLD",
  "OUR",
  "OUT",
  "PLC",
  "THE",
  "USD",
  "WAS",
  "WHO",
  "WHY",
  "YOU",
  "API",
  "EPS",
  "GDP",
  "IPO",
  "PE",
  "PB",
  "Q1",
  "Q2",
  "Q3",
  "Q4",
  "CEO",
  "CFO",
]);

/** True when the message is asking for a live equity price. */
export function isSharePriceQuery(text: string): boolean {
  const t = String(text ?? "").trim();
  if (!t) return false;
  return SHARE_PRICE_RE.test(t);
}

function addHint(out: string[], seen: Set<string>, raw: string) {
  const v = raw.trim().replace(/\s+/g, " ");
  if (v.length < 2) return;
  const key = v.toLowerCase();
  if (seen.has(key)) return;
  seen.add(key);
  out.push(v);
}

/** Extract ticker / symbol hints the USER typed (authoritative over assistant guesses). */
export function extractUserTickerHints(messages: SimpleMessage[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();

  for (const msg of messages) {
    if (msg.role !== "user") continue;
    const text = String(msg.content ?? "");

    for (const m of text.matchAll(/\babout\s+([A-Za-z]{2,6})\b/gi)) {
      addHint(out, seen, m[1].toUpperCase());
    }
    for (const m of text.matchAll(/\b(?:for|on|re)\s+([A-Za-z]{2,6})\b/gi)) {
      addHint(out, seen, m[1].toUpperCase());
    }
    for (const m of text.matchAll(/\b([A-Z]{2,6})(?:\.N\d{4})?\b/g)) {
      const t = m[1];
      if (!TICKER_STOPWORDS.has(t)) addHint(out, seen, t);
    }
    for (const m of text.matchAll(TICKER_PAREN_RE)) addHint(out, seen, m[1]);
    for (const m of text.matchAll(CSE_SYMBOL_RE)) addHint(out, seen, m[1]);
  }

  return out.slice(0, 5);
}

function extractHintsFromText(text: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();

  for (const m of text.matchAll(BOLD_NAME_RE)) {
    const name = m[1].trim();
    if (
      /^(from web sources|source|summary|note|key points)$/i.test(name) ||
      /^\d/.test(name)
    ) {
      continue;
    }
    addHint(out, seen, name);
    for (const t of name.matchAll(TICKER_PAREN_RE)) addHint(out, seen, t[1]);
  }

  for (const m of text.matchAll(TICKER_PAREN_RE)) addHint(out, seen, m[1]);
  for (const m of text.matchAll(CSE_SYMBOL_RE)) addHint(out, seen, m[1]);

  const forMatch = text.match(
    /\bfor\s+([A-Z][A-Za-z0-9&.\- ]{2,60}?)(?:\?|$|,|\s+(?:share|stock|price|today))/i,
  );
  if (forMatch) addHint(out, seen, forMatch[1]);

  return out;
}

/** Companies to look up for a share-price question (user tickers first). */
export function extractSharePriceCompanies(
  messages: SimpleMessage[],
): string[] {
  const userTickers = extractUserTickerHints(messages);
  if (userTickers.length > 0) return userTickers.slice(0, 3);

  const userAssistant = messages.filter(
    (m) => m.role === "user" || m.role === "assistant",
  );
  if (userAssistant.length === 0) return [];

  const lastUser = [...userAssistant].reverse().find((m) => m.role === "user");
  const fromLast = extractHintsFromText(String(lastUser?.content ?? ""));
  if (fromLast.length > 0) return fromLast.slice(0, 3);

  for (const msg of [...userAssistant].reverse().slice(0, 8)) {
    const hints = extractHintsFromText(String(msg.content ?? ""));
    if (hints.length > 0) return hints.slice(0, 3);
  }

  return [];
}

function formatSnapshot(s: CseSnapshot) {
  return {
    query: s.query,
    name: s.name,
    symbol: s.symbol,
    ticker: s.symbol.split(".")[0],
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

/** Resolve user-mentioned tickers against the live CSE universe. */
async function resolveCseTickers(hints: string[]) {
  const resolved: ReturnType<typeof formatSnapshot>[] = [];
  const unresolved: Array<{
    query: string;
    candidates: Array<{ name: string; symbol: string; score: number }>;
  }> = [];

  for (const query of hints) {
    const result = await lookupCseEquity(query);
    if (result.matched && result.matched.matchScore >= 0.9) {
      resolved.push(formatSnapshot(result.matched));
    } else if (result.matched) {
      resolved.push(formatSnapshot(result.matched));
    } else {
      unresolved.push({ query, candidates: result.candidates });
    }
  }

  return { resolved, unresolved };
}

/**
 * Run CSE preflight before every agent chat turn:
 * - resolve user ticker codes (SCAP → SOFTLOGIC CAPITAL PLC)
 * - auto-fetch share prices when the latest message asks for a price
 */
export async function runCseChatPreflight(
  messages: SimpleMessage[],
): Promise<CsePreflight | null> {
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const lastText = String(lastUser?.content ?? "").trim();
  const sharePrice = isSharePriceQuery(lastText);

  const userTickers = extractUserTickerHints(messages);
  const priceCompanies = sharePrice
    ? extractSharePriceCompanies(messages)
    : [];

  const lookupQueries = [
    ...new Set([...userTickers, ...priceCompanies].map((q) => q.trim())),
  ].filter(Boolean);

  if (lookupQueries.length === 0 && !sharePrice) return null;

  const events: CsePreflightEvent[] = [];
  const appendixParts: string[] = [];

  if (lookupQueries.length > 0) {
    const { resolved, unresolved } = await resolveCseTickers(lookupQueries);

    if (resolved.length > 0) {
      const identityBlock = resolved.map((r) => ({
        user_query: r.query,
        cse_name: r.name,
        cse_symbol: r.symbol,
        ticker: r.ticker,
        live_price_lkr: r.price_lkr,
        change_percent: r.change_percent,
        as_of: r.as_of,
      }));

      appendixParts.push(
        "CSE TICKER RESOLUTION (authoritative — overrides any prior assistant guess or foreign-company assumption):",
        "In the Ambeon Console, short codes like SCAP, DIAL, SAMP refer to CSE-listed companies unless the user explicitly says otherwise.",
        JSON.stringify(
          {
            resolved: identityBlock,
            instruction:
              "Use ONLY these CSE identities for this conversation. If a prior turn wrongly named a foreign company for the same ticker, ignore that — the CSE match is correct. For company news/expansion questions about this ticker, call web_search with the full CSE company name + ticker (e.g. 'Softlogic Capital SCAP Bangladesh expansion').",
          },
          null,
          2,
        ),
      );

      events.push({
        tool: "resolve_cse_ticker",
        arguments: { tickers: lookupQueries },
        result: { resolved, unresolved },
        ok: true,
      });
    }

    if (sharePrice) {
      const found = resolved.filter((r) =>
        lookupQueries.some(
          (q) => q.toLowerCase() === r.query.toLowerCase(),
        ),
      );

      appendixParts.push(
        "",
        "PRE-FETCHED CSE LIVE DATA (use these figures for share-price answers):",
        JSON.stringify(
          {
            prefetched: true,
            found: found.length > 0 ? found : resolved,
            not_found: unresolved,
            instruction:
              found.length > 0 || resolved.length > 0
                ? "Report price in LKR with symbol, change %, and as_of. Do NOT say the company is not on the CSE."
                : "No CSE match — try get_cse_share_price with alternate spellings, then web_search only if truly not listed.",
          },
          null,
          2,
        ),
      );

      events.push({
        tool: "get_cse_share_price",
        arguments: { companies: lookupQueries },
        result: {
          found: resolved,
          not_found: unresolved,
        },
        ok: true,
      });
    } else if (resolved.length === 0 && unresolved.length > 0) {
      appendixParts.push(
        "",
        "CSE lookup attempted for user ticker(s) but no confident match:",
        JSON.stringify({ not_found: unresolved }, null, 2),
        "Call get_cse_share_price with the ticker before assuming it is a foreign company.",
      );
    }
  } else if (sharePrice) {
    appendixParts.push(
      "MANDATORY — share-price question detected but no company/ticker resolved.",
      "Ask which CSE company or symbol they mean, or call get_cse_share_price once inferred from context.",
    );
  }

  if (appendixParts.length === 0) return null;

  return {
    systemAppendix: `\n${appendixParts.join("\n")}`,
    toolEvents: events,
  };
}

/** @deprecated Use runCseChatPreflight */
export const runCseSharePricePreflight = runCseChatPreflight;
