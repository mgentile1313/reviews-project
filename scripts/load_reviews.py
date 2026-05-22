"""Phase B: normalize raw Bright Data JSON and load into Supabase.

Reads brightdata-raw/*.json files saved by `scripts.pull_reviews` and writes
normalized rows to:
  - `reviews`              (one row per review)
  - `location_metadata`    (one row per (location, source))
  - `locations`            (Google-only: populates `google_full_address` if null)

Pure normalization — never re-pays Bright Data. Safe to re-run; upserts are
idempotent on (source, external_id) for reviews and (location_id, source)
for location_metadata. `locations.google_full_address` is updated only when
currently NULL, so re-runs don't overwrite once populated.

Spec: docs/scraper-mapping.md

Usage:
    python -m scripts.load_reviews --latest                      # newest google + yelp from brightdata-raw/
    python -m scripts.load_reviews --file path/to/google_raw_*.json
    python -m scripts.load_reviews --latest --dry-run            # validate without writing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from scripts.lib.db import get_client

RAW_DIR = Path(__file__).resolve().parent.parent / "brightdata-raw"

# Upsert in chunks to keep request payloads under a few MB.
UPSERT_CHUNK = 500

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", type=Path, help="Path to one specific raw JSON file")
    g.add_argument(
        "--latest", action="store_true",
        help="Load the newest google_raw_*.json and yelp_raw_*.json from brightdata-raw/ (Google first)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Normalize and print summary; do NOT write to Supabase",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# File / source detection
# ---------------------------------------------------------------------------

def detect_source(path: Path) -> str:
    """Return 'google' or 'yelp' from the filename prefix; hard-error otherwise."""
    for src in ("google", "yelp"):
        if path.name.startswith(f"{src}_raw_"):
            return src
    raise ValueError(
        f"Cannot detect source from filename: {path.name} "
        "(expected google_raw_*.json or yelp_raw_*.json)"
    )


def find_latest(source: str) -> Path | None:
    files = sorted(RAW_DIR.glob(f"{source}_raw_*.json"))
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Date parsing — all date columns store `date`, no time of day
# ---------------------------------------------------------------------------

def parse_iso_date(value: str | None) -> date | None:
    """ISO 8601 → date. Returns None (and logs) on failure or empty input."""
    if not value:
        return None
    try:
        # Accept trailing 'Z' (older Python's fromisoformat is picky on some platforms)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError) as e:
        log.warning("parse_iso_date: %r unparseable (%s)", value, e)
        return None


def _iso(d: date | None) -> str | None:
    """date → ISO string for JSON serialization, or None."""
    return d.isoformat() if d else None


# ---------------------------------------------------------------------------
# Google normalizers
# ---------------------------------------------------------------------------

def normalize_google_review(r: dict, location_id: str) -> dict | None:
    """Map a Google raw record to a `reviews` row. None if missing required fields."""
    external_id = r.get("review_id")
    if not external_id:
        log.warning("google: missing review_id, skipping record")
        return None
    return {
        "location_id": location_id,
        "source": "google",
        "external_id": external_id,
        "author": r.get("reviewer_name"),
        "author_profile_url": r.get("reviewer_url"),
        "author_review_count": r.get("reviews_by_reviewer"),
        "rating": r.get("review_rating"),
        "text": r.get("review"),
        "posted_at": _iso(parse_iso_date(r.get("review_date"))),
        "owner_response": r.get("response_of_owner"),
        "scraped_at": _iso(parse_iso_date(r.get("timestamp"))),
        "source_metadata": {
            "review_details": r.get("review_details"),
            "number_of_likes": r.get("number_of_likes"),
            "local_guide": r.get("local_guide"),
        },
    }


def google_location_metadata(r: dict, location_id: str) -> dict:
    """Build location_metadata row from a Google record's place-level fields."""
    cid = r.get("cid")
    return {
        "location_id": location_id,
        "source": "google",
        "external_id": str(cid) if cid is not None else None,
        "external_name": r.get("place_name"),
        "external_url": r.get("url"),
        "overall_rating": r.get("place_general_rating"),
        "total_reviews_visible": r.get("overall_place_riviews"),  # BD typo intentional
        "raw_metadata": {
            "country": r.get("country"),
            "address": r.get("address"),
            "place_id": r.get("place_id"),  # textual ChIJ... id, kept alongside cid
            "fid_location": r.get("fid_location"),
            "questions_answers": r.get("questions_answers"),
            "category": r.get("category"),  # not promoted to a column; preserved here
        },
        "scraped_at": _iso(date.today()),
    }


