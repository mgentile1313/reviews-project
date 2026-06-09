"""Week 2 Day 4: network-wide theme clustering + labeling.

Pools all embedded reviews across the network, partitions by rating
(1-3★ → action candidates, 4-5★ → what's-working candidates), clusters
each pool with KMeans, and applies the same two-stage Haiku labeling
used at the per-location level (scripts/label_themes.py).

What's different from label_themes.py:
- Single pool spanning all locations, not per-location.
- Bigger k (k_actions=6, k_working=10 by default) and bigger min_cluster_size
  (N_active_locations for actions, 2N for working) — encodes "a network
  theme should average ≥1 (or ≥2) reviews per location to qualify."
- themes rows are written with scope='network', location_id=NULL.
- Per-location prevalence into location_theme_scores is a separate step
  (scripts/compute_location_scores.py, written next).

Re-run safety:
- By default, refuse to insert if network themes already exist.
- --replace deletes existing scope='network' rows before inserting.

Usage:
    python -m scripts.cluster_network                    # full run
    python -m scripts.cluster_network --dry-run          # estimate, no calls
    python -m scripts.cluster_network --replace          # overwrite existing
"""

from __future__ import annotations

import argparse
import logging
import sys

import numpy as np

from scripts.lib.anthropic import get_client as get_anthropic
from scripts.lib.clustering import (
    PASS_DEFS,
    fit_kmeans,
    nearest_to_centroid,
    parse_embedding,
    partition_by_rating,
)
from scripts.lib.db import get_client as get_db
from scripts.lib.labeling import REP_PER_CLUSTER, stage1_candidates, stage2_select

log = logging.getLogger(__name__)

# Defaults — network-pool clustering uses more cluster budget than per-location
# because the pools are 50-100x bigger and span more sub-themes.
DEFAULT_K_ACTIONS_NETWORK = 6
DEFAULT_K_WORKING_NETWORK = 10

# min_cluster_size = N_active_locations * multiplier.
# Actions: 1x → "theme should average ≥1 review per location"
# Working: 2x → broader bar (positive pool is ~5-6x bigger; need a higher
#                floor to filter generic-positive mass clusters)
ACTIONS_MIN_MULTIPLIER = 1
WORKING_MIN_MULTIPLIER = 2

# PostgREST pagination.
FETCH_PAGE = 1000

# Haiku 4.5 pricing (per 1M tokens).
PRICE_INPUT_PER_M = 1.00
PRICE_OUTPUT_PER_M = 5.00
PRICE_CACHED_INPUT_PER_M = 0.10

