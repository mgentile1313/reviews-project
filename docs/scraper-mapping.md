# Bright Data → Supabase mapping (Phase B spec)

How `scripts/load_reviews.py` normalizes Bright Data raw JSON into Supabase
`reviews`, `location_metadata`, and `locations`. This doc is authoritative — when
the upstream field shape changes, update this doc *and* the code in lock-step.

---

## Overview

Phase B is pure normalization. It reads raw JSON files saved by `pull_reviews.py`
to `brightdata-raw/`, looks each record up against `locations` by URL, and writes
normalized rows to `reviews` and per-location aggregates to `location_metadata`.
The Google loader additionally populates `locations.google_full_address`.

Phase B is **free to re-run.** Iterating on this script does not re-pay Bright
Data — the raw data lives on disk.

**All date columns are stored as `date` (no time of day)** — deliberately
simplified to day granularity for analysis. The only timestamp columns left in
the system are operational (`raw_scrapes.triggered_at` / `completed_at`,
`locations.created_at`).

---

## CLI

```
python -m scripts.load_reviews --file <path>      # load one specific file
python -m scripts.load_reviews --latest           # load the newest google + yelp from brightdata-raw/
python -m scripts.load_reviews --dry-run [--file|--latest]
```

- `--file` and `--latest` are mutually exclusive; exactly one is required.
- `--dry-run` reads, normalizes, and prints the summary but does **not** write
  to Supabase. Use this to validate a file before committing.

---

## Source detection

Determined from the filename. Pattern: `<source>_raw_*.json` where `<source>` is
`google` or `yelp`. Anything else → hard error before any DB work.

Examples:
- `google_raw_20260521_173147.json` → source = `google`
- `yelp_raw_20260521_154417.json`   → source = `yelp`
- `mavis_union_test.json`           → error: unrecognized source prefix

---

## Ordering (`--latest` mode)

When `--latest` is passed, the script processes **Google first, then Yelp**, in
the same invocation. This is enforced in code, not by trust. Reason: the Google
loader populates `locations.google_full_address`, and downstream consumers of
the address (including the analyst staring at Yelp data) benefit from it being
filled in before Yelp work happens.

When `--file` is passed, the script loads exactly that file regardless of
source. The user is in control; no Google-before-Yelp enforcement applies to a
single explicit file.

---

## Location matching

For every record, match `record["input"]["url"]` against the URL field of the
location row:
- Google records → `locations.google_url`
- Yelp records → `locations.yelp_url`

Implementation: one Supabase query per run builds a `{url: location_id}` dict,
then in-memory dict lookup per record. **No platform-ID fallback is needed** —
this was validated against real raw data: 100% of records matched on
`input.url` (Google 18,495 / 18,495; Yelp 453 / 453).

If the field shape ever changes upstream and matches start failing, the
unmatched-record log (see Error policy) will surface it loudly.

---

## Error policy

