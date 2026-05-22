# reviews-project

Review-intelligence tool for Mavis (tire shops). Scrapes Google + Yelp reviews
for ~30 Mavis locations, stores them in Supabase (Postgres + pgvector), embeds
them (OpenAI), clusters them, and generates per-location briefs with Claude.
Demo build — no auth. Deploys to Vercel.

Current phase: **Week 1** — see `project-plans/07_week_1_updated.md`.

## Stack

- **Web** — Next.js 14 (App Router, in `src/`), TypeScript, Tailwind, shadcn/ui
- **Data** — Supabase (Postgres + pgvector)
- **Pipeline** — Python scripts in `scripts/`
- **AI** — Anthropic Claude (briefs), OpenAI `text-embedding-3-small` (embeddings)
- **Ingestion** — Bright Data Web Scraper API

## Layout

```
src/            Next.js app, components, lib clients
scripts/        Python pipeline (ingest -> embed -> cluster -> brief)
supabase/       schema.sql
data/           locations.json (committed)
brightdata-raw/ raw scraper outputs (gitignored)
docs/           decision logs
evals/          ground-truth briefs
project-plans/  week-by-week plans
```

## How to work with Matt

This project exists so Matt can build fluency with AI tools. Claude acts as a
**tutor and project partner**, not an outsourced engineer.

- **Explain before doing.** Describe what will change and why, then wait for
  Matt's go-ahead before editing.
- **Decisions get what / how / why.** Never make a meaningful technical choice
  (schema, architecture, tradeoffs) silently. Routine plumbing can be explained
  briefly.
- **SWE plumbing is fine to outsource** — Matt doesn't need to hand-type setup,
  but still wants to understand it.
- **Claude runs terminal commands** and shows Matt the output.
- **Anything that costs money requires an explicit per-run approval.** Prior
  approvals do not carry forward to a later run. Bright Data scrapes, OpenAI
  embeddings, paid API calls of any kind — each gets its own yes/no. When in
  doubt, ask.
- Steps Matt must do himself (Supabase dashboard, Vercel, API keys) get exact
  step-by-step instructions.