# ---------------------------------------------------------------------------
# Yelp normalizers
# ---------------------------------------------------------------------------

def _yelp_owner_response(replies: list[dict] | None) -> str | None:
    """First Replies[] item where is_owner_reply is True → Content. None otherwise."""
    if not replies:
        return None
    for reply in replies:
        if reply.get("is_owner_reply"):
            return reply.get("Content")
    return None


def normalize_yelp_review(r: dict, location_id: str) -> dict | None:
    external_id = r.get("review_id")
    if not external_id:
        log.warning("yelp: missing review_id, skipping record")
        return None
    author = r.get("Review_auther") or {}  # BD typo intentional
    return {
        "location_id": location_id,
        "source": "yelp",
        "external_id": external_id,
        "author": author.get("Username"),
        "author_profile_url": author.get("URL"),
        "author_review_count": author.get("Reviews_made"),
        "rating": r.get("Rating"),
        "text": r.get("Content"),
        "posted_at": _iso(parse_iso_date(r.get("date_iso_format"))),
        "owner_response": _yelp_owner_response(r.get("Replies")),
        "scraped_at": _iso(parse_iso_date(r.get("timestamp"))),
        "source_metadata": {
            "Reactions": r.get("Reactions"),
            "Replies": r.get("Replies"),
            "Review_auther": {
                k: v for k, v in author.items()
                if k in ("Location", "Friends", "Photos")
            },
            "recommended_review": r.get("recommended_review"),
        },
    }


def yelp_location_metadata(r: dict, location_id: str) -> dict:
    # Yelp scraper doesn't expose category / overall_rating / total_reviews at the review level.
    # Keep any other top-level fields we didn't explicitly map in raw_metadata.
    explicitly_handled = {
        "business_id", "business_name", "url", "input",
        "Review_auther", "Rating", "Content", "Date", "date_iso_format",
        "review_id", "Reactions", "Replies", "timestamp",
        # mapped to source_metadata at review level:
        "recommended_review",
        # explicitly NOT captured per spec:
        "review_order", "check-in_status", "profile_pic_url",
    }
    raw_metadata = {k: v for k, v in r.items() if k not in explicitly_handled}
    return {
        "location_id": location_id,
        "source": "yelp",
        "external_id": r.get("business_id"),
        "external_name": r.get("business_name"),
        "external_url": r.get("url"),
        "overall_rating": None,
        "total_reviews_visible": None,
        "raw_metadata": raw_metadata,
        "scraped_at": _iso(date.today()),
    }


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def fetch_url_to_loc(db, source: str) -> dict[str, str]:
    """Map URL → location_id from Supabase, for the given source's URL column."""
    url_col = f"{source}_url"
    res = db.table("locations").select(f"id, {url_col}").execute()
    return {row[url_col]: row["id"] for row in res.data if row.get(url_col)}


