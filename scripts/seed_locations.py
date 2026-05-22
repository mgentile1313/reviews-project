"""Seed the Supabase `locations` table from data/locations.json.

Inserts/updates only rows where verified_google AND verified_yelp are true.
Idempotent: keyed on internal_id via upsert — safe to re-run.

Usage:
    python -m scripts.seed_locations
    python -m scripts.seed_locations --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from scripts.lib.db import get_client

LOCATIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "locations.json"

# Explicit whitelist of columns the locations table accepts. Anything in
# locations.json not in this set is silently dropped, which keeps us robust
# to extra/legacy keys appearing in the source file.
LOCATION_COLUMNS = {
    "internal_id",
    "portco",
    "brand",
    "name",
    # NOTE: `address` removed — `locations.google_full_address` is now populated
    # by load_reviews.py from Google's canonical address. Seed must not touch it.
    "city",
    "state",
    "zip",
    "region",
    "performance_signal",
    "google_url",
    "yelp_url",
    "verified_google",
    "verified_yelp",
    "notes",
}

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be upserted; do not touch Supabase.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with open(LOCATIONS_FILE) as f:
        all_locations = json.load(f)

    verified = [
        loc for loc in all_locations
        if loc.get("verified_google") and loc.get("verified_yelp")
    ]
    log.info("locations.json: %d total / %d verified", len(all_locations), len(verified))

    if not verified:
        log.warning("Nothing to seed — no records pass `verified_google AND verified_yelp`.")
        return

    payload = [
        {k: loc[k] for k in LOCATION_COLUMNS if k in loc}
        for loc in verified
    ]

    if args.dry_run:
        log.info("DRY RUN — would upsert %d rows.", len(payload))
        log.info("First row preview: %s", json.dumps(payload[0], indent=2))
        return

    db = get_client()
    res = db.table("locations").upsert(payload, on_conflict="internal_id").execute()
    log.info("Upserted %d rows into locations.", len(res.data))


if __name__ == "__main__":
    main()
