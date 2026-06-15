import { env } from "../../config/env";
import { HttpError } from "../../utils/httpError";

const EMBED_URL = "https://api.openai.com/v1/embeddings";

/** OpenAI caps embedding inputs per request; stay well under it. */
const BATCH_SIZE = 96;

interface EmbeddingResponse {
  data: Array<{ index: number; embedding: number[] }>;
}

/**
 * Embed many texts at once (batched + order-preserving). Returns one vector
 * per input, aligned to the input order.
 */
export async function embedTexts(texts: string[]): Promise<number[][]> {
  if (texts.length === 0) return [];
  if (!env.openai.apiKey) {
    throw HttpError.badRequest(
      "OPENAI_API_KEY is not configured on the server. Add it to backend/.env to enable RAG.",
    );
  }

  const out: number[][] = new Array(texts.length);

  for (let start = 0; start < texts.length; start += BATCH_SIZE) {
    const batch = texts.slice(start, start + BATCH_SIZE);
    const res = await fetch(EMBED_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.openai.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: env.openai.embedModel,
        input: batch.map((t) => (t.trim() === "" ? " " : t)),
      }),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new HttpError(
        res.status === 401 ? 500 : 502,
        `OpenAI embeddings request failed (${res.status})`,
        text || undefined,
      );
    }

    const data = (await res.json()) as EmbeddingResponse;
    for (const item of data.data) {
      out[start + item.index] = item.embedding;
    }
  }

  return out;
}

/** Embed a single string (convenience wrapper around {@link embedTexts}). */
export async function embedText(text: string): Promise<number[]> {
  const [vector] = await embedTexts([text]);
  return vector ?? [];
}
