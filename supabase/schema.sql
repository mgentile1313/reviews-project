-- reviews-project schema
-- Run in the Supabase SQL editor, or via `supabase db push` if using the CLI.

-- pgvector for embedding similarity search
create extension if not exists vector;

-- A scraped source: a product or business page on some review platform.
create table if not exists sources (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  url        text not null,
  platform   text,                       -- e.g. 'g2', 'trustpilot', 'amazon'
  created_at timestamptz not null default now()
);

-- An individual review belonging to a source.
create table if not exists reviews (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid not null references sources(id) on delete cascade,
  external_id text,                      -- platform's own id, used for dedupe
  author      text,
  rating      numeric,
  title       text,
  body        text not null,
  posted_at   timestamptz,
  embedding   vector(1536),              -- OpenAI text-embedding-3-small
  cluster_id  integer,                   -- assigned by scripts/ml/cluster.py
  scraped_at  timestamptz not null default now(),
  unique (source_id, external_id)
);

-- Approximate nearest-neighbour index for cosine similarity search.
create index if not exists reviews_embedding_idx
  on reviews using hnsw (embedding vector_cosine_ops);

create index if not exists reviews_cluster_idx on reviews (cluster_id);

-- A cluster of semantically similar reviews within a source.
create table if not exists clusters (
  id         integer primary key,
  source_id  uuid references sources(id) on delete cascade,
  label      text,
  size       integer,
  created_at timestamptz not null default now()
);

-- A Claude-generated brief summarising a cluster (or a whole source).
create table if not exists briefs (
  id         uuid primary key default gen_random_uuid(),
  source_id  uuid references sources(id) on delete cascade,
  cluster_id integer references clusters(id) on delete cascade,
  content    text not null,
  model      text,
  created_at timestamptz not null default now()
);
