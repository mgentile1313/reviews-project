"""Week 2 Day 6 (Part A): per-location trend computation.

Computes two flavors of trends, both windowed at 90 days vs the prior 90 days:

  1. Overall per-location  (scope='overall', theme_id NULL)
       recent_value = avg rating in last 90 days
       prior_value  = avg rating in prior 90 days

  2. Per-(location, network theme)  (scope='theme')
       recent_value = location's prevalence on this theme in last 90 days
       prior_value  = location's prevalence in prior 90 days

Direction encoding:
- 'improving' / 'degrading' / 'stable' / NULL
- For overall: improving = avg rating went up; |delta| < STABLE_RATING_BAND = stable.
- For theme: improving means "this got BETTER for the location" — which depends on
  the theme's pass:
    - Action themes (1-3★): delta < 0  → improving (less prevalence is good)
    - Working themes (4-5★): delta > 0  → improving (more prevalence is good)
  Relative change < STABLE_REL_BAND of prior_value = stable.

Thin-window guard:
- If prior_n < MIN_WINDOW_N → direction = NULL (can't make a confident call).

Time anchor:
- We use max(posted_at) across all reviews as "today" so the windows are
  meaningful on the static dataset. Production with daily refresh would use
  now() at the application layer; the as_of column records what was used.

Re-run safety:
- By default, refuse to insert if trend rows already exist.
- --replace wipes the table first, then inserts fresh. This matches the
  pattern used by cluster_network and compute_location_scores — trends are
  a current-snapshot cache, not a historical archive, so each refresh
  fully replaces them anyway.

Usage:
    python -m scripts.compute_trends             # full run
    python -m scripts.compute_trends --dry-run   # compute + print summary
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean

from scripts.lib.clustering import PASS_DEFS
from scripts.lib.db import get_client as get_db

log = logging.getLogger(__name__)

# Window configuration.
WINDOW_DAYS = 90

# Direction thresholds.
STABLE_RATING_BAND = 0.10     # |Δavg rating| < 0.10 → stable
STABLE_REL_BAND = 0.20        # |Δprev| / prior_prev < 20% → stable

# Sample-size guard for direction call. Below this, write the row but leave
# direction NULL — we have the numbers but no confidence in the trend label.
MIN_WINDOW_N = 5

FETCH_PAGE = 1000
UPSERT_BATCH = 500

# rating → pass slug
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
        "--window-days", type=int, default=WINDOW_DAYS,
        help=f"Trend window in days (default {WINDOW_DAYS}).",
    )
    p.add_argument(
        "--replace", action="store_true",
        help="Wipe the trends table before inserting fresh rows.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print summaries; no DB writes.",
    )
    return p.parse_args(argv)


def existing_trend_count(db) -> int:
    res = db.table("trends").select("id").execute()
    return len(res.data or [])


def delete_all_trends(db) -> int:
    res = (
        db.table("trends")
        .delete()
        .neq("id", "00000000-0000-0000-0000-000000000000")
        .execute()
    )
    return len(res.data or [])


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_reviews_with_dates(db) -> list[dict]:
    """{id, location_id, rating, posted_at} for every review with a date.

    We need ALL reviews for window math (not just embedded ones) so we get
    the cleanest avg-rating signal. Theme prevalence trend still falls back
    to whatever's in member_review_ids (which is only embedded reviews) — that's
    fine, the comparison is internally consistent.
    """
    out: list[dict] = []
    last_id: str | None = None
    while True:
        q = (
            db.table("reviews")
            .select("id, location_id, rating, posted_at")
            .filter("posted_at", "not.is", "null")
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


def fetch_network_themes(db) -> list[dict]:
    res = (
        db.table("themes")
        .select("id, pass, label, member_review_ids, specific")
        .eq("scope", "network")
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Time window helpers
# ---------------------------------------------------------------------------

def parse_posted_at(value: str) -> datetime | None:
    """ISO 8601 timestamp from Supabase. Returns timezone-aware UTC datetime."""
    if not value:
        return None
    # Supabase returns either "2026-04-01T00:00:00+00:00" or "...T00:00:00".
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def compute_anchors(reviews: list[dict], window_days: int) -> tuple[datetime, datetime, datetime]:
    """Returns (as_of, recent_start, prior_start).

    Reviews in [recent_start, as_of] form the recent window.
    Reviews in [prior_start, recent_start) form the prior window.
    """
    timestamps = [parse_posted_at(r.get("posted_at")) for r in reviews]
    timestamps = [t for t in timestamps if t is not None]
    as_of = max(timestamps) if timestamps else datetime.now(timezone.utc)
    recent_start = as_of - timedelta(days=window_days)
    prior_start = as_of - timedelta(days=2 * window_days)
    return as_of, recent_start, prior_start


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def direction_for_rating(delta: float) -> str:
    if abs(delta) < STABLE_RATING_BAND:
        return "stable"
    return "improving" if delta > 0 else "degrading"


def direction_for_theme(delta: float, prior: float, pass_slug: str) -> str:
    """For action themes, less prevalence is better. For working, more is better."""
    if prior > 0 and abs(delta) / prior < STABLE_REL_BAND:
        return "stable"
    if abs(delta) < 1e-9:
        return "stable"
    if pass_slug == "actions":
        return "improving" if delta < 0 else "degrading"
    return "improving" if delta > 0 else "degrading"


def compute_overall_trends(
    reviews: list[dict],
    recent_start: datetime,
    prior_start: datetime,
    as_of: datetime,
    window_days: int,
) -> list[dict]:
    """One row per location with avg rating in the two windows."""
    # Group by location → (recent_ratings, prior_ratings)
    by_loc: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"recent": [], "prior": []})
    for r in reviews:
        loc = r.get("location_id")
        rating = r.get("rating")
        posted_at = parse_posted_at(r.get("posted_at"))
        if not loc or rating is None or posted_at is None:
            continue
        if posted_at >= recent_start:
            by_loc[loc]["recent"].append(rating)
        elif posted_at >= prior_start:
            by_loc[loc]["prior"].append(rating)

    rows: list[dict] = []
    for loc_id, buckets in by_loc.items():
        recent = buckets["recent"]
        prior = buckets["prior"]
        if not recent and not prior:
            continue
        recent_avg = mean(recent) if recent else None
        prior_avg = mean(prior) if prior else None
        if recent_avg is None or prior_avg is None:
            delta = None
            direction = None
        else:
            delta = recent_avg - prior_avg
            direction = (
                direction_for_rating(delta)
                if len(prior) >= MIN_WINDOW_N and len(recent) >= MIN_WINDOW_N
                else None
            )
        rows.append({
            "location_id": loc_id,
            "scope": "overall",
            "theme_id": None,
            "recent_value": recent_avg,
            "prior_value": prior_avg,
            "recent_n": len(recent),
            "prior_n": len(prior),
            "delta": delta,
            "direction": direction,
            "window_days": window_days,
            "as_of": as_of.isoformat(),
        })
    return rows


def compute_theme_trends(
    reviews: list[dict],
    themes: list[dict],
    recent_start: datetime,
    prior_start: datetime,
    as_of: datetime,
    window_days: int,
) -> list[dict]:
    """One row per (location, network theme) with prevalence in the two windows.

    Denominator: location's same-pass reviews in the same window.
    Numerator: those that are members of this theme.
    """
    # Pre-index per-location, per-pass, per-window review IDs.
    # by_loc_pass_window[(loc, pass)] = {'recent': set(ids), 'prior': set(ids)}
    by_loc_pass_window: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"recent": set(), "prior": set()}
    )
    for r in reviews:
        loc = r.get("location_id")
        rating = r.get("rating")
        posted_at = parse_posted_at(r.get("posted_at"))
        if not loc or rating not in RATING_TO_PASS or posted_at is None:
            continue
        pass_slug = RATING_TO_PASS[rating]
        if posted_at >= recent_start:
            by_loc_pass_window[(loc, pass_slug)]["recent"].add(r["id"])
        elif posted_at >= prior_start:
            by_loc_pass_window[(loc, pass_slug)]["prior"].add(r["id"])

    location_ids = sorted({loc for (loc, _p) in by_loc_pass_window.keys()})

    rows: list[dict] = []
    for theme in themes:
        pass_slug = theme["pass"]
        member_set = set(theme.get("member_review_ids") or [])
        for loc_id in location_ids:
            recent_pool = by_loc_pass_window[(loc_id, pass_slug)]["recent"]
            prior_pool = by_loc_pass_window[(loc_id, pass_slug)]["prior"]
            recent_n = len(recent_pool)
            prior_n = len(prior_pool)
            recent_value = len(recent_pool & member_set) / recent_n if recent_n > 0 else None
            prior_value = len(prior_pool & member_set) / prior_n if prior_n > 0 else None
            if recent_value is None or prior_value is None:
                delta = None
                direction = None
            else:
                delta = recent_value - prior_value
                direction = (
                    direction_for_theme(delta, prior_value, pass_slug)
                    if prior_n >= MIN_WINDOW_N and recent_n >= MIN_WINDOW_N
                    else None
                )
            rows.append({
                "location_id": loc_id,
                "scope": "theme",
                "theme_id": theme["id"],
                "recent_value": recent_value,
                "prior_value": prior_value,
                "recent_n": recent_n,
                "prior_n": prior_n,
                "delta": delta,
                "direction": direction,
                "window_days": window_days,
                "as_of": as_of.isoformat(),
            })
    return rows


# ---------------------------------------------------------------------------
# Summary printer (for dry-run)
# ---------------------------------------------------------------------------

def summarize(overall: list[dict], theme: list[dict]) -> None:
    from collections import Counter
    o_dir = Counter(r.get("direction") for r in overall)
    t_dir = Counter(r.get("direction") for r in theme)
    log.info("Overall trends: %d locations", len(overall))
    log.info("  direction breakdown: %s", dict(o_dir))
    log.info("Theme trends: %d (location × theme) rows", len(theme))
    log.info("  direction breakdown: %s", dict(t_dir))


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

    existing = existing_trend_count(db)
    if existing and not args.replace and not args.dry_run:
        log.error(
            "trends table already has %d rows. Re-run with --replace to overwrite.",
            existing,
        )
        return

    log.info("fetching reviews + dates...")
    reviews = fetch_reviews_with_dates(db)
    log.info("fetched %d reviews", len(reviews))

    log.info("fetching network themes...")
    themes = fetch_network_themes(db)
    log.info("fetched %d network themes", len(themes))

    as_of, recent_start, prior_start = compute_anchors(reviews, args.window_days)
    log.info(
        "time anchors: as_of=%s, recent=[%s, as_of], prior=[%s, recent)",
        as_of.date(), recent_start.date(), prior_start.date(),
    )

    log.info("computing overall trends...")
    overall_rows = compute_overall_trends(
        reviews, recent_start, prior_start, as_of, args.window_days,
    )
    log.info("computed %d overall trend rows", len(overall_rows))

    log.info("computing per-(location × theme) trends...")
    theme_rows = compute_theme_trends(
        reviews, themes, recent_start, prior_start, as_of, args.window_days,
    )
    log.info("computed %d theme trend rows", len(theme_rows))

    summarize(overall_rows, theme_rows)

    if args.dry_run:
        log.info("DRY RUN — no writes.")
        return

    if args.replace and existing:
        deleted = delete_all_trends(db)
        log.info("deleted %d existing trend rows", deleted)

    all_rows = overall_rows + theme_rows
    inserted = failed = 0
    for i in range(0, len(all_rows), UPSERT_BATCH):
        chunk = all_rows[i:i + UPSERT_BATCH]
        try:
            db.table("trends").insert(chunk).execute()
            inserted += len(chunk)
        except Exception as e:
            log.error("batch insert failed (offset %d): %s", i, e)
            failed += len(chunk)

    log.info("=" * 60)
    log.info("Done. Inserted %d trend rows, %d failed.", inserted, failed)


if __name__ == "__main__":
    main()
