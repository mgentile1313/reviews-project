"""Week 2 Day 4 (Part B): per-location prevalence on each network theme.

For each network theme, computes each location's prevalence within the
same rating pass:
- Action themes (1-3★): fraction of this location's 1-3★ reviews
- Working themes (4-5★): fraction of this location's 4-5★ reviews

Same-pass denominator means Part 3's anomaly detection compares apples
to apples — "Mid has 35% of its negatives in the unauthorized-service
theme; network median is 15%" is the kind of statement this enables.

Writes one row per (location, theme) pair into location_theme_scores.
The z_score + direction columns stay NULL — those get filled in Part 3
(compute_anomalies.py).

Re-run safety:
- By default, refuse to insert if scores already exist.
- --replace deletes existing rows before inserting.
- Cluster re-runs (cluster_network.py --replace) auto-cascade-delete
  these scores via the FK on themes(id), so a fresh score run is
  needed after every network re-cluster.

Usage:
    python -m scripts.compute_location_scores              # full run
    python -m scripts.compute_location_scores --dry-run    # counts, no writes
    python -m scripts.compute_location_scores --replace    # overwrite existing
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from statistics import median

from scripts.lib.clustering import PASS_DEFS
from scripts.lib.db import get_client as get_db

log = logging.getLogger(__name__)

# Build rating → pass map once from the canonical PASS_DEFS.
RATING_TO_PASS: dict[int, str] = {}
for _slug, _title, _ratings in PASS_DEFS:
    for _r in _ratings:
        RATING_TO_PASS[_r] = _slug

# PostgREST pagination + batched insert sizes.
FETCH_PAGE = 1000
INSERT_BATCH = 500


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--replace", action="store_true",
        help="Delete existing location_theme_scores rows before inserting.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print summary; no DB writes.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_network_themes(db) -> list[dict]:
    res = (
        db.table("themes")
        .select("id, pass, label, member_review_ids, specific")
        .eq("scope", "network")
        .execute()
    )
    return res.data or []


def fetch_review_locations(db) -> list[dict]:
    """Returns {id, location_id, rating} for every embedded review.

    Lighter than fetching full records — we only need ID, location, and
    rating to compute pass-membership and theme-membership.
    """
    out: list[dict] = []
    last_id: str | None = None
    while True:
        q = (
            db.table("reviews")
            .select("id, location_id, rating")
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


def existing_score_count(db) -> int:
    res = db.table("location_theme_scores").select("id").execute()
    return len(res.data or [])


def delete_all_scores(db) -> int:
    """Wipe location_theme_scores. PostgREST DELETE requires a WHERE clause,
    so we use a tautological filter that matches every row."""
    res = (
        db.table("location_theme_scores")
        .delete()
        .neq("id", "00000000-0000-0000-0000-000000000000")
        .execute()
    )
    return len(res.data or [])


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def compute_scores(themes: list[dict], reviews: list[dict]) -> list[dict]:
    """For each (location, theme): prevalence within same-pass denominator.

    Pre-indexes reviews by (location_id, pass) once, then walks themes ×
    locations. Each location gets a row for every theme — including themes
    where that location has zero members (prevalence 0.0) so the matrix
    is complete for downstream anomaly detection.
    """
    # location_id → pass_slug → list of review_ids in that pass at that location
    by_loc_pass: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in reviews:
        loc_id = r.get("location_id")
        rating = r.get("rating")
        if not loc_id or rating not in RATING_TO_PASS:
            continue
        by_loc_pass[loc_id][RATING_TO_PASS[rating]].append(r["id"])

    location_ids = sorted(by_loc_pass.keys())

    score_rows: list[dict] = []
    for theme in themes:
        pass_slug = theme["pass"]
        member_set = set(theme.get("member_review_ids") or [])
        for loc_id in location_ids:
            same_pass = by_loc_pass[loc_id].get(pass_slug, [])
            denom = len(same_pass)
            in_theme = sum(1 for rid in same_pass if rid in member_set)
            prevalence = (in_theme / denom) if denom > 0 else 0.0
            score_rows.append({
                "location_id": loc_id,
                "theme_id": theme["id"],
                "prevalence": prevalence,
            })
    return score_rows


def summarize_per_theme(themes: list[dict], score_rows: list[dict]) -> None:
    """Log per-theme prevalence stats — useful for sanity-checking the run."""
    by_theme: dict[str, list[float]] = defaultdict(list)
    for row in score_rows:
        by_theme[row["theme_id"]].append(row["prevalence"])

    log.info("Per-theme prevalence (across %d locations):", len(score_rows) // len(themes) if themes else 0)
    for theme in themes:
        vals = by_theme[theme["id"]]
        label = theme.get("label") or "[generic]"
        log.info(
            "  pass=%-7s  median=%5.1f%%  max=%5.1f%%  nonzero=%d/%d  %s",
            theme["pass"], 100 * median(vals), 100 * max(vals),
            sum(1 for v in vals if v > 0), len(vals),
            label[:60],
        )


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

    existing = existing_score_count(db)
    if existing and not args.replace and not args.dry_run:
        log.error(
            "location_theme_scores already has %d rows. Re-run with --replace to overwrite.",
            existing,
        )
        sys.exit(1)

    log.info("fetching network themes...")
    themes = fetch_network_themes(db)
    log.info("found %d network themes", len(themes))
    if not themes:
        log.error("no network themes found — run cluster_network.py first")
        sys.exit(1)

    log.info("fetching review (id, location_id, rating) across the network...")
    reviews = fetch_review_locations(db)
    log.info("fetched %d reviews", len(reviews))

    log.info("computing prevalences...")
    score_rows = compute_scores(themes, reviews)
    log.info(
        "computed %d score rows (%d locations × %d themes)",
        len(score_rows),
        len(score_rows) // len(themes) if themes else 0,
        len(themes),
    )
    summarize_per_theme(themes, score_rows)

    if args.dry_run:
        log.info("DRY RUN — no writes.")
        return

    if args.replace and existing:
        deleted = delete_all_scores(db)
        log.info("deleted %d existing score rows", deleted)

    inserted = failed = 0
    for i in range(0, len(score_rows), INSERT_BATCH):
        chunk = score_rows[i:i + INSERT_BATCH]
        try:
            db.table("location_theme_scores").insert(chunk).execute()
            inserted += len(chunk)
        except Exception as e:
            log.error("batch insert failed (offset %d): %s", i, e)
            failed += len(chunk)

    log.info("=" * 60)
    log.info("Done. Inserted %d score rows, %d failed.", inserted, failed)


if __name__ == "__main__":
    main()
