# Database schema — reviews-project

Human-readable map of the Supabase (Postgres + pgvector) database. **This is
documentation, not the source of truth.** The executable, reproducible schema
lives in [`supabase/migrations/`](./migrations/) and is applied with the
Supabase CLI (`supabase db push`). If anything here disagrees with a migration,
the migration wins.

To rebuild the schema from scratch:

```bash
supabase link --project-ref <ref>
supabase db push          # applies every migration in order
```

## How the data flows

The pipeline (`scripts/`) fills these tables in order:

```
pull_reviews → reviews + raw_scrapes        (Bright Data scrape, audited)
embed_reviews → reviews.embedding           (OpenAI text-embedding-3-small)
cluster_* + label_themes → themes           (cluster reviews, label with Claude)
compute_location_scores → location_theme_scores   (per-location prevalence + z-score)
compute_anomalies → (reads location_theme_scores)
compute_trends → trends                     (90-day recent vs prior windows)
generate_brief → briefs                     (Claude-written manager report)
```

`locations` is seeded first (`seed_locations.py` from `data/locations.json`).

## Tables

### `locations` — one row per physical Mavis store (~34)
The directory. `internal_id` (e.g. `mavis_001`) is the stable human-facing code;
`id` (uuid) is what every other table foreign-keys to. Holds brand/address fields
plus the Google/Yelp listing URLs and `verified_*` flags used by the scraper.

### `location_metadata` — per-source listing facts
One row per `(location, source)` — i.e. a location's Google listing and its Yelp
listing. Stores listing-level `overall_rating` and `total_reviews_visible` plus
raw scraper metadata. Unique on `(location_id, source)`.

### `reviews` — one row per individual review (~19k)
The core table. `source` is `google` | `yelp`; `external_id` dedupes within a
source; `rating` 1–5; `text` is null for rating-only reviews; `posted_at` is a
date. **`embedding vector(1536)`** holds the OpenAI embedding (with an `hnsw`
cosine index for fast similarity search). `owner_response` captures the store's
reply when present.

### `raw_scrapes` — Bright Data audit trail
One row per scrape snapshot job: `snapshot_id`, `status`, `record_count`,
timing. Lets you trace any review back to the ingest run that produced it.

### `themes` — clustered topics customers talk about
A theme is a cluster of reviews about one recurring topic. Key columns:
- `scope` — `location` (one store) or `network` (whole fleet)
- `pass` — `actions` (from negative reviews) or `working` (from positive)
- `specific` — true once a theme is distinct/actionable enough to surface
- `label`, `evidence_quote(s)`, `representative_review_ids`, `member_review_ids`
- `member_count`, `prevalence`, `avg_rating`
- `status` — `active` | `retired`; `previous_theme_id` links re-labeled themes
across runs. `candidates_json` retains the two-stage labeling intermediates.

### `location_theme_scores` — how each store indexes on each theme
Per `(location, theme)`: `prevalence` (share of that store's same-pass reviews in
the theme), `z_score` vs the network median, and `direction` (`above`/`below`).
This is the anomaly layer — where a store is unusually better or worse than peers.

### `trends` — directional movement over time
Per `(location, scope)`: compares a `recent` 90-day window to the `prior` one.
`scope` is `overall` (the store's rating) or `theme` (one theme's prevalence).
`direction` ∈ `improving` | `degrading` | `stable`; null when the prior window
is too thin to call.

### `briefs` — Claude-generated manager reports
One markdown brief per location: `content` (the report), the `model` used,
`generated_at`, `intelligence_as_of` (snapshot it was built from), `cost_usd`,
and `status` (`active` | `superseded`). The richest single artifact per store.

## View

### `review_summary`
Per-location, per-source rollup: `review_count`, `avg_rating`, oldest/newest
review dates, and `owner_response_count`. Used by the dashboard and the MCP
server's `get_location_stats`.

## Functions

### `search_reviews_by_embedding(query_embedding, match_count, min_similarity, target_location_id)`
Semantic search over `reviews.embedding`. Returns the nearest reviews by cosine
similarity (`1 - (embedding <=> query)`), optionally scoped to one location.
Called by `scripts/lib/search.py` and the MCP server's `search_reviews` tool.
Added in migration `..._add_review_search_function.sql`.

## Not part of this project

`langchain_pg_collection` and `langchain_pg_embedding` exist in the same Supabase
project but belong to a **separate SEC-filings RAG experiment** (LangChain's
PGVector store, holding Apple 10-K chunks). No code in this repo references them.
They appear in the baseline migration only so it stays a faithful mirror of the
live database. Ignore them for reviews-project.
