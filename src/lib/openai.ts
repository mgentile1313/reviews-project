import OpenAI from "openai";

let client: OpenAI | null = null;

/** OpenAI client, created lazily so the app can build without env vars. */
export function getOpenAI(): OpenAI {
  if (!client) {
    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) throw new Error("OPENAI_API_KEY is not set");
    client = new OpenAI({ apiKey });
  }
  return client;
}

export const EMBEDDING_MODEL = "text-embedding-3-small";
export const EMBEDDING_DIMENSIONS = 1536;

/** Embed one or more strings. Returns one vector per input. */
export async function embed(input: string | string[]): Promise<number[][]> {
  const res = await getOpenAI().embeddings.create({
    model: EMBEDDING_MODEL,
    input,
  });
  return res.data.map((d) => d.embedding);
}
