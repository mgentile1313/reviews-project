"""Clustering primitives shared between the eyeball script and the labeling script.

Two-pass KMeans on rating-partitioned reviews:
- 1-3★ → action-candidate themes
- 4-5★ → what's-working themes

KMeans optimizes inertia globally, which under volume asymmetry (e.g. Mid is
6:1 positive-to-negative) starves the minority polarity. Partitioning by rating
before clustering gives each polarity its own cluster budget.
"""

from __future__ import annotations

import numpy as np
from sklearn.cluster import KMeans

# Rating buckets — 1-3★ → action candidates, 4-5★ → what's working.
# Tuple order is the canonical pass order in all output.
PASS_DEFS: list[tuple[str, str, set[int]]] = [
    ("actions",  "Action-candidate clusters (1-3★)",   {1, 2, 3}),
    ("working",  "What's-working clusters (4-5★)",     {4, 5}),
]

# KMeans config.
RANDOM_STATE = 42      # pinned for reproducibility
N_INIT = 10            # 10 random centroid seeds, keep the best partition


def parse_embedding(raw) -> np.ndarray:
    """pgvector via PostgREST commonly serializes as a string '[x,y,...]'.
    Handle both string and list forms defensively."""
    if isinstance(raw, str):
        return np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
    return np.asarray(raw, dtype=np.float32)


def fit_kmeans(embeddings: np.ndarray, k: int) -> KMeans:
    """Fit KMeans with our pinned config."""
    km = KMeans(n_clusters=k, random_state=RANDOM_STATE, n_init=N_INIT)
    km.fit(embeddings)
    return km


def nearest_to_centroid(
    embeddings: np.ndarray,
    labels: np.ndarray,
    centroids: np.ndarray,
    n_rep: int,
) -> dict[int, list[int]]:
    """For each cluster c, return indices of the n_rep reviews closest to centroid c.

    Indices are positions in the original `embeddings` / `reviews` array, not
    member-only indices.
    """
    reps: dict[int, list[int]] = {}
    for c in range(centroids.shape[0]):
        member_idx = np.where(labels == c)[0]
        if member_idx.size == 0:
            reps[c] = []
            continue
        dists = np.linalg.norm(embeddings[member_idx] - centroids[c], axis=1)
        reps[c] = member_idx[np.argsort(dists)[:n_rep]].tolist()
    return reps


def partition_by_rating(reviews: list[dict]) -> dict[str, list[dict]]:
    """Split reviews into the rating buckets defined by PASS_DEFS.

    Reviews with rating not in any bucket (e.g. NULL) are dropped.
    """
    out: dict[str, list[dict]] = {slug: [] for slug, _title, _ratings in PASS_DEFS}
    for r in reviews:
        rating = r.get("rating")
        for slug, _title, ratings in PASS_DEFS:
            if rating in ratings:
                out[slug].append(r)
                break
    return out
