"""Semantic review search via pgvector.

Embeds a query string with OpenAI (same model used to embed reviews), then
calls the `search_reviews_by_embedding` Postgres function to find the
nearest reviews by cosine similarity. Cheap (~$0.0001 per query), fast
(sub-second), location-scopable.

Use cases:
  - MCP server tool: "any reviews mentioning safety issues at Mid?"
  - Dashboard theme drill-down: find reviews most similar to a theme's
    evidence quote.
  - Ad-hoc investigation: probe a hunch against the corpus.

NOT used by the brief generator — each brief is generated fresh, so we
have no persistent list of patterns to search for. The brief generator
relies on clustered themes + the raw-review fallback when clusters are
thin (see intelligence.get_location_intelligence).
"""

from __future__ import annotations

import logging

from .config import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from .db import get_client as get_db
from .openai import get_client as get_openai

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 10
DEFAULT_MIN_SIMILARITY = 0.0


def search_reviews(
    query: str,
    location_id: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    db=None,
) -> list[dict]:
    """Find the most similar reviews to the query string.

    Args:
        query: natural-language string. Embedded with the same OpenAI model
            used at ingest, so semantic distance is comparable.
        location_id: scope to one location's reviews, or None for network-wide.
        top_n: max reviews returned, sorted by similarity descending.
        min_similarity: floor on cosine similarity in [0, 1]. 0 returns the
            closest N regardless of how close. Higher values (e.g. 0.6) only
            return reviews actually about the query.
        db: reuse an existing Supabase client, or omit to create one.

    Returns a list of dicts with id, location_id, rating, source, posted_at,
    text, similarity — sorted by similarity descending.
    """
    db = db or get_db()
    openai_client = get_openai()

    res = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=query,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    query_embedding = res.data[0].embedding

    rpc_args = {
        "query_embedding": query_embedding,
        "match_count": top_n,
        "min_similarity": min_similarity,
    }
    if location_id is not None:
        rpc_args["target_location_id"] = location_id

    rpc_res = db.rpc("search_reviews_by_embedding", rpc_args).execute()
    return rpc_res.data or []
