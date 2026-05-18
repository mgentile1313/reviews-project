import Anthropic from "@anthropic-ai/sdk";

let client: Anthropic | null = null;

/** Anthropic client, created lazily so the app can build without env vars. */
export function getAnthropic(): Anthropic {
  if (!client) {
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) throw new Error("ANTHROPIC_API_KEY is not set");
    client = new Anthropic({ apiKey });
  }
  return client;
}

/** Model used for brief generation / reasoning. */
export const BRIEF_MODEL = "claude-opus-4-7";
