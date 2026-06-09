"""Week 2 Day 5: anomaly detection — z-scores and direction in location_theme_scores.

For each network theme, computes the prevalence median and IQR across the
network. For each (location, theme), computes a robust z-score using
(prev - median) / IQR, sets direction based on sign, and applies a
minimum-pool guard so tiny-pool locations don't throw false anomalies
(Houston has 2 negative reviews; a 50% prevalence is statistical noise,
not a 3-sigma outlier).

Pure compute — no Haiku, no embeddings. Reads location_theme_scores (the
prevalence column is already populated by compute_location_scores.py),
reads reviews for the same-pass pool sizes, writes back z_score + direction.

Re-run safety:
- Upserts on (location_id, theme_id) so re-runs cleanly overwrite the
  z_score and direction columns without affecting prevalence.

Usage:
    python -m scripts.compute_anomalies                # full run
    python -m scripts.compute_anomalies --dry-run      # compute + print, no writes
    python -m scripts.compute_anomalies --min-pool 15  # tighter pool guard
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from statistics import median

from scripts.lib.clustering import PASS_DEFS
from scripts.lib.db import get_client as get_db

log = logging.getLogger(__name__)

# Same-pass pool minimum for a location to get a z-score. Below this,
# z_score and direction stay NULL — the prevalence row stays in the
# table but we don't claim statistical signal.
DEFAULT_MIN_POOL = 10

FETCH_PAGE = 1000
UPSERT_BATCH = 500

# rating → pass slug from canonical PASS_DEFS
RATING_TO_PASS: dict[int, str] = {}
for _slug, _title, _ratings in PASS_DEFS:
    for _r in _ratings:
        RATING_TO_PASS[_r] = _slug


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--min-pool", type=int, default=DEFAULT_MIN_POOL,
        help=f"Minimum same-pass pool size for a z-score (default {DEFAULT_MIN_POOL}).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print the top anomalies per theme; no writes.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_scores_with_theme_pass(db) -> list[dict]:
    """Return scores joined with their theme's pass (since we score per pass)."""
    scores = db.table("location_theme_scores").select(
        "id, location_id, theme_id, prevalence"
    ).execute().data or []
    themes = db.table("themes").select(
        "id, pass, label"
    ).eq("scope", "network").execute().data or []
    theme_meta = {t["id"]: t for t in themes}
    out: list[dict] = []
    for s in scores:
        meta = theme_meta.get(s["theme_id"])
        if not meta:
            continue
        s["theme_pass"] = meta["pass"]
        s["theme_label"] = meta.get("label") or "[generic]"
        out.append(s)
    return out


def fetch_pool_sizes(db) -> dict[tuple[str, str], int]:
    """{(location_id, pass): n_embedded_reviews_in_that_pass}."""
    out: dict[tuple[str, str], int] = defaultdict(int)
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
        for r in rows:
            rating = r.get("rating")
            loc = r.get("location_id")
            if rating not in RATING_TO_PASS or not loc:
                continue
            out[(loc, RATING_TO_PASS[rating])] += 1
        last_id = rows[-1]["id"]
        if len(rows) < FETCH_PAGE:
            break
    return out


def fetch_location_names(db) -> dict[str, str]:
    res = db.table("locations").select("id, internal_id").execute()
    return {l["id"]: l["internal_id"] for l in res.data or []}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def iqr(values: list[float]) -> float:
    """Q3 − Q1, type-7 quantile (matches numpy default). Returns 0 for n < 4."""
    n = len(values)
    if n < 4:
        return 0.0
    sorted_v = sorted(values)

    def q(p: float) -> float:
        h = p * (n - 1)
        lo = int(h)
        frac = h - lo
        if lo + 1 < n:
            return sorted_v[lo] + frac * (sorted_v[lo + 1] - sorted_v[lo])
        return sorted_v[lo]

    return q(0.75) - q(0.25)


