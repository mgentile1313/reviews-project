"""Phase A: scrape reviews via Bright Data and save raw to disk.

Architecture (ELT, two-phase):
    Phase A (this script): scrape -> save raw -> log snapshot to raw_scrapes
    Phase B (load_reviews):  read raw -> normalize -> upsert to reviews/location_metadata
    Phase C (embed_reviews): embed un-embedded review text

This script does NOT touch the `reviews` table. Disk is free; Bright Data
costs money per pull. If the Phase B normalizer has a bug, we re-run B
against saved raw — never re-pay for data.

Batching: one Bright Data trigger per source (not per location). All N
verified location URLs go into one Yelp trigger and one Google trigger.
Two API triggers total per pull. Per-record -> per-location matching
happens at load time via the URL echoed back by Bright Data.

Usage:
    python -m scripts.pull_reviews --source both --days 7   # cheap test pull
    python -m scripts.pull_reviews --source google
    python -m scripts.pull_reviews                          # both, last 2 years

Note: neither the Google nor Yelp Bright Data scrapers accept a per-URL
record-count limit. Volume is controlled only by the date window
(--years / --days).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.lib.brightdata import (
    download_snapshot,
    trigger_scrape,
    wait_for_snapshot,
)
from scripts.lib.config import (
    BRIGHTDATA_GOOGLE_DATASET_ID,
    BRIGHTDATA_YELP_DATASET_ID,
)
from scripts.lib.db import get_client

RAW_DIR = Path(__file__).resolve().parent.parent / "brightdata-raw"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def fetch_verified_locations(db) -> list[dict]:
    """Return verified locations (id + URLs) from Supabase."""
    res = (
        db.table("locations")
        .select("id, internal_id, google_url, yelp_url")
        .eq("verified_google", True)
        .eq("verified_yelp", True)
        .execute()
    )
    return res.data


def log_raw_scrape(
    db,
    *,
    source: str,
    snapshot_id: str,
    status: str,
    record_count: int,
    triggered_at: datetime,
    completed_at: datetime,
    notes: str | None = None,
) -> None:
    """Insert one audit row per snapshot. No location_id — batched scrape."""
    db.table("raw_scrapes").insert({
        "source": source,
        "snapshot_id": snapshot_id,
        "status": status,
        "record_count": record_count,
        "triggered_at": triggered_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "notes": notes,
    }).execute()


# ---------------------------------------------------------------------------
# Bright Data input builders — per-source because field names differ.
# These field names are educated guesses from the spec; if the test pull
# returns wrong counts or full-history, check the dataset config and adjust.
# ---------------------------------------------------------------------------

def build_yelp_inputs(
    locations: list[dict], *, start_date: str
) -> list[dict]:
    # Note: Yelp scraper does NOT accept a per-URL record limit
    # (no num_of_reviews). Volume is controlled only by start_date.
    return [
        {
            "url": loc["yelp_url"],
            "start_date": start_date,
            "sort_by": "DATE_DESC",
        }
        for loc in locations
    ]


def build_google_inputs(
    locations: list[dict], *, days_limit: int
) -> list[dict]:
    # Note: Google scraper does NOT accept a per-URL record limit
    # (no reviews_count). Volume is controlled only by days_limit.
    return [
        {
            "url": loc["google_url"],
            "days_limit": days_limit,
            "sort_by": "Newest",
        }
        for loc in locations
    ]


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_raw(records: list[dict], *, source: str) -> Path:
    """Write pretty-printed JSON array to brightdata-raw/{source}_raw_{ts}.json."""
    RAW_DIR.mkdir(exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = RAW_DIR / f"{source}_raw_{ts}.json"
    with open(path, "w") as f:
        json.dump(records, f, indent=2)
    log.info("[%s] saved %d records -> %s", source, len(records), path)
    return path


# ---------------------------------------------------------------------------
# Per-source pull
# ---------------------------------------------------------------------------

def pull_source(
    db,
    *,
    source: str,
    locations: list[dict],
    window_days: int,
) -> None:
    triggered_at = datetime.now(UTC)

    if source == "yelp":
        start_date = (datetime.now(UTC).date() - timedelta(days=window_days)).isoformat()
        inputs = build_yelp_inputs(locations, start_date=start_date)
        dataset_id = BRIGHTDATA_YELP_DATASET_ID
        log.info("[yelp] start_date=%s locations=%d", start_date, len(inputs))
    elif source == "google":
        inputs = build_google_inputs(locations, days_limit=window_days)
        dataset_id = BRIGHTDATA_GOOGLE_DATASET_ID
        log.info("[google] days_limit=%d locations=%d", window_days, len(inputs))
    else:
        raise ValueError(f"unknown source: {source}")

    snapshot_id = trigger_scrape(dataset_id, inputs)
    wait_for_snapshot(snapshot_id)
    records = download_snapshot(snapshot_id)
    completed_at = datetime.now(UTC)

    save_raw(records, source=source)
    log_raw_scrape(
        db,
        source=source,
        snapshot_id=snapshot_id,
        status="ready",
        record_count=len(records),
        triggered_at=triggered_at,
        completed_at=completed_at,
        notes=f"window_days={window_days} locations={len(inputs)}",
    )
    log.info("[%s] done — %d records, snapshot=%s", source, len(records), snapshot_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", choices=["google", "yelp", "both"], default="both",
    )
    parser.add_argument(
        "--years", type=int, default=2,
        help="Pull reviews from the last N years (default: 2). Overridden by --days.",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Pull reviews from the last N days (overrides --years). Use for cheap test pulls.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    window_days = args.days if args.days is not None else args.years * 365

    db = get_client()
    locations = fetch_verified_locations(db)
    if not locations:
        log.error("No verified locations in Supabase — run scripts.seed_locations first.")
        return
    log.info("Pulling %d locations × %d days", len(locations), window_days)

    sources = ["google", "yelp"] if args.source == "both" else [args.source]
    for src in sources:
        pull_source(db, source=src, locations=locations, window_days=window_days)


if __name__ == "__main__":
    main()
