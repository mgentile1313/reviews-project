# Phase B debrief — normalize + load

What we built, what we considered, what we learned. Read alongside
`scripts/load_reviews.py` and `docs/scraper-mapping.md`.

> **For someone new to this:** Phase B is the part that turns raw scraped JSON
> into clean Supabase rows. It never talks to Bright Data — it only reads
> files from disk. That means iterating on it is free.

---

## Phase B in one sentence

Reads raw Bright Data JSON files from `brightdata-raw/`, normalizes records
per source, and writes them into Supabase's `reviews`, `location_metadata`,
and (Google only) `locations.google_full_address` columns. Idempotent and
re-runnable.

---

## The big architectural call: doc-first, then code

Before writing a line of normalizer code, we wrote `docs/scraper-mapping.md`
specifying every field mapping, error policy, and CLI shape. The code became
a mechanical translation of the doc.

### Why

- The mapping doc is reviewable in isolation. You can argue about the data
  model without reading Python.
- When upstream field names change, you edit one doc and update the code
  alongside it.
- Forces sub-decisions to be explicit (what goes in `source_metadata`? what's
  the unique key for upsert?) rather than emerge as accidents in code.

### What we considered and rejected

- **Mapping in code comments only.** Less reviewable. Encourages drift
  between intent and implementation.

---

## Twelve design decisions and their alternatives

### 1. CLI — `--file` xor `--latest`, plus `--dry-run`

| Approach | Trade-off |
|---|---|
| **`--file <path>` for one file, `--latest` for auto-pick newest of each source** ✅ | Two clear modes, mutually exclusive. |
| `--all` (process every file in dir) | Too easy to accidentally re-process. Rejected. |
| Always auto-detect, no flags | Less explicit. Bad for debugging single files. |

`--dry-run` was added on top — runs the parse + match logic without writing
to Supabase. Catches normalizer bugs before they pollute the DB.

### 2. Source detection — filename prefix

| Approach | Trade-off |
|---|---|
| **Filename starts with `google_raw_` or `yelp_raw_`** ✅ | Authoritative, deterministic. |
| Content inspection (Google has `place_id`, Yelp has `business_id`) | Works, more complex. |
| Required `--source` flag | Friction for the common case. |

### 3. Google-before-Yelp enforced in `--latest`, not in `--file`

| Approach | Trade-off |
|---|---|
| **Order enforced in `--latest` only** ✅ | Google's loader fills `locations.google_full_address`. Order matters for that side-effect. `--file` trusts user. |
| Enforce always | Too rigid for single-file invocations. |
| Never enforce | Yelp could load first, leaving addresses null longer than necessary. |

### 4. Location matching — one DB query per source, dict lookup per record

| Approach | Trade-off |
|---|---|
| **Build `{url → location_id}` dict once per source** ✅ | 2 DB queries total. Per-record lookup is microseconds. |
| Per-record DB query | 18,948 queries. ~1000× slower. |

### 5. Idempotent upserts

| Approach | Trade-off |
|---|---|
| **`UPSERT ON CONFLICT (source, external_id)` for reviews, `(location_id, source)` for metadata** ✅ | Re-runs update in place, no duplicates. |
| `TRUNCATE + INSERT` | Loses partial state when multiple files in play. |
| INSERT-only with duplicate-error catch | Slow on re-runs, awkward. |

### 6. `google_full_address` updated only if currently NULL

| Approach | Trade-off |
|---|---|
| **Update only when NULL** ✅ | Google owns the column at first write. Re-runs and re-seeds don't fight; manual fixes survive. |
| Always overwrite from Google | A user correction would get undone on re-load. |
| Don't update from Google | Defeats the original goal: canonical address as single source of truth. |

### 7. Date columns are `date`, not `timestamptz`

| Approach | Trade-off |
|---|---|
| **`date`** ✅ | Simpler. Day granularity is enough for review analysis. |
| `timestamptz` | More fidelity (BD gives us time-of-day). |

You explicitly chose simplicity over optionality. Reversible later if needed
(would require an `ALTER COLUMN TYPE`).

### 8. Owner response text kept, owner response date dropped

BD's `response_date` field returns unreliable values (e.g., 2019 timestamps
on 2026 reviews).

| Approach | Trade-off |
|---|---|
| **Keep `owner_response` (text), drop `owner_response_at` (date)** ✅ | Text is high-signal (engagement, tone). Date was untrustworthy. |
| Keep both with "trust at your own risk" flag | Clutter for no benefit. |
| Drop both | Loses real signal. |