# Token-budget estimates per stage (cost guardrail, not billing-accurate).
EST_STAGE1_INPUT_UNCACHED = 250
EST_STAGE1_INPUT_CACHED = 700
EST_STAGE1_OUTPUT = 200
EST_STAGE2_INPUT_UNCACHED = 250
EST_STAGE2_INPUT_CACHED = 350
EST_STAGE2_OUTPUT = 80


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--k-actions", type=int, default=DEFAULT_K_ACTIONS_NETWORK,
        help=f"Clusters for the network 1-3★ pool (default {DEFAULT_K_ACTIONS_NETWORK}).",
    )
    p.add_argument(
        "--k-working", type=int, default=DEFAULT_K_WORKING_NETWORK,
        help=f"Clusters for the network 4-5★ pool (default {DEFAULT_K_WORKING_NETWORK}).",
    )
    p.add_argument(
        "--replace", action="store_true",
        help="Delete existing network themes before inserting.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show counts and cost estimate; no Haiku calls, no DB writes.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_all_embedded_reviews(db) -> list[dict]:
    """Cursor-paged through every embedded review across all locations."""
    out: list[dict] = []
    last_id: str | None = None
    while True:
        q = (
            db.table("reviews")
            .select("id, location_id, source, rating, posted_at, text, embedding")
            .filter("embedding", "not.is", "null")
            .filter("text", "not.is", "null")
            .order("id")
            .limit(FETCH_PAGE)
        )
        if last_id is not None:
            q = q.gt("id", last_id)
        rows = q.execute().data
        if not rows:
            break
        out.extend(rows)
        last_id = rows[-1]["id"]
        if len(rows) < FETCH_PAGE:
            break
    return out


def active_location_count(reviews: list[dict]) -> int:
    """N distinct locations with at least one embedded review.

    Counted directly from the fetched pool rather than a separate query —
    semantically equivalent to "count(distinct location_id) from reviews
    where embedding is not null" and avoids a redundant round-trip.
    """
    return len({r["location_id"] for r in reviews if r.get("location_id")})


def existing_network_theme_count(db) -> int:
    res = db.table("themes").select("id").eq("scope", "network").execute()
    return len(res.data or [])


def delete_network_themes(db) -> int:
    res = db.table("themes").delete().eq("scope", "network").execute()
    return len(res.data or [])


# ---------------------------------------------------------------------------
# Cluster — two-pass KMeans + per-pass min-size filter
# ---------------------------------------------------------------------------

def cluster_network(
    reviews: list[dict],
    k_actions: int,
    k_working: int,
    min_actions: int,
    min_working: int,
) -> list[dict]:
    """Two-pass KMeans on the full network pool.

    Returns ordered cluster jobs (actions first by size desc, then working
    by size desc). Each job has the same shape as label_themes' jobs so it
    can flow into the same Stage 1/Stage 2 pipeline.
    """
    partitioned = partition_by_rating(reviews)
    config = {
        "actions": (k_actions, min_actions),
        "working": (k_working, min_working),
    }

    jobs: list[dict] = []
    for slug, _title, _ratings in PASS_DEFS:
        rows = partitioned[slug]
        k, min_size = config[slug]
        if len(rows) < k:
            log.info("pass '%s' has %d reviews, below k=%d — skipping pass", slug, len(rows), k)
            continue
        embeddings = np.vstack([parse_embedding(r["embedding"]) for r in rows])
        log.info(
            "fitting KMeans pass=%s (k=%d, n=%d, min_cluster_size=%d)",
            slug, k, len(rows), min_size,
        )
        km = fit_kmeans(embeddings, k)
        reps = nearest_to_centroid(embeddings, km.labels_, km.cluster_centers_, REP_PER_CLUSTER)

        pass_jobs: list[dict] = []
        for c in range(k):
            member_idx = np.where(km.labels_ == c)[0]
            if member_idx.size < min_size:
                log.info(
                    "pass=%s cluster=%d below min_cluster_size (%d < %d) — dropped",
                    slug, c, member_idx.size, min_size,
                )
                continue
            member_ids = [rows[i]["id"] for i in member_idx]
            rep_ids = [rows[i]["id"] for i in reps[c]]
            representatives = [rows[i] for i in reps[c]]
            ratings = [
                rows[i].get("rating") for i in member_idx
                if rows[i].get("rating") is not None
            ]
            avg_rating = float(np.mean(ratings)) if ratings else 0.0
            prevalence = float(member_idx.size) / len(rows)
            pass_jobs.append({
                "pass": slug,
                "cluster_idx": c,
                "member_ids": member_ids,
                "rep_ids": rep_ids,
                "representatives": representatives,
                "avg_rating": avg_rating,
                "member_count": int(member_idx.size),
                "prevalence": prevalence,
            })
        pass_jobs.sort(key=lambda j: -j["member_count"])
        jobs.extend(pass_jobs)
    return jobs


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------

def estimate_cost(total_clusters: int) -> dict:
    if total_clusters == 0:
        return {"calls": 0, "estimated_cost_usd": 0.0}
    s1_unc = total_clusters * EST_STAGE1_INPUT_UNCACHED + EST_STAGE1_INPUT_CACHED
    s1_cac = max(0, total_clusters - 1) * EST_STAGE1_INPUT_CACHED
    s1_out = total_clusters * EST_STAGE1_OUTPUT
    s2_unc = total_clusters * EST_STAGE2_INPUT_UNCACHED + EST_STAGE2_INPUT_CACHED
    s2_cac = max(0, total_clusters - 1) * EST_STAGE2_INPUT_CACHED
    s2_out = total_clusters * EST_STAGE2_OUTPUT
    uncached = s1_unc + s2_unc
    cached = s1_cac + s2_cac
    output = s1_out + s2_out
    cost = (
        uncached / 1_000_000 * PRICE_INPUT_PER_M
        + cached / 1_000_000 * PRICE_CACHED_INPUT_PER_M
        + output / 1_000_000 * PRICE_OUTPUT_PER_M
    )
    return {
        "calls": total_clusters * 2,
        "uncached_input": uncached,
        "cached_input": cached,
        "output": output,
        "estimated_cost_usd": round(cost, 4),
    }


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
    anthropic_client = None if args.dry_run else get_anthropic()

    existing = existing_network_theme_count(db)
    if existing and not args.replace and not args.dry_run:
        log.error(
            "network themes already exist (%d rows). Re-run with --replace to overwrite.",
            existing,
        )
        sys.exit(1)

    log.info("fetching all embedded reviews across the network...")
    reviews = fetch_all_embedded_reviews(db)
    log.info("fetched %d reviews", len(reviews))
    if not reviews:
        log.error("no embedded reviews found")
        sys.exit(1)

    loc_count = active_location_count(reviews)
    min_actions = loc_count * ACTIONS_MIN_MULTIPLIER
    min_working = loc_count * WORKING_MIN_MULTIPLIER
    log.info(
        "active locations: %d → min_cluster_size: actions=%d (%dx), working=%d (%dx)",
        loc_count, min_actions, ACTIONS_MIN_MULTIPLIER, min_working, WORKING_MIN_MULTIPLIER,
    )

    jobs = cluster_network(reviews, args.k_actions, args.k_working, min_actions, min_working)
    log.info("network produced %d clusters to label", len(jobs))

    if args.dry_run:
        est = estimate_cost(len(jobs))
        log.info("DRY RUN — no Haiku calls or DB writes.")
        log.info(
            "Clusters that would be labeled: %d. ~%d Haiku calls. Estimated cost: $%.4f",
            len(jobs), est["calls"], est["estimated_cost_usd"],
        )
        return

    if args.replace and existing:
        deleted = delete_network_themes(db)
        log.info("deleted %d existing network themes", deleted)

    # STAGE 1 — candidates per cluster.
    for job in jobs:
        candidates = stage1_candidates(
            anthropic_client,
            job["pass"],
            job["member_count"],
            job["avg_rating"],
            job["representatives"],
        )
        job["candidates"] = candidates if candidates is not None else []
        log.info(
            "stage1 pass=%s n=%d → %d candidates",
            job["pass"], job["member_count"], len(job["candidates"]),
        )

    # STAGE 2 — sequential per pass.
    for pass_slug, _title, _ratings in PASS_DEFS:
        pass_jobs = [j for j in jobs if j["pass"] == pass_slug]
        pass_jobs.sort(key=lambda j: -j["member_count"])
        previously_picked: list[str] = []
        for job in pass_jobs:
            sel = stage2_select(anthropic_client, job["candidates"], previously_picked)
            job["stage2_pick"] = sel
            picked_idx = sel.get("picked_index")
            if picked_idx is not None and 0 <= picked_idx < len(job["candidates"]):
                previously_picked.append(job["candidates"][picked_idx]["label"])
                log.info(
                    "stage2 pass=%s n=%d → picked [%d] %r",
                    pass_slug, job["member_count"], picked_idx,
                    job["candidates"][picked_idx]["label"],
                )
            else:
                log.info(
                    "stage2 pass=%s n=%d → no distinct pick (%s)",
                    pass_slug, job["member_count"], sel.get("rationale"),
                )

    # INSERT — one row per cluster, scope='network', location_id=NULL.
    inserted = specific_count = generic_count = failed = 0
    for job in jobs:
        candidates = job["candidates"]
        sel = job["stage2_pick"]
        picked_idx = sel.get("picked_index")

        if picked_idx is not None and 0 <= picked_idx < len(candidates):
            picked = candidates[picked_idx]
            specific = True
            label = picked["label"]
            evidence_quote = picked["evidence_quote"]
            rejection_reason = None
        else:
            specific = False
            label = None
            evidence_quote = None
            rejection_reason = sel.get("rationale") or "no specific theme"

        row = {
            "location_id": None,
            "scope": "network",
            "pass": job["pass"],
            "specific": specific,
            "label": label,
            "evidence_quote": evidence_quote,
            "rejection_reason": rejection_reason,
            "representative_review_ids": job["rep_ids"],
            "member_review_ids": job["member_ids"],
            "prevalence": job["prevalence"],
            "avg_rating": job["avg_rating"],
            "member_count": job["member_count"],
            "candidates_json": {
                "stage1_candidates": candidates,
                "stage2_pick": sel,
            },
        }
        try:
            db.table("themes").insert(row).execute()
            inserted += 1
            specific_count += 1 if specific else 0
            generic_count += 0 if specific else 1
        except Exception as e:
            log.error(
                "insert failed for pass=%s cluster=%d: %s",
                job["pass"], job["cluster_idx"], e,
            )
            failed += 1

    log.info("=" * 60)
    log.info(
        "Done. Network themes inserted: %d (specific=%d, generic=%d). Failed inserts: %d.",
        inserted, specific_count, generic_count, failed,
    )


if __name__ == "__main__":
    main()
