"""Week 2 Day 2: two-pass per-location KMeans clustering, eyeball-only.

Splits a location's embedded reviews by rating bucket — 1-3★ become
action-candidate themes, 4-5★ become what's-working themes — and clusters
each half independently with KMeans. Avoids the volume-asymmetry failure
mode where one pole's cluster budget gets swallowed by the other's mass
(observed at Mid where 6:1 positive-to-negative volume produced 11 positive
clusters and only 1 negative cluster at k=12 single-pass).

For each pass, emits prevalence within the pass, avg rating, and the 3
reviews nearest the centroid as representatives. Writes a single combined
markdown file under evals/clusters/<internal_id>-twopass-k<k>.md.

No DB writes — Day 3 is where Claude labels each cluster and writes the
themes table.

Usage:
    python -m scripts.cluster_reviews --location-id <uuid>
    python -m scripts.cluster_reviews --location-id <uuid> --k 6
    python -m scripts.cluster_reviews --location-id <uuid> --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans

from scripts.lib.clustering import (
    N_INIT,
    PASS_DEFS,
    RANDOM_STATE,
    fit_kmeans,
    nearest_to_centroid,
    parse_embedding,
)
from scripts.lib.config import PROJECT_ROOT
from scripts.lib.db import get_client as get_db

log = logging.getLogger(__name__)

# PostgREST max page size.
FETCH_PAGE = 1000

# Default k when neither --k-actions nor --k-working is set.
DEFAULT_K = 5

# Output formatting.
REP_PER_CLUSTER = 3
SNIPPET_CHARS = 200
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evals" / "clusters"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--location-id", required=True,
        help="Location UUID (must exist in locations.id).",
    )
    p.add_argument(
        "--k", type=int, default=DEFAULT_K,
        help=f"Clusters per pass when --k-actions/--k-working not set (default {DEFAULT_K}).",
    )
    p.add_argument(
        "--k-actions", type=int, default=None,
        help="Clusters for the 1-3★ pass (overrides --k for actions).",
    )
    p.add_argument(
        "--k-working", type=int, default=None,
        help="Clusters for the 4-5★ pass (overrides --k for working).",
    )
    p.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="Where to write the markdown report.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the report but don't write the .md file.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Only cluster N reviews PER PASS (debugging shortcut).",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Fetch — location + paged reviews with embeddings
# ---------------------------------------------------------------------------

def fetch_location(db, location_id: str) -> dict:
    res = (
        db.table("locations")
        .select("id, internal_id, name")
        .eq("id", location_id)
        .execute()
    )
    if not res.data:
        raise SystemExit(f"location {location_id} not found")
    return res.data[0]


def fetch_embedded_reviews(db, location_id: str) -> list[dict]:
    """All reviews for one location where embedding + text are present.

    Cursor-paginated by id for stability — mirrors embed_reviews.fetch_unembedded.
    """
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


# ---------------------------------------------------------------------------
# Format — file-level header + one section per pass
# ---------------------------------------------------------------------------

def _snippet(text: str) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS].rstrip() + "..."


def _review_line(review: dict) -> str:
    date = (review.get("posted_at") or "----")[:10]
    rating = review.get("rating") or "?"
    source = review.get("source") or "?"
    return f"  [{source:6s} {rating}★ {date}] {_snippet(review.get('text'))}"


def build_header(location: dict, totals: dict[str, int], k_per_pass: dict[str, int]) -> str:
    lines = [
        f"# Cluster eyeball (two-pass) — {location['name']}",
        "",
        f"- Location ID: `{location['id']}`",
        f"- Internal ID: `{location['internal_id']}`",
        f"- Reviews fetched: {sum(totals.values())} "
        f"(actions: {totals.get('actions', 0)}, working: {totals.get('working', 0)})",
        f"- k_actions = {k_per_pass['actions']}, k_working = {k_per_pass['working']}, "
        f"random_state={RANDOM_STATE}, n_init={N_INIT}",
        "",
    ]
    return "\n".join(lines)


def build_pass_section(
    title: str,
    reviews: list[dict],
    km: KMeans | None,
    reps: dict[int, list[int]] | None,
    k: int,
) -> str:
    """Format one pass — either KMeans output or a graceful skip note."""
    lines = [f"## {title}", ""]
    if not reviews:
        lines.append("_(no reviews in this rating bucket — skipped)_")
        lines.append("")
        return "\n".join(lines)
    if km is None or reps is None:
        lines.append(f"_(only {len(reviews)} reviews, below k={k} — clustering skipped, listing raw)_")
        lines.append("")
        for r in reviews:
            lines.append(_review_line(r))
        lines.append("")
        return "\n".join(lines)

    lines.append(f"_KMeans inertia: {km.inertia_:.1f}_")
    lines.append("")

    labels = km.labels_
    total = len(reviews)
    sizes = sorted(
        ((c, int((labels == c).sum())) for c in range(k)),
        key=lambda x: -x[1],
    )
    for c, size in sizes:
        prevalence = 100.0 * size / total
        member_idx = np.where(labels == c)[0]
        ratings = [reviews[i].get("rating") for i in member_idx if reviews[i].get("rating") is not None]
        avg_rating = float(np.mean(ratings)) if ratings else float("nan")
        lines.append(
            f"### Cluster {c} — prevalence {prevalence:.1f}%, avg rating {avg_rating:.2f}, n={size}"
        )
        lines.append("")
        for idx in reps[c]:
            lines.append(_review_line(reviews[idx]))
        lines.append("")
    return "\n".join(lines)


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
    location = fetch_location(db, args.location_id)
    log.info("clustering %s (%s)", location["name"], location["internal_id"])

    all_reviews = fetch_embedded_reviews(db, args.location_id)
    if not all_reviews:
        log.error("no embedded reviews found for %s", args.location_id)
        sys.exit(1)
    log.info("fetched %d embedded reviews total", len(all_reviews))

    # Resolve k per pass — explicit flags override the shared --k default.
    k_per_pass = {
        "actions": args.k_actions if args.k_actions is not None else args.k,
        "working": args.k_working if args.k_working is not None else args.k,
    }

    # Partition by rating bucket.
    partitioned: dict[str, list[dict]] = {}
    for slug, _title, ratings in PASS_DEFS:
        bucket = [r for r in all_reviews if r.get("rating") in ratings]
        if args.limit:
            bucket = bucket[: args.limit]
        partitioned[slug] = bucket
        log.info("pass '%s': %d reviews", slug, len(bucket))

    # Build report — header once, then one section per pass.
    totals = {slug: len(rows) for slug, rows in partitioned.items()}
    sections = [build_header(location, totals, k_per_pass)]

    for slug, title, _ratings in PASS_DEFS:
        rows = partitioned[slug]
        k = k_per_pass[slug]
        if not rows or len(rows) < k:
            if rows and len(rows) < k:
                log.warning("pass '%s' has %d reviews, below k=%d — listing without clustering",
                            slug, len(rows), k)
            sections.append(build_pass_section(title, rows, None, None, k))
            continue
        embeddings = np.vstack([parse_embedding(r["embedding"]) for r in rows])
        km = fit_kmeans(embeddings, k)
        reps = nearest_to_centroid(embeddings, km.labels_, km.cluster_centers_, REP_PER_CLUSTER)
        sections.append(build_pass_section(title, rows, km, reps, k))

    report = "\n".join(sections)
    print(report)

    if args.dry_run:
        log.info("dry run — not writing markdown file.")
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = (
        args.output_dir
        / f"{location['internal_id']}-twopass-a{k_per_pass['actions']}-w{k_per_pass['working']}.md"
    )
    out_path.write_text(report + "\n")
    log.info("wrote %s", out_path)


if __name__ == "__main__":
    main()
