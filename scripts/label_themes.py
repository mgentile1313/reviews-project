"""Week 2 Day 3: cluster + label themes (two-stage), persist to themes table.

Pipeline per location:
  1. Cluster reviews via two-pass KMeans (k_actions=6, k_working=8 by default).
  2. Drop clusters with fewer than MIN_CLUSTER_SIZE members.
  3. STAGE 1 — per cluster, Haiku returns up to 3 candidate concrete practices,
     each with a verbatim evidence quote.
  4. STAGE 2 — sequentially per cluster (size-desc within each pass), Haiku
     picks the one candidate that is distinct from previously-picked themes,
     OR returns picked_index=null if every candidate overlaps with what's
     already been chosen.
  5. Insert one themes row per cluster — Stage 2 winner if specific=true,
     else specific=false with the rationale. The full Stage 1 candidate list
     plus Stage 2 pick metadata persists in `candidates_json` for debugging.

Re-run safety:
  - By default, refuse to insert if themes already exist for the target location.
  - --replace deletes existing rows for the location before inserting.
  - Jaccard identity-mapping for monthly refreshes is deferred (post-demo).

Usage:
    python -m scripts.label_themes --location-id <uuid>            # one location
    python -m scripts.label_themes                                  # all locations
    python -m scripts.label_themes --location-id <uuid> --replace
    python -m scripts.label_themes --dry-run                        # cost estimate, no Haiku
"""

from __future__ import annotations

import argparse
import logging

import httpx
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

# Defaults — k_actions bumped to 6 (from 4) to give KMeans more room to split
# the mixed action-side cluster we observed at Park Rd.
DEFAULT_K_ACTIONS = 6
DEFAULT_K_WORKING = 8

# Clusters smaller than this are dropped before labeling — too small to be a
# pattern; severe single-incident outliers surface via a separate path.
MIN_CLUSTER_SIZE = 3

# REP_PER_CLUSTER is imported from lib.labeling so both consumers share it.

# PostgREST pagination.
FETCH_PAGE = 1000

# Haiku 4.5 pricing (per 1M tokens).
PRICE_INPUT_PER_M = 1.00
PRICE_OUTPUT_PER_M = 5.00
PRICE_CACHED_INPUT_PER_M = 0.10

# Token-budget estimates per stage (cost guardrail, not billing-accurate).
EST_STAGE1_INPUT_UNCACHED = 250
EST_STAGE1_INPUT_CACHED = 700      # bigger system prompt now
EST_STAGE1_OUTPUT = 200            # up to 3 candidates with quotes
EST_STAGE2_INPUT_UNCACHED = 250
EST_STAGE2_INPUT_CACHED = 350
EST_STAGE2_OUTPUT = 80


