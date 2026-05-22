"""Phase C: embed reviews with OpenAI text-embedding-3-large (truncated to 1536d).

Reads reviews from Supabase where embedding IS NULL and text IS NOT NULL,
batches them through OpenAI's embeddings API, and writes the 1536-dim vectors
back to `reviews.embedding`.

Resumable: the `WHERE embedding IS NULL` filter naturally picks up where a
crashed/interrupted run left off. Re-running is safe.

Cost: text-embedding-3-large is $0.13 per 1M input tokens. Our ~18.6K reviews
at ~100 tokens each ≈ $0.25 total. `--dry-run` prints an estimate before any
API calls.

Usage:
    python -m scripts.embed_reviews                 # embed all unembedded
    python -m scripts.embed_reviews --limit 100     # only N (testing)
    python -m scripts.embed_reviews --dry-run       # count + estimate cost, no calls
"""

from __future__ import annotations

import argparse
import logging
import time

from scripts.lib.config import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from scripts.lib.db import get_client as get_db
from scripts.lib.openai import get_client as get_openai

# OpenAI batch size — 250 inputs per embeddings.create call.
BATCH_SIZE = 250

# text-embedding-3-large pricing, $ per 1M input tokens.
PRICE_PER_M_TOKENS = 0.13

# PostgREST default max-rows per query.
FETCH_PAGE = 1000

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost estimate (rough — for guardrail only, not billing-accurate)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """~4 chars per token for English. Good enough for a cost guardrail."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit", type=int, default=None,
        help="Process only N reviews (default: all unembedded). Useful for testing.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Count what would be embedded and estimate cost; do not call OpenAI.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Fetch — paginated via id cursor (stable across concurrent embedding updates)
# ---------------------------------------------------------------------------

def fetch_unembedded(db, *, limit: int | None) -> list[dict]:
    """All reviews where embedding IS NULL and text IS NOT NULL, ordered by id.

    Uses cursor-based pagination (id > last_id) rather than offset-based, so
    rows updated mid-fetch by a concurrent worker don't cause us to skip
    anything.
    """
    out: list[dict] = []
    last_id: str | None = None
    while True:
        remaining = (limit - len(out)) if limit is not None else None
        if remaining is not None and remaining <= 0:
            break
        page_size = min(FETCH_PAGE, remaining) if remaining is not None else FETCH_PAGE

        q = (
            db.table("reviews")
            .select("id, text")
            .is_("embedding", "null")
            .filter("text", "not.is", "null")
            .order("id")
            .limit(page_size)
        )
        if last_id is not None:
            q = q.gt("id", last_id)

        rows = q.execute().data
        if not rows:
            break
        out.extend(rows)
        last_id = rows[-1]["id"]
        if len(rows) < page_size:
            break  # last page
    return out


# ---------------------------------------------------------------------------
# Embed — one chunk per OpenAI call, retry once on transient errors
# ---------------------------------------------------------------------------

def embed_chunk(openai_client, texts: list[str]) -> list[list[float]] | None:
    """One OpenAI embeddings.create call. Returns N vectors in input order,
    or None if the call failed after one retry."""
    for attempt in (1, 2):
        try:
            res = openai_client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
                dimensions=EMBEDDING_DIMENSIONS,
            )
            # OpenAI returns data with explicit `index`; sort defensively in case.
            return [d.embedding for d in sorted(res.data, key=lambda d: d.index)]
        except Exception as e:
            if attempt == 1:
                log.warning("embed_chunk failed (attempt 1), retrying in 2s: %s", e)
                time.sleep(2)
                continue
            log.error("embed_chunk failed after retries: %s", e)
            return None


def write_embedding(db, row_id: str, vec: list[float], *, retries: int = 2) -> bool:
    """One UPDATE per row, retry on transient network errors.
    Returns True on success; on permanent failure the row stays NULL and
    will be picked up on the next run."""
    for attempt in range(retries + 1):
        try:
            db.table("reviews").update({"embedding": vec}).eq("id", row_id).execute()
            return True
        except Exception as e:
            if attempt < retries:
                log.warning("write_embedding %s failed (attempt %d), retrying in 1s: %s",
                            row_id, attempt + 1, e)
                time.sleep(1)
                continue
            log.error("write_embedding %s failed after %d attempts: %s",
                      row_id, attempt + 1, e)
            return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    db = get_db()
    log.info("fetching unembedded reviews...")
    rows = fetch_unembedded(db, limit=args.limit)
    log.info("found %d unembedded reviews with text", len(rows))

    if not rows:
        log.info("nothing to embed.")
        return

    # Cost guardrail
    total_tokens = sum(estimate_tokens(r["text"]) for r in rows)
    est_cost = total_tokens / 1_000_000 * PRICE_PER_M_TOKENS
    log.info(
        "estimated cost: ~$%.4f (%s tokens × $%.2f / 1M, model=%s @ %dd)",
        est_cost, f"{total_tokens:,}", PRICE_PER_M_TOKENS, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS,
    )

    if args.dry_run:
        log.info("DRY RUN — no API calls, no writes.")
        return

    openai_client = get_openai()
    chunks = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    log.info("processing %d chunks of up to %d", len(chunks), BATCH_SIZE)

    embedded = 0
    skipped_chunks = 0
    failed_writes = 0
    for ci, chunk in enumerate(chunks, 1):
        vectors = embed_chunk(openai_client, [r["text"] for r in chunk])
        if vectors is None:
            skipped_chunks += 1
            log.warning("chunk %d/%d: skipped (failed after retry)", ci, len(chunks))
            continue
        # PostgREST has no per-row bulk-update primitive, so we issue one UPDATE per row.
        # Each write is retried independently so a single transient network blip
        # doesn't kill the whole run; failed rows stay NULL and are re-tried on re-run.
        chunk_written = 0
        for r, vec in zip(chunk, vectors):
            if write_embedding(db, r["id"], vec):
                chunk_written += 1
            else:
                failed_writes += 1
        embedded += chunk_written
        log.info("chunk %d/%d: +%d embedded (%d failed writes; running %d/%d)",
                 ci, len(chunks), chunk_written, len(chunk) - chunk_written,
                 embedded, len(rows))

    # Summary
    print()
    print("=== embed_reviews summary ===")
    print(f"  candidates (unembedded with text):    {len(rows):>6}")
    print(f"  successfully embedded:                {embedded:>6}")
    print(f"  chunks skipped (failed after retry):  {skipped_chunks:>6}")
    print(f"  individual row writes failed:         {failed_writes:>6}")
    print(f"  estimated cost (rough):               ~${est_cost:.4f}")


if __name__ == "__main__":
    main()
