# reviews-project

Scrape product/business reviews, cluster them into themes, and generate briefs
with Claude.

## Stack

- **Web** — Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui
- **Backend** — Supabase (Postgres + pgvector) for storage and similarity search
- **Data pipeline** — Python scripts in [`scripts/`](./scripts) for ingestion + ML
- **AI** — Anthropic Claude (brief generation), OpenAI `text-embedding-3-small`
  (embeddings)
- **Ingestion** — Bright Data Web Scraper API
- **Deploy** — Vercel

> Demo build — no auth.

## Layout

```
src/
  app/            Next.js App Router pages + API routes
    api/sources/  example route (Supabase access pattern)
  components/ui/  shadcn/ui components
  lib/            supabase / anthropic / openai clients
scripts/          Python data pipeline — see scripts/README.md
supabase/
  schema.sql      database schema (tables + pgvector index)
```

## Getting started

1. **Env** — copy `.env.example` to `.env.local` and fill in the keys.
2. **Database** — run `supabase/schema.sql` in the Supabase SQL editor.
3. **Web app**

   ```bash
   npm install
   npm run dev
   ```

   Open [http://localhost:3000](http://localhost:3000).

4. **Data pipeline** — see [`scripts/README.md`](./scripts/README.md).

## Pipeline

```
Bright Data  ->  ingest  ->  embed (OpenAI)  ->  cluster (KMeans)  ->  brief (Claude)
```
