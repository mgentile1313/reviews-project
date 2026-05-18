"""Embed reviews that don't yet have an embedding.

Pipeline step 2 of 3:  ingest -> embed -> cluster

Usage:
    python -m scripts.ml.embed
"""

from openai import OpenAI

from scripts.lib.config import EMBEDDING_MODEL, OPENAI_API_KEY
from scripts.lib.db import get_client

BATCH_SIZE = 100

client = OpenAI(api_key=OPENAI_API_KEY)


def fetch_unembedded(db) -> list[dict]:
    """Return reviews with no embedding yet."""
    res = (
        db.table("reviews")
        .select("id, body")
        .is_("embedding", "null")
        .limit(BATCH_SIZE)
        .execute()
    )
    return res.data


def main() -> None:
    db = get_client()
    total = 0
    while True:
        batch = fetch_unembedded(db)
        if not batch:
            break
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[r["body"] for r in batch],
        )
        for review, item in zip(batch, resp.data):
            db.table("reviews").update({"embedding": item.embedding}).eq(
                "id", review["id"]
            ).execute()
        total += len(batch)
        print(f"Embedded {total} reviews...")

    print(f"Done. Embedded {total} reviews.")


if __name__ == "__main__":
    main()
