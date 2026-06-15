# Design decisions & engineering log

Why the system is built the way it is — the decisions worth defending, the
alternatives that were tried and rejected, the bugs that shaped the design, and
what was deliberately left out. Where [`ARCHITECTURE.md`](ARCHITECTURE.md)
describes how the system works today, this document is the *journey*. For the
ingest stage specifically, the deeper rationale lives in
[`docs/phase-a-debrief.md`](docs/phase-a-debrief.md) and
[`docs/phase-b-debrief.md`](docs/phase-b-debrief.md).

---

## Key decisions

### Two-pass KMeans clustering (1–3★ "actions", 4–5★ "working")

**Problem:** Single-pass KMeans on the full review pool is dominated by whichever
polarity has more volume. At the ~6:1 positive-to-negative store, single-pass
`k=12` produced 11 positive clusters and 1 giant negative cluster — all
substructure on the negative side was lost, and the negative side is exactly
what drives manager actions.

**Decision:** Partition reviews by rating *before* clustering. Each pool gets its
own cluster budget, independent of the other.

**Trade-off:** A little more code and config, in exchange for negative-side themes
that actually resolve. Clearly worth it.

### Multi-stage Haiku labeling (candidates → selection → evidence)

A single labeling call produced redundant themes — at the worst store, 3 of 4
action themes were variants of "rude staff." Splitting into three stages fixed
it: (1) generate up to 3 quote-backed candidates per cluster, (2) select one per
cluster *with awareness of themes already picked* so the same idea doesn't
surface twice, (3) gather up to 3 verbatim supporting quotes, each tied to a
`source_review_id`. Haiku was chosen because labeling is cheap, fast,
structured extraction (~$0.60 for all locations + network), preserving the Opus
budget for brief writing.

### Same-pass denominator for prevalence

Prevalence ("what share of this location's reviews are in this theme?") uses the
**same rating pass** as its denominator — an action theme is divided by the
location's 1–3★ count, not its total. This makes prevalence comparable across
locations regardless of how positive or negative each store's mix is, which is
what lets the anomaly z-scores mean anything.

### Median + IQR for anomaly z-scores (not mean + std)

A robust z-score `(prevalence − median) / IQR` resists distortion from a single
extreme store (e.g. a location with 2 negative reviews and 50% prevalence on one
theme). A **min-pool guard** sets z-score to NULL when a location has fewer than
10 reviews in the relevant pass: the prevalence row stays, but no statistical
significance is claimed where the sample can't support it.

### Snapshot-anchored trends, not historical curves

Trends compare the last 90 days to the prior 90 days within a single refresh.
Per-theme historical curves aren't attempted because theme identity isn't stable
across refreshes (each clustering run mints new theme UUIDs) — honest multi-month
theme curves require Jaccard identity mapping, which is deferred. Overall rating
trend *is* theme-independent, so that curve is durable across refreshes.

### Opus for briefs, Haiku for labeling

Brief writing is high-judgment synthesis with quote integrity and rhetorical
structure — worth Opus (~$14 per full-network refresh). Labeling is structured
extraction — cheap and fast on Haiku. Spending the model budget where judgment
actually matters.

### Raw-review fallback when action clustering is thin

Low-negative-volume locations don't have enough 1–3★ reviews to cluster, so the
action pass would skip or produce only generic clusters — leaving the brief with
nothing to write from, even though a human could find real actions in ~14 raw
reviews. **Decision:** when a location has ≤2 specific action themes, inject its
top 10 substantive negative reviews into the intelligence packet and let the
brief generator extract directly. At the thinnest store this moved action-side
coverage (measured against the hand-written brief) from ~37% to ~75%+.

### Dashboard: Server Components only, no client-side data fetching

Every dashboard page is an async Server Component that reads Supabase at request
time — no SWR/React Query, no client data loading. With ~34 locations × ~10
themes the per-page data is small and queries are sub-200ms; pages are stateless
reads with no mutation surface, so there's no cache to invalidate and no secret
reaches the browser. The trade-off (full-page server transitions instead of SPA
interactivity) is the right call for a read-only intelligence surface.

### Dashboard: heatmap as an HTML table, no chart library

The `/heatmap` view is a plain `<table>` with sticky first row/column and a
Tailwind background class per cell from a 5-bin z-score function — avoiding
100–300 kB of charting JS for what is fundamentally a grid of colored squares.
**Color polarity inverts by pass:** for action columns a high z is bad (rose) and
low is good (green); for what's-working columns the semantics flip. Same scale,
opposite meaning — the clearest visual encoding of "what does this cell mean for
the manager."

### Dashboard: UI labels decoupled from DB enums

The database stores `'actions'` / `'working'`; the UI renders "Action items" /
"What's working" through a single helper. The enum is left intact because
renaming it would cascade through the Python scripts, the labeling prompts, and
the brief generator's expected packet shape — a UI-label change shouldn't force a
data-layer migration.

### Filter `specific=true` everywhere themes surface

The labeler marks only distinct, actionable themes `specific=true`; catch-all
clusters are retained with `member_count > 0` but no label, for diagnostics. The
UI mirrors the labeler's own opinion by filtering to `specific=true` everywhere,
so unlabeled clusters never leak into anomaly panels or theme pages. (An early
cut missed this filter and leaked empty theme pages — see below.)

---

## Alternatives tried, and what was rejected

