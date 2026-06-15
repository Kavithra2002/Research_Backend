/* ────────────────────────────────────────────────────────────────────────── *
 * Shared agent tools — available to EVERY agent (Robin, Sage, …).
 *
 * These make the agents friendlier and more accurate on everyday + hard
 * questions:
 *   • get_current_time → real date/time awareness (LLMs have none on their own)
 *   • calculate        → exact arithmetic (LLMs are unreliable at maths)
 *
 * Each tool is a plain schema object + a handler, mirroring the RAG tool so the
 * agents can spread them into their TOOLS array and dispatch by name.
 * ────────────────────────────────────────────────────────────────────────── */

const DEFAULT_TZ = "Asia/Colombo";

/** A one-line "today is …" string to inject into system prompts. */
export function currentDateContext(timeZone = DEFAULT_TZ): string {
  const now = new Date();
  try {
    const formatted = new Intl.DateTimeFormat("en-GB", {
      timeZone,
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(now);
    return `Current date & time: ${formatted} (${timeZone}). Use this for any time/date questions instead of guessing.`;
  } catch {
    return `Current date & time (UTC): ${now.toISOString()}. Use this for any time/date questions instead of guessing.`;
  }
}

/* ────────────────────────────────────────────────────────────────────────── *
 * get_current_time
 * ────────────────────────────────────────────────────────────────────────── */

export const GET_CURRENT_TIME_TOOL = {
  type: "function" as const,
  function: {
    name: "get_current_time",
    description:
      "Get the real current date and time. Call this whenever the user asks what time/day/date it is, or when you need 'today' to compute ages, durations, deadlines, or how recent something is. Never guess the date.",
    parameters: {
      type: "object",
      properties: {
        timezone: {
          type: "string",
          description:
            "Optional IANA timezone, e.g. 'Asia/Colombo', 'UTC', 'America/New_York'. Defaults to Asia/Colombo.",
        },
      },
      additionalProperties: false,
    },
  },
} as const;

export function getCurrentTime(args: { timezone?: unknown }): unknown {
  const tz =
    typeof args.timezone === "string" && args.timezone.trim()
      ? args.timezone.trim()
      : DEFAULT_TZ;
  const now = new Date();
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short",
    }).format(now);
    return {
      timezone: tz,
      iso_utc: now.toISOString(),
      formatted: parts,
    };
  } catch {
    return {
      timezone: "UTC",
      iso_utc: now.toISOString(),
      formatted: now.toUTCString(),
      note: `Unknown timezone "${tz}"; returned UTC instead.`,
    };
  }
}

/* ────────────────────────────────────────────────────────────────────────── *
 * calculate — safe arithmetic via a tiny recursive-descent evaluator
 * (no eval; only numbers, + - * / % ^, parentheses and a few math functions).
 * ────────────────────────────────────────────────────────────────────────── */

export const CALCULATE_TOOL = {
  type: "function" as const,
  function: {
    name: "calculate",
    description:
      "Evaluate a mathematical expression EXACTLY. Use this for any arithmetic — sums, differences, percentages, growth rates, ratios, averages — instead of doing the maths yourself. Pass plain numbers (no thousands separators or currency symbols; convert bracketed negatives like (1,234) to -1234).",
    parameters: {
      type: "object",
      properties: {
        expression: {
          type: "string",
          description:
            "The expression, e.g. '(2100-1800)/1800*100', 'sqrt(144)', 'max(10, 20)', '(459-443)/443*100'. Supports + - * / % ^ , parentheses, and sqrt/abs/round/floor/ceil/min/max/pow/ln/log/exp, plus pi and e.",
        },
      },
      required: ["expression"],
      additionalProperties: false,
    },
  },
} as const;

type Token =
  | { kind: "num"; value: number }
  | { kind: "ident"; value: string }
  | { kind: "op"; value: string };

function tokenize(input: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  const s = input;
  while (i < s.length) {
    const ch = s[i];
    if (ch === " " || ch === "\t" || ch === "\n") {
      i += 1;
      continue;
    }
    if (/[0-9.]/.test(ch)) {
      let num = "";
      while (i < s.length && /[0-9.]/.test(s[i])) {
        num += s[i];
        i += 1;
      }
      const value = Number(num);
      if (Number.isNaN(value)) throw new Error(`Invalid number "${num}"`);
      tokens.push({ kind: "num", value });
      continue;
    }
    if (/[a-zA-Z_]/.test(ch)) {
      let id = "";
      while (i < s.length && /[a-zA-Z_0-9]/.test(s[i])) {
        id += s[i];
        i += 1;
      }
      tokens.push({ kind: "ident", value: id.toLowerCase() });
      continue;
    }
    if ("+-*/%^(),".includes(ch)) {
      // Support ** as power.
      if (ch === "*" && s[i + 1] === "*") {
        tokens.push({ kind: "op", value: "^" });
        i += 2;
        continue;
      }
      tokens.push({ kind: "op", value: ch });
      i += 1;
      continue;
    }
    throw new Error(`Unexpected character "${ch}"`);
  }
  return tokens;
}

