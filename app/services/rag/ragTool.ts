import { searchChunks } from "./vectorStore";
import { resolveCompanySlug } from "./ingest";

/**
 * Shared RAG tool exposed to EVERY agent (Robin, Sage, …).
 *
 * It performs semantic vector search over the embedded report text stored in
 * MongoDB (`document_chunks`) and returns the most relevant passages with
 * citations, so the model can answer narrative / qualitative questions that are
 * not available as structured figures.
 */
export const SEARCH_REPORT_TEXT_TOOL = {
  type: "function" as const,
  function: {
    name: "search_report_text",
    description:
      "Semantic (vector) search over the text of companies' annual/quarterly reports — About Us, Chairman's review, MD&A, risk management, governance, sustainability, and the narrative around financial statements. Use this for qualitative questions, WHY a figure moved (e.g. reason for a loss/profit drop, forex impact, provisions, impairments, one-off charges), or 'what does the report say about …' questions. Call this automatically — do NOT ask the user for permission first. Returns the most relevant passages with citations. Always ground your answer in the returned passages and cite the section.",
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description:
            "What to look for, in natural language (e.g. 'risk management approach', 'sustainability initiatives', 'chairman's outlook on growth').",
        },
        company: {
          type: "string",
          description:
            "Optional company name or slug to restrict the search to one company. Omit to search across all companies.",
        },
        top_k: {
          type: "integer",
          description: "How many passages to return (default 5, max 10).",
        },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
} as const;

export interface SearchReportTextArgs {
  query?: unknown;
  company?: unknown;
  top_k?: unknown;
}

/** Execute the search_report_text tool. Returns a model-friendly result. */
export async function searchReportText(args: SearchReportTextArgs): Promise<unknown> {
  const query = typeof args.query === "string" ? args.query.trim() : "";
  if (!query) return { error: "A search query is required." };

  let companySlug: string | undefined;
  if (typeof args.company === "string" && args.company.trim()) {
    companySlug = (await resolveCompanySlug(args.company)) ?? undefined;
  }

  const topK = Math.min(
    Math.max(typeof args.top_k === "number" ? args.top_k : 5, 1),
    10,
  );

  const matches = await searchChunks({ query, companySlug, topK });

  if (matches.length === 0) {
    return {
      match_count: 0,
      matches: [],
      note:
        "No indexed report text matched. The report may not be ingested yet, or there is no relevant passage. Tell the user you couldn't find it in the reports.",
    };
  }

  return {
    match_count: matches.length,
    matches: matches.map((m) => ({
      company: m.company_name ?? m.company_slug,
      source: m.source,
      section: m.section_title,
      year: m.year,
      citation: m.ref,
      relevance: Number(m.score.toFixed(4)),
      text: m.text,
    })),
  };
}