- **Cluster size (`k=4/6/8/12`, single-pass):** `k=12` worked at the higher-
  negative store but collapsed at the lower-negative one (1 negative theme).
  Pivoted to two-pass with asymmetric `k`; two-pass `k=4` recovered 5 distinct
  negative themes where single-pass `k=12` found 1.
- **Single-quote evidence:** started with one quote per theme; some weren't
  strong enough alone. Pivoted to up to 3 quotes per theme, with the brief
  generator choosing the strongest — measurably better evidence sections.
- **Strict substring quote check:** the naive `quote in source_text` rejected 62
  of ~385 faithful quotes because Haiku normalizes curly punctuation to ASCII.
  Pivoted to a Unicode-tolerant match (curly→straight quotes, em/en→hyphen);
  rejection dropped from ~16% to ~0.5% while still catching real fabrication.
- **Semantic search inside brief generation:** initially proposed having the
  brief generator probe for "known watchlist patterns" — rejected because the
  generator has no persistent watchlist to draw from. Semantic search lives in
  the MCP server and dashboard drill-down instead; the brief stays self-contained
  on its intelligence packet.
- **`Profile` field on the brief header** (low-performer / middling / …): dropped
  in favor of showing the actual rating + trend, to avoid an LLM-inferred
  meta-classification.
- **Mixed-prevalence home grid:** the first home page interleaved action and
  positive themes by prevalence and read as confusing. Split into a rose "Action
  items" section over a green "What's working" section — same data, far clearer
  hierarchy.

---

## Bugs that shaped the design

- **Haiku Unicode normalization** silently rejected 62 valid quotes (curly vs
  straight punctuation). Fix: a Unicode punctuation map in the substring check.
  This same bug was the root cause of two watchlist items shipping without
  verifiable quotes in v1 — both got real verbatim quotes once the check was
  fixed.
- **`httpx` ReadTimeout crashed full-network labeling** at location 19 of 34 on a
  transient Supabase timeout — the per-location loop had no error boundary. Fix:
  a retry wrapper that retries transient errors once, skips non-transient ones,
  and never lets one location crash the batch.
- **PostgREST `on_conflict` can't target expression-based unique indexes.** The
  trends table used a `COALESCE(theme_id, sentinel)` unique index so
  overall-trend rows (NULL `theme_id`) could coexist with per-theme rows;
  PostgREST rejected the upsert because expression indexes aren't constraints.
  Fix: switched to insert + a `--replace` flag that wipes first — cleaner anyway,
  since trends are a single-snapshot cache, not history.
- **Theme leakage from a missing filter:** an early `getLocationDetail` queried
  scores without `specific=true`, so unlabeled catch-all clusters appeared in the
  anomaly panel and produced empty theme pages. Fix: filter `specific=true` at
  every theme join.
- **Schema-vs-code column drift:** a data fetcher assumed descriptive column
  names (`recent_rating_mean`) that didn't match the actual schema
  (`recent_value`); a stat tile silently rendered nothing. A smoke test caught
  it. Lesson: verify real column names before writing the fetcher.
- **Markdown soft-breaks** put multiple evidence quotes on one line (CommonMark
  treats single newlines as spaces). Fix: a `remark-breaks` plugin — cheaper than
  regenerating every brief to use double newlines.

---

## Explicitly deferred (and why)

- **Jaccard theme-identity mapping across refreshes** — the schema already has
  `previous_theme_id` / `first_seen_at` / `last_seen_at`, but the matching logic
  isn't built, so each refresh mints new theme UUIDs. Single-snapshot analysis is
  sufficient for now; multi-month theme curves are a later build on a ready
  schema.
- **Authentication / RBAC** — out of scope for a no-auth demo build.
- **Reviews browser, "compare locations" view, notes/starred locations** —
  deferred; the first depends on the same semantic-search layer the MCP server
  uses, the others need an auth layer.

---

## Cost economics

**Per full-network refresh (~monthly):**

| Step | Cost |
|---|---|
| Bright Data scrape (Google + Yelp, ~7k new reviews) | ~$12 |
| OpenAI embeddings (`text-embedding-3-large`, batched) | ~$0.25 |
| Haiku labeling (all three stages) | ~$0.60 |
| Opus brief generation × 34 | ~$14 |
| Scoring / anomalies / trends (DB ops only) | ~$0 |
| **Total** | **~$27** |

Ad-hoc single-location brief regeneration is ~$0.40; an MCP semantic-search query
is ~$0.005; dashboard loads are pure DB reads (~$0).

---

## Known limitations

- **Single-incident severe stories** (e.g. an engine failure attributed to one
  oil change) don't form clusters and so can't be surfaced by clustering-based
  action selection. The hand-written brief led with one such case the system
  missed. Mitigation: MCP semantic search recovers these on demand ("any
  safety-related complaints?"), but brief generation alone cannot.
- **Theme identity isn't preserved across refreshes**, so theme-level trend
  history won't be honest until Jaccard mapping ships (overall rating trend is
  fine).
- **No notion of resolved vs active** — a complaint from two years ago counts
  toward a theme as much as one from yesterday. Recency weighting would help.
- **Network-level rare-but-critical patterns** (rare safety or conduct
  allegations) fall below the min-cluster-size floor and are invisible at the
  network level even though they matter most — worth a dedicated "severe outlier"
  surface.
