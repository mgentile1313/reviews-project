"""Ingest reviews via the Bright Data Web Scraper API.

Pipeline step 1 of 3:  ingest -> embed -> cluster

Usage:
    python -m scripts.ingest.brightdata <source_url>
"""

import sys

import requests

from scripts.lib.config import BRIGHTDATA_API_KEY
from scripts.lib.db import get_client

BRIGHTDATA_API = "https://api.brightdata.com"


def scrape_reviews(url: str) -> list[dict]:
    """Trigger a Bright Data scrape job for `url` and return raw review rows.

    TODO: wire up the specific Bright Data dataset/collector for the target
    platform. The exact endpoint and payload depend on the dataset chosen in
    the Bright Data dashboard.
    """
    headers = {"Authorization": f"Bearer {BRIGHTDATA_API_KEY}"}
    raise NotImplementedError("Configure the Bright Data dataset, then implement.")


def store(source_url: str, reviews: list[dict]) -> None:
    """Upsert a source and its reviews into Supabase."""
    db = get_client()
    source = (
        db.table("sources")
        .upsert({"name": source_url, "url": source_url}, on_conflict="url")
        .execute()
    )
    source_id = source.data[0]["id"]

    rows = [
        {
            "source_id": source_id,
            "external_id": r.get("id"),
            "author": r.get("author"),
            "rating": r.get("rating"),
            "title": r.get("title"),
            "body": r["body"],
            "posted_at": r.get("posted_at"),
        }
        for r in reviews
    ]
    db.table("reviews").upsert(rows, on_conflict="source_id,external_id").execute()
    print(f"Stored {len(rows)} reviews for {source_url}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m scripts.ingest.brightdata <source_url>")
    url = sys.argv[1]
    reviews = scrape_reviews(url)
    store(url, reviews)


if __name__ == "__main__":
    main()
