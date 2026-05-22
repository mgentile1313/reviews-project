# scripts/

Python data pipeline for reviews-project. The Next.js app reads what these
scripts write into Supabase.

## Pipeline

```
seed -> pull (Phase A) -> load (Phase B) -> embed (Phase C)
```

Two-phase ELT: scrape and normalize are separate scripts, with raw JSON
persisted to disk between them. Bright Data costs money per pull; disk is
free. If the normalizer has a bug, re-run the load against saved raw —
never re-pay for data.

| Script | What it does |
|---|---|
| `seed_locations.py` | Read `data/locations.json`, upsert verified rows into Supabase `locations`. Idempotent. |
| `pull_reviews.py` | **Phase A.** Read verified locations from Supabase, batch one Bright Data trigger per source, save raw JSON to `brightdata-raw/`, log snapshot to `raw_scrapes`. Does NOT touch `reviews`. |
| `load_reviews.py` *(not yet built)* | **Phase B.** Read raw JSON, normalize, upsert into `reviews` + `location_metadata`. |
| `embed_reviews.py` *(not yet built)* | **Phase C.** Embed un-embedded review text via OpenAI. |
| `lib/brightdata.py` | Bright Data API helpers (trigger / poll / download). |
| `lib/db.py` | Supabase client (service-role; server-side only). |
| `lib/config.py` | Env loading + var validation. |

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
python -m scripts.seed_locations              # --dry-run to preview
python -m scripts.pull_reviews --source both --years 2 --limit 5
```
