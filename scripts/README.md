# scripts/

The Python data pipeline and MCP server for Reviews Intelligence. The Next.js
app and the MCP server both read what these scripts write into Supabase. For the
design rationale behind each stage, see [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## Pipeline

```
seed → pull → load → embed → cluster+label → score → anomaly → trend → brief
```

Each stage is a standalone, re-runnable job that communicates only through
Postgres tables. Stages that cost money print a cost estimate and accept
`--dry-run`.

| Script | Stage | What it does |
|---|---|---|
| `seed_locations.py` | seed | Upsert verified rows from `data/locations.json` into `locations`. Idempotent. |
| `pull_reviews.py` | ingest A | Trigger Bright Data (one job per source), poll, download raw JSON to `brightdata-raw/`, log to `raw_scrapes`. **Paid.** |
| `load_reviews.py` | ingest B | Normalize raw JSON → upsert into `reviews` + `location_metadata`. Pure disk→DB, free, idempotent. |
| `embed_reviews.py` | embed | Embed un-embedded review text via OpenAI (`text-embedding-3-large` → 1536d). Resumable. **Paid (~$0.25 full).** |
| `cluster_network.py` | cluster | Network-wide rating-partitioned KMeans → label → write `themes` (scope=network). **Paid (Haiku).** |
| `label_themes.py` | cluster | Per-location version of the same cluster→label pipeline (scope=location). **Paid (Haiku).** |
| `cluster_reviews.py` | cluster | Eyeball-only clustering for tuning `k` — writes intermediate artifacts, not `themes`. |
| `compute_location_scores.py` | score | Build the location × theme prevalence matrix into `location_theme_scores`. |
| `compute_anomalies.py` | anomaly | Robust z-scores + direction per theme; small-sample guard. |
| `compute_trends.py` | trend | Recent-vs-prior 90-day windows (overall rating + per-theme) into `trends`. |
| `generate_brief.py` | brief | Assemble intelligence packet → one Claude Opus call → write `briefs`. **Paid (Opus).** |
| `print_intelligence.py` | debug | Print the assembled intelligence packet for a location (no model call). |
| `mcp_server.py` | serve | Read-only MCP server (8 tools) over the dataset. Launched by an MCP client. |

### `lib/`

| Module | Purpose |
|---|---|
| `config.py` | Env loading + validation; canonical model names and embedding dims. |
| `db.py` | Supabase client (service-role, server-side only, 30s timeout). |
| `openai.py` | OpenAI client + embedding constants. |
| `anthropic.py` | Anthropic client for labeling (Haiku) and briefs (Opus). |
| `brightdata.py` | Bright Data API helpers (trigger / poll / download, with race handling). |
| `clustering.py` | Rating-partitioned KMeans primitives. |
| `labeling.py` | Three-stage Haiku theme labeling with verbatim-quote validation. |
| `intelligence.py` | Assemble a per-location intelligence packet from precomputed tables. |
| `search.py` | Semantic review search via the `search_reviews_by_embedding` pgvector RPC. |

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
python -m scripts.seed_locations                 # --dry-run to preview
python -m scripts.pull_reviews   --source both --years 2
python -m scripts.load_reviews   --latest
python -m scripts.embed_reviews  --dry-run        # then without --dry-run
python -m scripts.cluster_network --replace
python -m scripts.label_themes   --replace
python -m scripts.compute_location_scores --replace
python -m scripts.compute_anomalies
python -m scripts.compute_trends --replace
python -m scripts.generate_brief --all --replace
```

Most paid stages take `--dry-run` (prints a cost estimate and exits) and
`--replace` (overwrite existing rows for the target instead of refusing).

## MCP server

`mcp_server.py` is launched by an MCP client (e.g. Claude Desktop) as a stdio
subprocess, not run by hand. To smoke-test it interactively:

```bash
npx @modelcontextprotocol/inspector python -m scripts.mcp_server
```
