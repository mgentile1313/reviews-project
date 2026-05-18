# scripts/

Python data pipeline for reviews-project. The Next.js app reads what these
scripts write into Supabase.

## Pipeline

```
ingest  ->  embed  ->  cluster  ->  (brief)
```

1. **ingest** — `scripts/ingest/brightdata.py` scrapes reviews via the Bright
   Data Web Scraper API and upserts them into the `sources` / `reviews` tables.
2. **embed** — `scripts/ml/embed.py` embeds un-embedded reviews with OpenAI
   `text-embedding-3-small` and writes the `reviews.embedding` vectors.
3. **cluster** — `scripts/ml/cluster.py` clusters a source's embeddings
   (KMeans) and writes `reviews.cluster_id` + the `clusters` table.
4. **brief** — _TODO_: generate a Claude brief per cluster.

## Setup

```bash
cd scripts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Env vars are read from `../.env.local` (see `../.env.example`).

## Running

Run from the **project root** so the `scripts` package resolves:

```bash
python -m scripts.ingest.brightdata "https://www.g2.com/products/<slug>/reviews"
python -m scripts.ml.embed
python -m scripts.ml.cluster <source_id> 8
```
