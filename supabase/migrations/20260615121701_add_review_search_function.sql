-- search_reviews_by_embedding — semantic search over review embeddings.
--
-- Called by scripts/lib/search.py (and through it, the MCP server's
-- search_reviews tool). Given a query embedding, returns the nearest
-- reviews by cosine similarity, optionally scoped to one location.
--
-- pgvector's `<=>` operator is cosine DISTANCE in [0, 2]; we report
-- similarity as `1 - distance` so callers get an intuitive [-1, 1] score
-- where higher = closer. Ordering by distance ascending lets the hnsw
-- index on reviews.embedding do the work.

create or replace function public.search_reviews_by_embedding (
  query_embedding vector(1536),
  match_count int default 10,
  min_similarity float default 0.0,
  target_location_id uuid default null
)
returns table (
  id uuid,
  location_id uuid,
  rating int,
  source text,
  posted_at date,
  text text,
  similarity float
)
language sql
stable
as $$
  select
    r.id,
    r.location_id,
    r.rating,
    r.source,
    r.posted_at,
    r.text,
    1 - (r.embedding <=> query_embedding) as similarity
  from public.reviews r
  where r.embedding is not null
    and (target_location_id is null or r.location_id = target_location_id)
    and 1 - (r.embedding <=> query_embedding) >= min_similarity
  order by r.embedding <=> query_embedding
  limit match_count;
$$;