### 9. `source_metadata` jsonb is curated, not "everything else"

Each source has an explicit list of fields that go into `source_metadata`.
Fields not on the list are dropped.

| Approach | Trade-off |
|---|---|
| **Curated list per source** ✅ | Smaller rows, higher signal-to-noise. Forces a decision per field. |
| Everything else → metadata | Bloated rows; preserves noise like `check-in_status: "0 check-in"` on every Yelp row. |

You personally redlined the source_metadata lists (drop photos /
profile_pic / check-in_status; keep Reactions / Replies / recommended_review).

### 10. Chunked upserts at 500 per batch

| Approach | Trade-off |
|---|---|
| **500 per batch** ✅ | ~37 round trips for Google's 18,495. Each <1s. Conservative payload size. |
| Single bulk upsert | Risk of payload size limit, all-or-nothing failure surface. |
| 1 per upsert | Slow, high overhead. |
| Larger (e.g., 2000) | Marginally faster, more reviews lost per failed call. |

### 11. Error policy — soft-fail on data, hard-fail on DB

| Class | Policy |
|---|---|
| Unmatched URL (`input.url` not in our dict) | Skip + log + count. Continue. |
| Missing required field (e.g., `review_id`) | Skip + log + count. Continue. |
| Unparseable date | Store NULL + log. Continue. |
| Supabase write error | **Hard fail. Bubble up.** |

**Why:** data errors are per-record and the rest are usable. Write errors
mean schema/constraints are wrong — fail fast to fix the root cause.

### 12. Honest schema documentation

`supabase/schema.sql` explicitly labels what's verified-from-live (columns,
types) vs reconstructed-from-intent (constraints, defaults, view bodies).
The introspection technique has gaps; the doc says so.

---

## Three real bugs that taught us something

### Bug 1: PostgREST can't match `ON CONFLICT` on a partial unique index

Our original `reviews` table had `unique (source, external_id) where external_id is not null`. PostgREST's `?on_conflict=source,external_id`
sends `ON CONFLICT (source, external_id)` — no WHERE clause. PostgreSQL
refused: `42P10: there is no unique or exclusion constraint matching the
ON CONFLICT specification`.

**Lesson:** partial indexes are fine for direct SQL but cause trouble with
API layers that don't expose the full WHERE clause syntax.

**Fix:** drop partial, add regular `unique(source, external_id)` constraint.
PostgreSQL's standard `NULL ≠ NULL` semantics gave us the same effective
behavior — and since our normalizer never sends NULL `external_id`, the
change was zero-impact in practice.

### Bug 2: `schema.sql` claimed a constraint that wasn't actually deployed

`location_metadata` had `unique(location_id, source)` in our repo's
`schema.sql` but not in Supabase. The PostgREST OpenAPI introspection I'd
relied on shows columns + types but NOT constraints — so I'd reconstructed
that line from the Week 1 plan and assumed it was deployed.

**Lesson:** introspection has gaps. Constraints, indexes, defaults, and
triggers don't show in the OpenAPI spec. Don't assume `schema.sql == deployed`
without explicit verification.

**Fix:** added the missing constraint via `ALTER TABLE`. schema.sql was
already correct in shape; deployed state had to catch up.

### Bug 3: Bright Data race condition (carried from Phase A)

For large snapshots, BD's `/progress` endpoint reports `ready` before
`/snapshot` is fetchable. Phase A's `download_snapshot` now retries on the
`{"status": "building"}` envelope. Mentioned here because the recovery flow
benefited Phase B too — we re-downloaded 18,495 records without re-scraping.

---

## Coding patterns that recur

- **Spec doc precedes code** (`docs/scraper-mapping.md`). Code is mechanical
  translation; debate happens at the doc level.
- **Per-source normalizer functions** (`normalize_google_review`,
  `normalize_yelp_review`). Each understood in isolation; main flow
  branches on source once at top.
- **"Explicitly handled" pattern for jsonb.** Declare what you DO want by
  name, declare what you explicitly DON'T want; everything else falls out
  by complement. Self-documenting.
- **Stats dict accumulated** through `process_file`, used for summary
  printing at the end.
- **Small focused helpers** (`_iso(date)`, `_yelp_owner_response(replies)`)
  — easy to understand, easy to test if we ever add tests.
- **`process_file` is the unit of work.** All file-level logic in one
  function; `main` just orchestrates which files to process.
- **Validation tier before write tier.** `--dry-run` runs the full read +
  parse + match logic, just skips the writes. Catches issues before
  committing.