const CONSTS: Record<string, number> = { pi: Math.PI, e: Math.E };
const FUNCS: Record<string, (args: number[]) => number> = {
  sqrt: (a) => Math.sqrt(a[0]),
  abs: (a) => Math.abs(a[0]),
  round: (a) => Math.round(a[0]),
  floor: (a) => Math.floor(a[0]),
  ceil: (a) => Math.ceil(a[0]),
  ln: (a) => Math.log(a[0]),
  log: (a) => Math.log10(a[0]),
  exp: (a) => Math.exp(a[0]),
  pow: (a) => Math.pow(a[0], a[1]),
  min: (a) => Math.min(...a),
  max: (a) => Math.max(...a),
};

function evaluate(tokens: Token[]): number {
  let pos = 0;
  const peek = () => tokens[pos];
  const next = () => tokens[pos++];

  function parseExpr(): number {
    let left = parseTerm();
    while (peek() && peek().kind === "op" && "+-".includes((peek() as { value: string }).value)) {
      const op = (next() as { value: string }).value;
      const right = parseTerm();
      left = op === "+" ? left + right : left - right;
    }
    return left;
  }

  function parseTerm(): number {
    let left = parsePower();
    while (
      peek() &&
      peek().kind === "op" &&
      "*/%".includes((peek() as { value: string }).value)
    ) {
      const op = (next() as { value: string }).value;
      const right = parsePower();
      if (op === "*") left *= right;
      else if (op === "/") left /= right;
      else left %= right;
    }
    return left;
  }

  function parsePower(): number {
    const base = parseUnary();
    if (peek() && peek().kind === "op" && (peek() as { value: string }).value === "^") {
      next();
      const exp = parsePower(); // right-associative
      return Math.pow(base, exp);
    }
    return base;
  }

  function parseUnary(): number {
    const t = peek();
    if (t && t.kind === "op" && (t.value === "-" || t.value === "+")) {
      next();
      const v = parseUnary();
      return t.value === "-" ? -v : v;
    }
    return parsePrimary();
  }

  function parsePrimary(): number {
    const t = next();
    if (!t) throw new Error("Unexpected end of expression");
    if (t.kind === "num") return t.value;
    if (t.kind === "ident") {
      // Function call?
      if (peek() && peek().kind === "op" && (peek() as { value: string }).value === "(") {
        next(); // consume '('
        const args: number[] = [];
        if (!(peek() && peek().kind === "op" && (peek() as { value: string }).value === ")")) {
          args.push(parseExpr());
          while (peek() && peek().kind === "op" && (peek() as { value: string }).value === ",") {
            next();
            args.push(parseExpr());
          }
        }
        const close = next();
        if (!close || close.kind !== "op" || close.value !== ")") {
          throw new Error("Missing closing parenthesis");
        }
        const fn = FUNCS[t.value];
        if (!fn) throw new Error(`Unknown function "${t.value}"`);
        return fn(args);
      }
      if (t.value in CONSTS) return CONSTS[t.value];
      throw new Error(`Unknown identifier "${t.value}"`);
    }
    if (t.kind === "op" && t.value === "(") {
      const v = parseExpr();
      const close = next();
      if (!close || close.kind !== "op" || close.value !== ")") {
        throw new Error("Missing closing parenthesis");
      }
      return v;
    }
    throw new Error(`Unexpected token "${(t as { value: unknown }).value}"`);
  }

  const result = parseExpr();
  if (pos !== tokens.length) {
    throw new Error("Unexpected trailing input in expression");
  }
  return result;
}

export function calculate(args: { expression?: unknown }): unknown {
  const expression =
    typeof args.expression === "string" ? args.expression.trim() : "";
  if (!expression) return { error: "An expression is required." };
  try {
    const tokens = tokenize(expression);
    if (tokens.length === 0) return { error: "Empty expression." };
    const value = evaluate(tokens);
    if (!Number.isFinite(value)) {
      return { expression, error: "Result is not a finite number." };
    }
    return { expression, result: value };
  } catch (err) {
    return {
      expression,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

/* ────────────────────────────────────────────────────────────────────────── *
 * Convenience bundle
 * ────────────────────────────────────────────────────────────────────────── */

export const COMMON_TOOLS = [GET_CURRENT_TIME_TOOL, CALCULATE_TOOL] as const;

/** Dispatch a common tool by name. Returns undefined if not a common tool. */
export function dispatchCommonTool(
  name: string,
  args: Record<string, unknown>,
): unknown | undefined {
  switch (name) {
    case "get_current_time":
      return getCurrentTime(args);
    case "calculate":
      return calculate(args);
    default:
      return undefined;
  }
}