- **Unmatched location** (record's `input.url` not in our dict): skip the
  record, log to stderr with `record_id` + URL + reason. Continue.
- **Missing required field** (e.g., Google `review_id` absent, Yelp
  `review_id` absent): skip + log + continue.
- **Unparseable date**: log a warning, store the row with that date
  field set to `NULL`, continue.
- **Supabase write error**: hard fail. Bubble up.

At the end of the run, print a summary (see "Summary output" at bottom).

---

## Idempotency

- `reviews`: upsert on the partial unique index `(source, external_id)
  where external_id is not null`. Re-runs update existing rows in place — no
  duplicates.
- `location_metadata`: upsert on `(location_id, source)`. One row per
  (location, source) pair, kept fresh on every load.
- `locations.google_full_address`: updated **only if currently NULL** (avoids
  re-overwriting once set, which would also collide with a future re-seed).

Safe to run the same file (or `--latest`) any number of times.

---

## `locations.google_full_address` update

During Google loading only. For each distinct `location_id` encountered:
- Take the `address` field from the first record seen for that location
  (Bright Data repeats place-level info on every row, so any record works)
- Update `locations.google_full_address` **only if it's currently NULL**

The "only if NULL" rule means: Google owns this column at first population,
and subsequent re-loads or re-seeds don't fight over it. Manual override is
still possible via direct SQL.

---

## Field mapping — Google

### Per-review fields → `reviews`

| Raw field (Google) | `reviews` column | Notes |
|---|---|---|
| `review_id` | `external_id` | Required; record skipped if missing |
| `reviewer_name` | `author` | |
| `reviewer_url` | `author_profile_url` | |
| `reviews_by_reviewer` | `author_review_count` | Integer |
| `review_rating` | `rating` | Integer 1–5; CHECK enforces |
| `review` | `text` | **Nullable** — ~18% of Google records are rating-only (`null`) |
| `review_date` | `posted_at` | ISO 8601 → parse → store `.date()` (date only) |
| `response_of_owner` | `owner_response` | Flat field on Google. **Nullable** — not every review has one |
| `timestamp` | `scraped_at` | BD record timestamp → date |
| (computed) | `location_id` | Looked up via `input.url` → `locations.google_url` |
| (literal) | `source` | `'google'` |

Not mapped to columns: `response_date` (date was unreliable; `owner_response_at`
column dropped from schema).

### Per-record place-level → `location_metadata` (one row per location)

| Raw field (Google) | `location_metadata` column | Notes |
|---|---|---|
| `cid` | `external_id` | Numeric Google customer ID, stable per business |
| `place_name` | `external_name` | |
| `url` | `external_url` | Canonical Google Maps URL (BD echoes the input URL here, more or less) |
| `place_general_rating` | `overall_rating` | e.g., 4.6 |
| `overall_place_riviews` *(BD typo intentional)* | `total_reviews_visible` | e.g., 2212 |
| (assembled) | `raw_metadata` (jsonb) | See below |
| (today's date) | `scraped_at` | Load date |

`raw_metadata` jsonb for Google contains: `country`, `address` (also goes to
`locations.google_full_address` — duplicated here for completeness), `place_id`
(the textual `ChIJ...` Google ID, kept alongside `cid`), `fid_location`,
`questions_answers`, `category` (the BD field — preserved in raw, not promoted
to a column since Yelp doesn't have a matching one).

### `reviews.source_metadata` jsonb — Google

| Raw field | Goes in `source_metadata` |
|---|---|
| `review_details` | The `[{title, value}, ...]` list (Price assessment, Services, etc.) |
| `number_of_likes` | Likes on this review |
| `local_guide` | Boolean. **Not promoted to a column** — credentialing fields stay in metadata only (per analytical decision). |

Not captured: `photos`, `photos_by_reviewer`, `profile_pic_url` (no analytical
use planned for this project).

---

## Field mapping — Yelp

### Per-review fields → `reviews`

| Raw field (Yelp) | `reviews` column | Notes |
|---|---|---|
| `review_id` | `external_id` | Required |
| `Review_auther.Username` *(BD typo intentional)* | `author` | |
| `Review_auther.URL` | `author_profile_url` | |
| `Review_auther.Reviews_made` | `author_review_count` | Integer |
| `Rating` | `rating` | Integer 1–5 |
| `Content` | `text` | |
| `date_iso_format` | `posted_at` | ISO 8601 → parse → store `.date()`. Prefer this over the top-level `Date` (which is DD/MM/YYYY). |
| (extracted) | `owner_response` | First `Replies[]` item where `is_owner_reply == true` → take `.Content`. **Nullable** — not every review has one |
| `timestamp` | `scraped_at` | BD scrape timestamp → date |
| (computed) | `location_id` | Looked up via `input.url` → `locations.yelp_url` |
| (literal) | `source` | `'yelp'` |

Not mapped to columns: the owner reply's own `Date` (no `owner_response_at`
column anymore — see Google note above).

### Per-record place-level → `location_metadata`

| Raw field (Yelp) | `location_metadata` column | Notes |
|---|---|---|
| `business_id` | `external_id` | The Yelp slug, e.g., `mavis-discount-tire-union` |
| `business_name` | `external_name` | |
| `url` | `external_url` | Yelp business URL |
| — | `overall_rating` | `NULL` — Yelp scraper doesn't provide it at the review level |
| — | `total_reviews_visible` | `NULL` — same reason |
| (assembled) | `raw_metadata` (jsonb) | Anything else useful |
| (today's date) | `scraped_at` | |

### `reviews.source_metadata` jsonb — Yelp

| Raw field | Goes in `source_metadata` |
|---|---|
| `Reactions` | The `[{Number, Title}, ...]` list (Helpful, Thanks, etc.) |
| `Replies` | Full array (including non-owner replies if any) |
| `Review_auther.Location` | Reviewer's home city — **not** business address |
| `Review_auther.Friends` | Reviewer's friend count |
| `Review_auther.Photos` | Reviewer's photo count |
| `recommended_review` | Boolean. **Not promoted to a column** — credentialing stays in metadata (analytical decision). |

Not captured: `review_order`, `check-in_status`, `profile_pic_url` (no
analytical use planned).

---

## Date parsing notes

All date fields land in `date` columns (no time of day).

| Field | Format | Storage |
|---|---|---|
| Google `review_date` | ISO 8601 | Parse, `.date()`, store |
| Google record `timestamp` | ISO 8601 | Parse, `.date()`, store |
| Yelp `date_iso_format` | ISO 8601 | Parse, `.date()`, store |
| Yelp top-level `Date` | DD/MM/YYYY | **Don't use** — `date_iso_format` is unambiguous |

Wrap each parse in try/except; on failure, log a warning and store `NULL`.

---

## Summary output

After the run (or `--dry-run`), print to stdout:

```
=== load_reviews summary ===
[google] file: brightdata-raw/google_raw_20260521_173147.json
  records read:                  18495
  matched:                       18494
  unmatched (skipped + logged):      1
  reviews upserted:              18494
  location_metadata upserted:       34
  locations.google_full_address updated: 34
[yelp] file: brightdata-raw/yelp_raw_20260521_154417.json
  records read:                    453
  matched:                         453
  unmatched (skipped + logged):      0
  reviews upserted:                453
  location_metadata upserted:       34
```

If unmatched > 0, the first 5 unmatched URLs are printed below the summary for
quick diagnosis.
