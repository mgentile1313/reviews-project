"""Cluster embedded reviews for a source into themes.

Pipeline step 3 of 3:  ingest -> embed -> cluster

Usage:
    python -m scripts.ml.cluster <source_id> [n_clusters]
"""

import sys

import numpy as np
from sklearn.cluster import KMeans

from scripts.lib.db import get_client

DEFAULT_K = 8


def load_embeddings(db, source_id: str) -> tuple[list[str], np.ndarray]:
    """Return (review_ids, embedding matrix) for one source."""
    res = (
        db.table("reviews")
        .select("id, embedding")
        .eq("source_id", source_id)
        .not_.is_("embedding", "null")
        .execute()
    )
    ids = [r["id"] for r in res.data]
    matrix = np.array([r["embedding"] for r in res.data], dtype=np.float32)
    return ids, matrix


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m scripts.ml.cluster <source_id> [n_clusters]")
    source_id = sys.argv[1]
    k = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_K

    db = get_client()
    ids, matrix = load_embeddings(db, source_id)
    if len(ids) < k:
        sys.exit(f"Only {len(ids)} embedded reviews; need at least {k}.")

    labels = KMeans(n_clusters=k, n_init="auto", random_state=42).fit_predict(matrix)

    for review_id, label in zip(ids, labels):
        db.table("reviews").update({"cluster_id": int(label)}).eq(
            "id", review_id
        ).execute()

    for label in range(k):
        size = int((labels == label).sum())
        db.table("clusters").upsert(
            {"id": label, "source_id": source_id, "size": size}
        ).execute()

    print(f"Assigned {len(ids)} reviews to {k} clusters.")
    # TODO: generate a brief per cluster with Claude (scripts/ml/brief.py).


if __name__ == "__main__":
    main()