# Stage 1 + Stage 2 system prompts and Haiku call primitives now live in
# scripts/lib/labeling.py — shared with cluster_network.py.


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--location-id", default=None,
        help="Cluster + label one location. Omit to run all locations.",
    )
    p.add_argument(
        "--k-actions", type=int, default=DEFAULT_K_ACTIONS,
        help=f"Clusters for the 1-3★ pass (default {DEFAULT_K_ACTIONS}).",
    )
    p.add_argument(
        "--k-working", type=int, default=DEFAULT_K_WORKING,
        help=f"Clusters for the 4-5★ pass (default {DEFAULT_K_WORKING}).",
    )
    p.add_argument(
        "--replace", action="store_true",
        help="Delete existing themes for the target location(s) before inserting.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show counts and cost estimate; do not call Haiku or write to DB.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Stop after labeling N clusters total (debugging).",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Fetch — locations + paged reviews + existing-theme check
# ---------------------------------------------------------------------------

def fetch_locations(db, location_id: str | None) -> list[dict]:
    q = db.table("locations").select("id, internal_id, name")
    if location_id:
        q = q.eq("id", location_id)
    rows = q.execute().data
    if not rows:
        raise SystemExit(f"no locations matched (location_id={location_id})")
    return rows


def fetch_embedded_reviews(db, location_id: str) -> list[dict]:
    out: list[dict] = []
    last_id: str | None = None
    while True:
        q = (
            db.table("reviews")
            .select("id, source, rating, posted_at, text, embedding")
            .eq("location_id", location_id)
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


def existing_theme_count(db, location_id: str) -> int:
    res = db.table("themes").select("id").eq("location_id", location_id).execute()
    return len(res.data or [])


def delete_themes_for_location(db, location_id: str) -> int:
    res = db.table("themes").delete().eq("location_id", location_id).execute()
    return len(res.data or [])


# ---------------------------------------------------------------------------
# Per-location pipeline
# ---------------------------------------------------------------------------

def cluster_one_location(
    location_id: str,
    reviews: list[dict],
    k_actions: int,
    k_working: int,
) -> list[dict]:
    """Two-pass KMeans + sub-min-size filter. Returns ordered cluster-jobs.

    Each job has: pass, cluster_idx, member_ids, rep_ids, representatives,
    avg_rating, member_count, prevalence. Order: actions clusters by size desc,
    then working clusters by size desc.
    """
    partitioned = partition_by_rating(reviews)
    k_per_pass = {"actions": k_actions, "working": k_working}

    jobs: list[dict] = []
    for slug, _title, _ratings in PASS_DEFS:
        rows = partitioned[slug]
        k = k_per_pass[slug]
        if len(rows) < k:
            log.info("pass '%s' has %d reviews, below k=%d — skipping", slug, len(rows), k)
            continue
        embeddings = np.vstack([parse_embedding(r["embedding"]) for r in rows])
        log.info("fitting KMeans pass=%s (k=%d, n=%d)", slug, k, len(rows))
        km = fit_kmeans(embeddings, k)
        reps = nearest_to_centroid(embeddings, km.labels_, km.cluster_centers_, REP_PER_CLUSTER)

        pass_jobs: list[dict] = []
        for c in range(k):
            member_idx = np.where(km.labels_ == c)[0]
            if member_idx.size < MIN_CLUSTER_SIZE:
                log.info("pass=%s cluster=%d below MIN_CLUSTER_SIZE (%d < %d) — dropped",
                         slug, c, member_idx.size, MIN_CLUSTER_SIZE)
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
        # Sort by size desc within pass — biggest cluster goes first in Stage 2.
        pass_jobs.sort(key=lambda j: -j["member_count"])
        jobs.extend(pass_jobs)
    return jobs


def process_location(db, anthropic_client, location: dict, args) -> dict:
    location_id = location["id"]
    log.info("=== %s (%s) ===", location["name"], location["internal_id"])

    existing = existing_theme_count(db, location_id)
    if existing and not args.replace and not args.dry_run:
        log.warning("skipping %s — %d existing themes; use --replace to overwrite",
                    location["internal_id"], existing)
        return {"location": location["internal_id"], "skipped": True}

    reviews = fetch_embedded_reviews(db, location_id)
    if not reviews:
        log.warning("no embedded reviews for %s", location["internal_id"])
        return {"location": location["internal_id"], "no_reviews": True}

    jobs = cluster_one_location(location_id, reviews, args.k_actions, args.k_working)
    log.info("location %s produced %d clusters to label",
             location["internal_id"], len(jobs))

    if args.dry_run:
        return {"location": location["internal_id"], "clusters_to_label": len(jobs)}

    if args.replace and existing:
        deleted = delete_themes_for_location(db, location_id)
        log.info("deleted %d existing themes for %s", deleted, location["internal_id"])

    # STAGE 1 — independent per cluster, collect candidates.
    for job in jobs:
        candidates = stage1_candidates(
            anthropic_client,
            job["pass"],
            job["member_count"],
            job["avg_rating"],
            job["representatives"],
        )
        if candidates is None:
            job["candidates"] = []
            job["stage1_failed"] = True
        else:
            job["candidates"] = candidates
        log.info("stage1 pass=%s n=%d → %d candidates",
                 job["pass"], job["member_count"], len(job["candidates"]))

    # STAGE 2 — sequential per pass, sees previously-picked labels.
    for pass_slug, _title, _ratings in PASS_DEFS:
        pass_jobs = [j for j in jobs if j["pass"] == pass_slug]
        # Already size-desc from cluster_one_location, but pin for safety.
        pass_jobs.sort(key=lambda j: -j["member_count"])
        previously_picked: list[str] = []
        for job in pass_jobs:
            sel = stage2_select(anthropic_client, job["candidates"], previously_picked)
            job["stage2_pick"] = sel
            picked_idx = sel.get("picked_index")
            if picked_idx is not None and 0 <= picked_idx < len(job["candidates"]):
                previously_picked.append(job["candidates"][picked_idx]["label"])
                log.info("stage2 pass=%s n=%d → picked [%d] %r",
                         pass_slug, job["member_count"], picked_idx,
                         job["candidates"][picked_idx]["label"])
            else:
                log.info("stage2 pass=%s n=%d → no distinct pick (%s)",
                         pass_slug, job["member_count"], sel.get("rationale"))

    # INSERT — one row per cluster.
    inserted = specific_count = generic_count = failed_count = 0
    for job in jobs:
        if args.limit and inserted >= args.limit:
            break
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

        candidates_json_blob = {
            "stage1_candidates": candidates,
            "stage2_pick": sel,
        }

        row = {
            "location_id": location_id,
            "scope": "location",
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
            "candidates_json": candidates_json_blob,
        }
        try:
            db.table("themes").insert(row).execute()
            inserted += 1
            specific_count += 1 if specific else 0
            generic_count += 0 if specific else 1
        except Exception as e:
            log.error(
                "insert failed for %s pass=%s cluster=%d: %s",
                location["internal_id"], job["pass"], job["cluster_idx"], e,
            )
            failed_count += 1

    return {
        "location": location["internal_id"],
        "inserted": inserted,
        "specific": specific_count,
        "generic": generic_count,
        "failed": failed_count,
    }


# ---------------------------------------------------------------------------
# Cost estimate (two stages now)
# ---------------------------------------------------------------------------

def estimate_cost(total_clusters: int) -> dict:
    if total_clusters == 0:
        return {"calls": 0, "estimated_cost_usd": 0.0}

    # Stage 1: one call per cluster. First call's system prompt is uncached,
    # subsequent calls hit cache.
    s1_uncached_input = total_clusters * EST_STAGE1_INPUT_UNCACHED + EST_STAGE1_INPUT_CACHED
    s1_cached_input = max(0, total_clusters - 1) * EST_STAGE1_INPUT_CACHED
    s1_output = total_clusters * EST_STAGE1_OUTPUT

    # Stage 2: one call per cluster. Different system prompt — own cache lineage.
    s2_uncached_input = total_clusters * EST_STAGE2_INPUT_UNCACHED + EST_STAGE2_INPUT_CACHED
    s2_cached_input = max(0, total_clusters - 1) * EST_STAGE2_INPUT_CACHED
    s2_output = total_clusters * EST_STAGE2_OUTPUT

    uncached_input = s1_uncached_input + s2_uncached_input
    cached_input = s1_cached_input + s2_cached_input
    output = s1_output + s2_output

    cost = (
        uncached_input / 1_000_000 * PRICE_INPUT_PER_M
        + cached_input / 1_000_000 * PRICE_CACHED_INPUT_PER_M
        + output / 1_000_000 * PRICE_OUTPUT_PER_M
    )
    return {
        "calls": total_clusters * 2,  # stage 1 + stage 2
        "uncached_input_tokens": uncached_input,
        "cached_input_tokens": cached_input,
        "output_tokens": output,
        "estimated_cost_usd": round(cost, 4),
    }


# ---------------------------------------------------------------------------
# Per-location error handling — retry transients once, skip everything else
# ---------------------------------------------------------------------------

def _is_transient_error(e: BaseException) -> bool:
    """Network-level errors that might succeed on retry.

    Catches httpx timeout/network errors directly, plus anything from the
    Anthropic SDK whose class name suggests transience (Timeout, Connection).
    """
    if isinstance(e, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    cls_name = type(e).__name__
    if "Timeout" in cls_name or "Connection" in cls_name:
        return True
    return False


def process_location_with_retry(db, anthropic_client, location: dict, args) -> dict:
    """Wrap process_location with a single retry on transient errors.

    - Success first try: return summary normally.
    - Transient error first try (network timeout, connection error): retry once.
    - Two transient errors in a row: log and return an error summary.
    - Non-transient error: log and return an error summary immediately (no retry).
    """
    for attempt in (1, 2):
        try:
            return process_location(db, anthropic_client, location, args)
        except Exception as e:
            transient = _is_transient_error(e)
            if attempt == 1 and transient:
                log.warning(
                    "transient error on %s (attempt 1), retrying immediately: %s: %s",
                    location["internal_id"], type(e).__name__, e,
                )
                continue
            log.error(
                "%s error on %s after %d attempt(s) — skipping location: %s: %s",
                "two-strikes transient" if transient else "non-transient",
                location["internal_id"], attempt, type(e).__name__, e,
            )
            return {
                "location": location["internal_id"],
                "error": True,
                "error_type": type(e).__name__,
                "error_message": str(e),
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

    locations = fetch_locations(db, args.location_id)
    log.info("processing %d location(s)", len(locations))

    summaries = []
    for loc in locations:
        summary = process_location_with_retry(db, anthropic_client, loc, args)
        summaries.append(summary)

    log.info("=" * 60)
    if args.dry_run:
        total = sum(s.get("clusters_to_label", 0) for s in summaries)
        est = estimate_cost(total)
        log.info("DRY RUN — no Haiku calls or DB writes.")
        log.info(
            "Clusters that would be labeled: %d across %d location(s). "
            "~%d Haiku calls (Stage 1 + Stage 2).",
            total, len(locations), est["calls"],
        )
        log.info(
            "Estimated cost: $%.4f (uncached input: %d tok, cached: %d tok, output: %d tok)",
            est["estimated_cost_usd"],
            est.get("uncached_input_tokens", 0),
            est.get("cached_input_tokens", 0),
            est.get("output_tokens", 0),
        )
        return

    skipped = sum(1 for s in summaries if s.get("skipped"))
    errored = [s for s in summaries if s.get("error")]
    inserted = sum(s.get("inserted", 0) for s in summaries)
    specific = sum(s.get("specific", 0) for s in summaries)
    generic = sum(s.get("generic", 0) for s in summaries)
    failed = sum(s.get("failed", 0) for s in summaries)
    log.info(
        "Done. Locations processed: %d, skipped: %d, errored: %d. "
        "Themes inserted: %d (specific=%d, generic=%d). Failed inserts: %d.",
        len(locations) - skipped - len(errored), skipped, len(errored),
        inserted, specific, generic, failed,
    )
    if errored:
        log.info("Locations that errored (re-run to retry):")
        for s in errored:
            log.info("  - %s: %s — %s", s["location"], s["error_type"], s["error_message"])


if __name__ == "__main__":
    main()