def compute_anomalies(
    scores: list[dict],
    pool_sizes: dict[tuple[str, str], int],
    min_pool: int,
) -> tuple[list[dict], dict[str, tuple[float, float]], int, int]:
    """Per theme, compute median + IQR over all locations' prevalences.
    Per (location, theme), compute z-score = (prev - median) / IQR if IQR > 0.

    Returns:
      - upsert rows (one per score, with new z_score and direction)
      - theme stats dict {theme_id: (median, iqr)} for diagnostics
      - n_scored, n_guarded counts
    """
    # Group prevalences by theme
    by_theme: dict[str, list[float]] = defaultdict(list)
    for s in scores:
        by_theme[s["theme_id"]].append(s["prevalence"] or 0.0)

    theme_stats: dict[str, tuple[float, float]] = {}
    for tid, vals in by_theme.items():
        med = median(vals) if vals else 0.0
        spread = iqr(vals)
        theme_stats[tid] = (med, spread)

    upserts: list[dict] = []
    n_scored = n_guarded = 0
    for s in scores:
        med, spread = theme_stats[s["theme_id"]]
        pool = pool_sizes.get((s["location_id"], s["theme_pass"]), 0)
        prev = s["prevalence"] or 0.0

        if pool < min_pool:
            z = None
            direction = None
            n_guarded += 1
        elif spread == 0:
            # All locations equal on this theme — no anomaly signal possible.
            z = 0.0
            direction = None
            n_scored += 1
        else:
            z = (prev - med) / spread
            if z > 0:
                direction = "above"
            elif z < 0:
                direction = "below"
            else:
                direction = None
            n_scored += 1

        upserts.append({
            "location_id": s["location_id"],
            "theme_id": s["theme_id"],
            "prevalence": prev,
            "z_score": z,
            "direction": direction,
        })

    return upserts, theme_stats, n_scored, n_guarded


# ---------------------------------------------------------------------------
# Sanity print
# ---------------------------------------------------------------------------

def show_top_anomalies(
    upserts: list[dict],
    theme_stats: dict[str, tuple[float, float]],
    scores_indexed_by_theme: dict[str, list[dict]],
    loc_names: dict[str, str],
    top_n: int = 3,
) -> None:
    """For each theme, print top above-median anomalies (z-scored only)."""
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for u in upserts:
        if u["z_score"] is not None:
            by_theme[u["theme_id"]].append(u)

    log.info("Top %d above-median anomalies per theme (guarded locations excluded):", top_n)
    for tid, label_meta in scores_indexed_by_theme.items():
        if not label_meta:
            continue
        label = label_meta[0]["theme_label"][:65]
        med, spread = theme_stats.get(tid, (0.0, 0.0))
        log.info(
            "  [%s  median=%.1f%%  IQR=%.1f%%]  %s",
            label_meta[0]["theme_pass"], 100 * med, 100 * spread, label,
        )
        rows = by_theme.get(tid, [])
        top = sorted(rows, key=lambda r: -(r["z_score"] or 0))[:top_n]
        for r in top:
            log.info(
                "      %-22s  z=%+5.2f  prev=%5.1f%%",
                loc_names.get(r["location_id"], "?"),
                r["z_score"], 100 * (r["prevalence"] or 0),
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

    log.info("fetching scores joined with theme pass...")
    scores = fetch_scores_with_theme_pass(db)
    log.info("fetched %d scores across %d themes",
             len(scores), len({s["theme_id"] for s in scores}))

    log.info("computing per-location same-pass pool sizes...")
    pools = fetch_pool_sizes(db)
    log.info("counted pool sizes for %d (location, pass) pairs", len(pools))

    upserts, theme_stats, n_scored, n_guarded = compute_anomalies(
        scores, pools, args.min_pool,
    )
    log.info(
        "computed: %d z-scored, %d guarded (pool < %d)",
        n_scored, n_guarded, args.min_pool,
    )

    # Diagnostic display
    by_theme_scores: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        by_theme_scores[s["theme_id"]].append(s)
    loc_names = fetch_location_names(db)
    show_top_anomalies(upserts, theme_stats, by_theme_scores, loc_names)

    if args.dry_run:
        log.info("DRY RUN — no writes.")
        return

    inserted = failed = 0
    for i in range(0, len(upserts), UPSERT_BATCH):
        chunk = upserts[i:i + UPSERT_BATCH]
        try:
            db.table("location_theme_scores").upsert(
                chunk, on_conflict="location_id,theme_id",
            ).execute()
            inserted += len(chunk)
        except Exception as e:
            log.error("batch upsert failed (offset %d): %s", i, e)
            failed += len(chunk)

    log.info("=" * 60)
    log.info("Done. Upserted %d rows, %d failed.", inserted, failed)


if __name__ == "__main__":
    main()
