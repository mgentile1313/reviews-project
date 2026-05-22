# Phase A debrief — scrape pipeline

What we built, what we considered, what we learned. Read alongside
`scripts/pull_reviews.py` and `scripts/lib/brightdata.py`.

> **For someone new to this:** Phase A is the part that talks to Bright Data
> (the scraping service we pay), pulls down raw review data, and saves it to
> disk + an audit log. It deliberately doesn't do anything with the data
> beyond saving it. The "doesn't do anything else" is the most important
> design choice.

---

## Phase A in one sentence

Reads verified locations from Supabase, asks Bright Data to scrape Google and
Yelp reviews for each, and saves the raw responses to disk plus an audit row
in `raw_scrapes`.

---

## The big architectural call: two-phase ELT, not single-phase ETL

We split scrape from normalize, with raw JSON persisted on disk between them:

```
Phase A (this):    locations → BD → raw JSON on disk + raw_scrapes audit row
Phase B (next):    raw JSON on disk → normalize → reviews / location_metadata
```

### Why

- **Bright Data charges per record scraped.** If the normalizer in Phase B
  has a bug, we re-run B against saved raw — no re-scrape, no second bill.
- **Disk is free.** 38MB of raw JSON costs zero.
- **Decoupling = each phase debuggable in isolation.** Phase B was rewritten
  twice (constraint errors); Phase A never had to re-run.

### What we considered and rejected

- **Single-script ETL** (one script does scrape → normalize → load). Fewer
  lines. But every normalizer iteration becomes a money-costing re-run, and
  any constraint or shape problem in Supabase forces a re-scrape. Bad
  trade-off.

### How it played out

We hit two real bugs in Phase B after the scrape was done. Iterating cost zero
because Phase A's output was on disk. Decision validated by experience.

---

## Six design decisions and their alternatives

### 1. Batching — one trigger per source, not per location

| Approach | Trade-off |
|---|---|
| **One trigger per source, 34 URLs in one batch** ✅ | 2 API calls total. BD parallelizes internally. |
| One trigger per location | 68 API calls, more round trips, no benefit. |

**Why it matters:** BD's API was designed for batched inputs. Per-location
triggers would have been a misuse of the platform — and 34× the overhead
without any gain.

### 2. `raw_scrapes` granularity — one row per snapshot

| Approach | Trade-off |
|---|---|
| **One audit row per snapshot** ✅ | Matches BD's atomic unit. 2 rows per pull. |
| One row per location | 67 redundant rows (same snapshot_id, same timestamps). |
| One row per record | 18,948 audit rows for one scrape. Useless. |

**Conceptually:** a "snapshot" in BD terms is one batch job. The audit row is
a receipt for that job. One job, one receipt.

### 3. Volume control — `--days N` flag, no `--limit`

We discovered at validation time that neither Bright Data scraper supports a
per-URL record-count limit. The only real lever is the date window.

| Approach | Trade-off |
|---|---|
| **`--days N` flag overriding `--years`** ✅ | Smaller window = fewer records = cheaper test pulls. Honest about what BD actually controls. |
| `--limit` flag with post-download truncation | Doesn't save money (BD bills on scrape, not download). Adds code complexity. |
| Trust BD's nonexistent record-count fields | Silent overspend the day a field name changes upstream. |

**The lesson:** when an API contradicts your spec, the simpler answer is
usually right. Drop the misleading flag; control volume the way the system
actually supports.

### 4. Record → location matching — trust `input.url` (echoed back)

Bright Data preserves the URL we sent in each output record's `input.url`
field. So matching is a dict lookup, not a parse.

| Approach | Trade-off |
|---|---|
| **Match on `input.url`** ✅ | Trivial dict lookup. Validated 100% match (18,948/18,948). |
| Extract platform IDs (Google `cid`, Yelp slug) from URLs | More code. Needed only if URL match fails. |
| Hybrid (URL first, ID extraction fallback) | Originally planned as belt-and-suspenders. Made unnecessary by perfect URL match. |

**Plan for the worst, but check what's actually true.** We earmarked the
hybrid; we never needed it.

### 5. Environment variables — keep the existing naming

Multiple incoming prompts asked us to rename `NEXT_PUBLIC_SUPABASE_URL` →
`SUPABASE_URL` and `BRIGHTDATA_API_KEY` → `BRIGHTDATA_TOKEN`. We kept the
originals.

| Approach | Trade-off |
|---|---|
| **Keep existing names** ✅ | `config.py` already reads them; `NEXT_PUBLIC_` is meaningful in Next.js. |
| Rename to shorter forms | Pure churn — would break working code for cosmetic alignment. |

### 6. Async scrape pattern — trigger → poll → download, with explicit timeouts

| Approach | Trade-off |
|---|---|
| **Trigger, poll progress every 30s, download when ready** ✅ | Matches BD's API. 60-min default timeout absorbs large snapshots. |
| Synchronous "trigger that returns data" | Not supported by BD. |
| Short timeouts (e.g., 5 min) | Risk aborting on legitimate large snapshots — happened to us during early dev. Bumped to 60 min. |

---

## Two real bugs that taught us something

### Bug 1: BD input field names differed per scraper

We sent `reviews_count: 5` for Google → HTTP 400: *"This input should not
contain a reviews_count field."* Yelp also rejected its equivalent
(`num_of_reviews`).

**Lesson:** API specs ≠ runtime behavior. Validate with one URL before sending
34.

**Fix:** verbose logging at every API call surfaced the exact error message.
Dropped the rejected fields; volume control via date window alone.

### Bug 2: Race condition between `/progress` and `/snapshot` for large pulls

For Google's 18,495-record snapshot, BD's `/progress` endpoint flipped to
`status: ready`, but `/snapshot` returned `{"status": "building"}` for
another minute. Our code took the `ready` signal at face value, called
download, got the "building" envelope, and unwrapped it as a one-element
list. Saved 1 record on disk instead of 18,495.

**Lesson:** APIs with separate progress/data endpoints can be eventually
consistent under load. Treat both as authoritative independently.

**Fix:** `download_snapshot` retries on the `{"status": "building"}` envelope
with backoff. Recovered the 18,495 records without re-scraping (BD retains
snapshots).

---

## Coding patterns that recur

- **Explicit input builders per source** (`build_google_inputs`,
  `build_yelp_inputs`). Per-source field shape stays in one place; the main
  flow doesn't branch on source.
- **Verbose logging at every API boundary.** Log truncated body for debugging
  without filling logs with megabytes.
- **Type hints on every public function.** Python 3.10+ syntax (`int | None`,
  `list[dict]`) — modern and readable.
- **Mutually exclusive CLI flags** via `argparse.add_mutually_exclusive_group`
  when only one mode applies at a time.
- **Idempotent operations.** Re-running is always safe; nothing accumulates
  by accident.
- **Long timeouts with reason.** Where waits can be legitimately long
  (snapshot polling at 60 min), name why.
- **Background-friendly.** Long-running operations runnable in background
  with their full log written to a file.
