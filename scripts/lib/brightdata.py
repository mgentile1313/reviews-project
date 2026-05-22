"""Bright Data Web Scraper API client helpers.

Async pattern (trigger -> poll -> download):

    snapshot_id = trigger_scrape(dataset_id, inputs)
    wait_for_snapshot(snapshot_id)           # blocks until ready
    records = download_snapshot(snapshot_id)

Endpoint reference (Bright Data v3):
    POST /datasets/v3/trigger?dataset_id=<id>   body = list[input dict]
    GET  /datasets/v3/progress/<snapshot_id>    status polling
    GET  /datasets/v3/snapshot/<snapshot_id>?format=json   download data

Every API call logs its request shape and response status — the first real
run will surface any per-scraper field-name mismatches in the logs and in
the saved brightdata-raw JSON, which is the intended debugging path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .config import BRIGHTDATA_API_KEY

API_BASE = "https://api.brightdata.com/datasets/v3"

log = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {BRIGHTDATA_API_KEY}",
        "Content-Type": "application/json",
    }


def trigger_scrape(dataset_id: str, inputs: list[dict[str, Any]]) -> str:
    """Trigger a Bright Data batch scrape. Returns the snapshot_id."""
    if not inputs:
        raise ValueError("inputs is empty — nothing to scrape.")

    url = f"{API_BASE}/trigger"
    # include_errors=true makes Bright Data return per-URL error rows instead
    # of failing the whole snapshot, so partial batches still come back.
    params = {"dataset_id": dataset_id, "include_errors": "true"}

    log.info("trigger_scrape: dataset=%s inputs=%d", dataset_id, len(inputs))
    r = requests.post(url, headers=_headers(), params=params, json=inputs, timeout=60)
    log.info("trigger_scrape: HTTP %d body=%s", r.status_code, r.text[:2000])
    r.raise_for_status()

    snapshot_id = r.json().get("snapshot_id")
    if not snapshot_id:
        raise RuntimeError(f"trigger_scrape: no snapshot_id in response: {r.text}")
    log.info("trigger_scrape: snapshot_id=%s", snapshot_id)
    return snapshot_id


def wait_for_snapshot(
    snapshot_id: str,
    *,
    poll_interval: int = 30,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Poll the progress endpoint until status == 'ready'.

    Raises on 'failed'/'error' or if timeout (default 30 min) is exceeded.
    """
    url = f"{API_BASE}/progress/{snapshot_id}"
    deadline = time.time() + timeout
    elapsed = 0

    while True:
        r = requests.get(url, headers=_headers(), timeout=30)
        r.raise_for_status()
        progress = r.json()
        status = progress.get("status")
        log.info(
            "wait_for_snapshot[%s]: status=%s elapsed=%ds progress=%s",
            snapshot_id, status, elapsed, {k: v for k, v in progress.items() if k != "status"},
        )

        if status == "ready":
            return progress
        if status in ("failed", "error"):
            raise RuntimeError(f"snapshot {snapshot_id} failed: {progress}")
        if time.time() >= deadline:
            raise TimeoutError(
                f"snapshot {snapshot_id} not ready after {timeout}s (last status: {status})"
            )

        time.sleep(poll_interval)
        elapsed += poll_interval


def download_snapshot(
    snapshot_id: str,
    *,
    poll_interval: int = 30,
    timeout: int = 1800,
) -> list[dict[str, Any]]:
    """Download the snapshot's records as a list of dicts.

    Bright Data's /progress endpoint can flip to 'ready' before the /snapshot
    endpoint is fetchable — for large snapshots /snapshot returns
    {"status": "building", "message": "Dataset is not ready yet, try again in 30s"}
    until the data is materialized. This retries on that envelope with backoff
    until ready, or raises TimeoutError after `timeout` seconds.

    Also handles two response formats: a JSON array (default) or NDJSON
    (one JSON object per line, returned by some datasets).
    """
    url = f"{API_BASE}/snapshot/{snapshot_id}"
    log.info("download_snapshot: %s", snapshot_id)
    deadline = time.time() + timeout
    elapsed = 0

    while True:
        r = requests.get(url, headers=_headers(), params={"format": "json"}, timeout=300)
        r.raise_for_status()

        text = r.text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # NDJSON fallback: one JSON object per non-empty line.
            data = [json.loads(line) for line in text.splitlines() if line.strip()]

        # BD race condition: /snapshot can lag /progress on big snapshots.
        if isinstance(data, dict) and data.get("status") == "building":
            if time.time() >= deadline:
                raise TimeoutError(
                    f"snapshot {snapshot_id} still building after {timeout}s"
                )
            log.info(
                "download_snapshot[%s]: still building (elapsed=%ds), retrying in %ds",
                snapshot_id, elapsed, poll_interval,
            )
            time.sleep(poll_interval)
            elapsed += poll_interval
            continue

        # Some datasets wrap records in {"data": [...]}.
        if isinstance(data, dict):
            data = data.get("data", [data])

        if not isinstance(data, list):
            raise RuntimeError(
                f"download_snapshot {snapshot_id}: unexpected payload type {type(data).__name__}"
            )

        log.info("download_snapshot: %d records", len(data))
        return data