def process_file(db, path: Path, *, dry_run: bool) -> dict[str, Any]:
    """Load + normalize one raw file. Returns stats dict."""
    source = detect_source(path)
    log.info("[%s] file: %s", source, path)

    with open(path) as f:
        records = json.load(f)

    url_map = fetch_url_to_loc(db, source)
    log.info("[%s] loaded url_map: %d entries", source, len(url_map))

    review_rows: list[dict] = []
    loc_md_by_loc: dict[str, dict] = {}     # location_id -> location_metadata row
    addr_updates: dict[str, str] = {}        # location_id -> google's address (google only)
    unmatched: list[str] = []
    skipped_no_id: int = 0

    for r in records:
        url = (r.get("input") or {}).get("url")
        loc_id = url_map.get(url)
        if not loc_id:
            unmatched.append(url or "<no input.url>")
            continue

        if source == "google":
            row = normalize_google_review(r, loc_id)
            if loc_id not in loc_md_by_loc:
                loc_md_by_loc[loc_id] = google_location_metadata(r, loc_id)
            if loc_id not in addr_updates and r.get("address"):
                addr_updates[loc_id] = r["address"]
        else:  # yelp
            row = normalize_yelp_review(r, loc_id)
            if loc_id not in loc_md_by_loc:
                loc_md_by_loc[loc_id] = yelp_location_metadata(r, loc_id)

        if row is None:
            skipped_no_id += 1
            continue
        review_rows.append(row)

    stats = {
        "source": source,
        "file": str(path),
        "records_read": len(records),
        "matched": len(records) - len(unmatched),
        "unmatched": unmatched,
        "skipped_no_external_id": skipped_no_id,
        "reviews_to_upsert": len(review_rows),
        "location_metadata_to_upsert": len(loc_md_by_loc),
        "addr_candidates": len(addr_updates),
        "addr_updated": 0,
    }

    if dry_run:
        log.info("[%s] DRY RUN — skipping writes", source)
        return stats

    # 1) reviews — upsert in chunks
    for i in range(0, len(review_rows), UPSERT_CHUNK):
        chunk = review_rows[i:i + UPSERT_CHUNK]
        db.table("reviews").upsert(chunk, on_conflict="source,external_id").execute()
        log.info("[%s] reviews upserted: %d/%d", source,
                 min(i + UPSERT_CHUNK, len(review_rows)), len(review_rows))

    # 2) location_metadata — one row per (location, source)
    if loc_md_by_loc:
        db.table("location_metadata").upsert(
            list(loc_md_by_loc.values()), on_conflict="location_id,source",
        ).execute()
        log.info("[%s] location_metadata upserted: %d", source, len(loc_md_by_loc))

    # 3) Google only: locations.google_full_address — only if currently NULL
    if source == "google" and addr_updates:
        for loc_id, addr in addr_updates.items():
            res = (
                db.table("locations")
                .update({"google_full_address": addr})
                .eq("id", loc_id)
                .is_("google_full_address", "null")
                .execute()
            )
            if res.data:
                stats["addr_updated"] += 1
        log.info("[google] locations.google_full_address updated: %d of %d candidates",
                 stats["addr_updated"], len(addr_updates))

    return stats


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(all_stats: list[dict]) -> None:
    print()
    print("=== load_reviews summary ===")
    for s in all_stats:
        print(f"[{s['source']}] file: {s['file']}")
        print(f"  records read:                          {s['records_read']:>6}")
        print(f"  matched:                               {s['matched']:>6}")
        print(f"  unmatched (skipped + logged):          {len(s['unmatched']):>6}")
        print(f"  skipped — missing external_id:         {s['skipped_no_external_id']:>6}")
        print(f"  reviews upserted:                      {s['reviews_to_upsert']:>6}")
        print(f"  location_metadata upserted:            {s['location_metadata_to_upsert']:>6}")
        if s['source'] == 'google':
            print(f"  locations.google_full_address updated: {s['addr_updated']:>6}")
        if s['unmatched']:
            print("  Sample unmatched URLs (first 5):")
            for u in s['unmatched'][:5]:
                print(f"    {u}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    files: list[Path] = []
    if args.file:
        if not args.file.exists():
            log.error("File not found: %s", args.file); sys.exit(1)
        files = [args.file]
    else:
        for src in ("google", "yelp"):  # google first — enforced
            f = find_latest(src)
            if f:
                files.append(f)
            else:
                log.warning("[%s] no %s_raw_*.json found in %s — skipping", src, src, RAW_DIR)
        if not files:
            log.error("No raw files found in %s", RAW_DIR); sys.exit(1)

    db = get_client()
    all_stats: list[dict] = []
    for f in files:
        all_stats.append(process_file(db, f, dry_run=args.dry_run))

    print_summary(all_stats)


if __name__ == "__main__":
    main()
