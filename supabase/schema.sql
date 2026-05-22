-- reviews-project — database schema
--
-- This file documents the schema deployed in Supabase. Table and column
-- names + types were introspected from the live database (2026-05-19) via
-- the PostgREST OpenAPI spec, so they are accurate. Constraints, defaults,
-- indexes, and the review_summary view body are RECONSTRUCTED to match
-- intent — the deployed database remains the source of truth if they ever
-- diverge. Safe to re-run: every statement is idempotent.

-- pgvector: similarity search over review embeddings.
create extension if not exists vector;

-- ---------------------------------------------------------------------------
-- locations — one row per physical Mavis store.
-- ---------------------------------------------------------------------------
create table if not exists locations (
  id                 uuid primary key default gen_random_uuid(),
  internal_id        text unique not null,           -- e.g. 'mavis_001'
  portco             text not null default 'mavis',  -- portfolio company
  brand              text,
  name               text not null,
  google_full_address text,                       -- populated by load_reviews.py from Google's canonical address; null until then
  city               text,
  state              text,
  zip                text,
  region             text,
  performance_signal text,
  google_url         text,
  yelp_url           text,
  verified_google    boolean default false,
  verified_yelp      boolean default false,
  notes              text,
  created_at         timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- location_metadata — per-source listing facts for a location.
-- One row per (location, source): its Google listing, its Yelp listing.
-- ---------------------------------------------------------------------------
create table if not exists location_metadata (
  id                    uuid primary key default gen_random_uuid(),
  location_id           uuid not null references locations(id) on delete cascade,
  source                text not null check (source in ('google', 'yelp')),
  external_id           text,           -- platform's own place/business id
  external_name         text,
  external_url          text,
  overall_rating        numeric,        -- listing-level star average
  total_reviews_visible integer,        -- listing-level review count
  raw_metadata          jsonb,
  scraped_at            date not null default current_date,
  unique (location_id, source)
);

-- ---------------------------------------------------------------------------
-- reviews — one row per individual review.
-- ---------------------------------------------------------------------------
create table if not exists reviews (
  id                  uuid primary key default gen_random_uuid(),
  location_id         uuid not null references locations(id) on delete cascade,
  source              text not null check (source in ('google', 'yelp')),
  external_id         text,             -- platform review id, used for dedupe
  author              text,
  author_profile_url  text,
  author_review_count integer,
  rating              integer check (rating between 1 and 5),
  text                text,             -- null for rating-only reviews
  posted_at           date,
  owner_response      text,             -- nullable; not every review has one
  scraped_at          date not null default current_date,
  embedding           vector(1536),     -- OpenAI text-embedding-3-small
  source_metadata     jsonb
);

create index if not exists reviews_location_idx on reviews (location_id);
create index if not exists reviews_source_idx   on reviews (source);
create index if not exists reviews_posted_idx   on reviews (posted_at desc);

-- One review per (source, external_id); only enforced when external_id exists.
create unique index if not exists reviews_external_unique
  on reviews (source, external_id) where external_id is not null;

-- Approximate nearest-neighbour index for cosine similarity search.
create index if not exists reviews_embedding_idx
  on reviews using hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- raw_scrapes — audit trail of Bright Data snapshot jobs.
-- ---------------------------------------------------------------------------
create table if not exists raw_scrapes (
  id           uuid primary key default gen_random_uuid(),
  source       text not null,
  snapshot_id  text not null unique,
  status       text,
  record_count integer,
  triggered_at timestamptz not null default now(),
  completed_at timestamptz,
  notes        text
);

-- ---------------------------------------------------------------------------
-- review_summary — per-location, per-source rollup.
-- RECONSTRUCTED: produces the deployed view's columns; the deployed view
-- body may differ. Verify before relying on exact semantics.
-- ---------------------------------------------------------------------------
create or replace view review_summary as
select
  l.internal_id,
  l.name                  as location_name,
  l.region,
  r.source,
  count(*)                as review_count,
  avg(r.rating)           as avg_rating,
  min(r.posted_at)        as oldest_review,
  max(r.posted_at)        as newest_review,
  count(r.owner_response) as owner_response_count
from locations l
join reviews r on r.location_id = l.id
group by l.internal_id, l.name, l.region, r.source;
