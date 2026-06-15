"""Week 3 Day 1: brief generation via Opus, one call per location.

Takes the intelligence packet from get_location_intelligence(), passes it to
Opus with a strict-format system prompt, and writes the markdown brief back
to the briefs table. Single call per location — no retries, no multi-pass.
The prompt is strict about verbatim quotes (no paraphrasing, no cleanup).

Two regimes handled by the same prompt:
  - Normal: location has 3+ specific action themes from clustering. Brief
    pulls actions from local_themes.
  - Fallback: <=2 specific action themes. Intelligence packet includes
    raw_action_reviews; brief reads them directly to identify actions.

Re-run safety:
  - --replace marks any active brief for the location as 'superseded' and
    inserts a fresh one.
  - Without --replace, refuses if an active brief already exists.

Usage:
    python -m scripts.generate_brief --location-id <uuid>
    python -m scripts.generate_brief --internal-id mavis_019
    python -m scripts.generate_brief --internal-id mavis_019 --dry-run
    python -m scripts.generate_brief --internal-id mavis_019 --replace
    python -m scripts.generate_brief                                  # all locations
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from scripts.lib.anthropic import get_client as get_anthropic
from scripts.lib.config import BRIEF_MODEL
from scripts.lib.db import get_client as get_db
from scripts.lib.intelligence import get_location_intelligence

log = logging.getLogger(__name__)

MAX_TOKENS = 4000

# Pricing must match BRIEF_MODEL (config: claude-opus-4-7), per 1M tokens —
# used only for the dry-run estimate; actual cost is computed from API usage.
PRICE_INPUT_PER_M = 15.0
PRICE_OUTPUT_PER_M = 75.0
PRICE_CACHED_INPUT_PER_M = 1.5


SYSTEM_PROMPT = """You are writing a brief for a tire-shop store manager. \
The brief follows a strict format and the evidence in it must be VERBATIM \
from real reviews — never paraphrase.

# QUOTE INTEGRITY — THE LOAD-BEARING RULE

Every quote in the brief must be VERBATIM from a review in the input data.
You may not:
- Paraphrase. Not even a word.
- "Clean up" punctuation, typos, capitalization, or grammar.
- Compose a quote across multiple reviews.
- Invent a quote that "represents" a pattern.
- Translate, abbreviate, or summarize a quote.

When you include a quote, you must:
1. Copy the exact text from one of the reviews provided to you.
2. Attribute it: source platform, date, rating, and the approximate echo
   count when known (the member_count of the source theme).

If you cannot find a real verbatim quote that supports a point you want to
make, DO NOT make that point. Drop the action, drop the watchlist item.
False evidence is worse than missing content.

# FORMAT (strict — match exactly)

# Brief — [location.name verbatim — do not add city/state suffix; the name already contains them]

**Location ID:** [uuid]
**Current rating:** [recent_value]★ (last [window_days] days, n=[recent_n]) — \
[direction] from [prior_value]★ prior [window_days] days
**As of:** [as_of date]
**Generated:** [today's date]

---

## Top 3 actions

Order reflects priority. Priority is set by frequency + content scale,
not severity alone. Each action must be implementable by the store manager —
not a reworded theme.

### 1. [action item title]

**Intervention:** [1-3 sentences. Concrete change a store manager could
implement Monday morning. Two-part interventions labeled (a) and (b). If
a stronger alternative was considered and rejected, mention it in one line.]

**Evidence:** [1-3 verbatim quotes, each on its own line, each with its own
attribution. Use multiple quotes when each adds distinct color or
strengthens the case; use 1 when one quote captures the pattern fully.
Format each line as:]
*"[verbatim quote]"* — [source], [YYYY-MM-DD], [N]★
(~[member_count] other reviews echo this pattern — include only on the last quote)

**Why it matters:** [1-2 sentences. Business consequence — revenue,
retention, liability, brand, safety. Not a restatement of the complaint.]

### 2. [action item title]
### 3. [action item title]

---

## Watchlist

0-4 items. Patterns that are real but didn't make top-3 — too low
frequency, ambiguous signal, or worth tracking. Each entry needs at least
one verbatim quote with attribution. If nothing meets the bar, leave the
section header in place with no items beneath.

- **[Pattern name]:** [1-2 sentences explaining the pattern and why it's
  watchlist rather than top-3.]
  *"[verbatim quote]"* — [source], [YYYY-MM-DD], [N]★

---

## What's working

1-3 items. Specific, pattern-supported positives ONLY. "Friendly staff",
"great service", "fast" without a specific practice or named employee are
GENERIC VIBES — exclude them. Same evidence bar as actions.

If nothing meets the bar, write exactly:
*"No specific, pattern-supported positives identified above the
generic-vibes threshold."*

- **[Specific practice]:** [1 sentence describing the practice.]
  *"[verbatim quote]"* — [source], [YYYY-MM-DD], [N]★

---

## Labeler notes

0-4 observations the structured sections don't capture. Hypotheses about
what's driving patterns, signals that conflict with each other, surprises,
context worth carrying forward. If nothing to add, leave the section header
in place with no items beneath.

- [Observation]

# INPUT INTERPRETATION

You receive an intelligence packet with these fields:

- `location`: facts about the store (name, city, state).
- `local_themes`: clustered themes from this store's reviews. Each
  `specific: true` theme has `label`, `member_count`, and `candidate_quotes`
  — a list of up to 3 verbatim phrases with full attribution
  (`{quote, source, posted_at, rating, review_id}`). The brief generator
  picks 1-3 of these candidate_quotes per evidence section, choosing the
  strongest and most distinct. Generic themes (`specific: false`) have no
  useful content — ignore them.
- `raw_action_reviews`: when present and non-empty, the cluster pass
  produced ≤2 specific action themes. Read these raw reviews DIRECTLY to
  identify action patterns. Same verbatim-quote rule applies — pull
  quotes from the review `text` field.
- `anomalies`: this location's z-scored deviations from the network median
  per theme. A positive z on an action theme = this location is worse than
  peers; a negative z on a working theme = below peers on a positive
  practice. Useful for prioritization and labeler-notes color.
- `overall_trend`: 90d vs prior 90d avg rating. Brief should mention if
  direction is improving or degrading (not stable).
- `theme_trends`: same windowed comparison per theme. Useful for "this is
  getting worse" / "this is getting better" callouts in labeler notes.
- `network_context`: total locations, as_of date.

# CONTENT GUIDANCE

ACTIONS:
- Header is the action's own concise title — your choice of phrasing, but
  it must describe an INTERVENTION, not just restate a theme. "Address
  the appointment problem" is not an action title; the intervention itself
  belongs there.
- The intervention paragraph names concrete steps a manager could implement
  Monday morning.
- "Why it matters" is about business consequence, not a restated complaint.

WATCHLIST:
- Patterns from `local_themes` (specific=true, not in top-3), or from
  `raw_action_reviews` when present, or from clear `anomalies` signal
  where this location is far above network median on something not picked
  as an action.
- Each item needs verbatim evidence.

WHAT'S WORKING:
- Pull from `local_themes` working pass (specific=true). Generic themes
  don't qualify regardless of size.
- Named-employee + specific behavior is the gold standard.

LABELER NOTES:
- Trend-related ("overall sentiment improving 0.44 stars in 90 days").
- Network-comparison ("you're an outlier on X compared to peers" — in
  third person: "this location is an outlier on X").
- Tensions between data points ("below network on a positive practice but
  above network on the corresponding action theme — suggests some staff
  do this well, others don't").
- Don't restate what the structured sections say.

VOICE: third person throughout ("this location", "the store", not "your
location" or "you").

OUTPUT ONLY THE BRIEF. NO PREAMBLE, NO POSTAMBLE."""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--location-id", help="Location UUID.")
    g.add_argument("--internal-id", help="Resolve internal_id (e.g. mavis_019) → UUID.")
    p.add_argument(
        "--model", default=BRIEF_MODEL,
        help=f"Anthropic model id (default {BRIEF_MODEL}).",
    )
    p.add_argument(
        "--replace", action="store_true",
        help="Supersede the current active brief and write a fresh one.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show the prompt and intelligence; estimate cost; no API call.",
    )
    return p.parse_args()


def resolve_location(db, args) -> str | None:
    if args.location_id:
        return args.location_id
    if args.internal_id:
        res = db.table("locations").select("id").eq("internal_id", args.internal_id).execute()
        if not res.data:
            sys.exit(f"internal_id {args.internal_id!r} not found")
        return res.data[0]["id"]
    return None


def fetch_all_location_ids(db) -> list[str]:
    res = db.table("locations").select("id").execute()
    return [r["id"] for r in (res.data or [])]


def existing_active_brief(db, location_id: str) -> dict | None:
    res = (
        db.table("briefs")
        .select("id, generated_at, model")
        .eq("location_id", location_id)
        .eq("status", "active")
        .execute()
    )
    return res.data[0] if res.data else None


def supersede_active_briefs(db, location_id: str) -> int:
    res = (
        db.table("briefs")
        .update({"status": "superseded"})
        .eq("location_id", location_id)
        .eq("status", "active")
        .execute()
    )
    return len(res.data or [])


# ---------------------------------------------------------------------------
# Opus call + cost estimate
# ---------------------------------------------------------------------------

def build_user_message(intel: dict) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return (
        f"Today's date (for the Generated header): {today}\n\n"
        "INTELLIGENCE PACKET:\n"
        + json.dumps(intel, indent=2, default=str)
    )


def estimate_tokens(text: str) -> int:
    """Rough 4-chars-per-token approximation."""
    return max(1, len(text) // 4)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * PRICE_INPUT_PER_M
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_M
    )


def generate_brief_for_location(
    db,
    anthropic_client,
    location_id: str,
    model: str,
    replace: bool,
    dry_run: bool,
) -> dict:
    """Returns a summary dict for the run."""
    intel = get_location_intelligence(location_id, db=db)
    loc = intel["location"]
    log.info("=== %s (%s) ===", loc["name"], loc["internal_id"])

    existing = existing_active_brief(db, location_id)
    if existing and not replace and not dry_run:
        log.warning(
            "active brief already exists (id=%s, model=%s). Use --replace to overwrite.",
            existing["id"], existing["model"],
        )
        return {"location": loc["internal_id"], "skipped": True}

    user_msg = build_user_message(intel)
    input_tokens_est = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(user_msg)
    output_tokens_est = 2500  # rough upper bound for a strict-format brief

    if dry_run:
        cost = estimate_cost(input_tokens_est, output_tokens_est)
        log.info(
            "DRY RUN — would send ~%d input + ~%d output tokens, estimated cost ~$%.4f",
            input_tokens_est, output_tokens_est, cost,
        )
        print("=" * 90)
        print("SYSTEM PROMPT")
        print("=" * 90)
        print(SYSTEM_PROMPT)
        print()
        print("=" * 90)
        print("USER MESSAGE (intelligence packet)")
        print("=" * 90)
        print(user_msg)
        return {
            "location": loc["internal_id"],
            "dry_run": True,
            "estimated_cost_usd": cost,
            "estimated_input_tokens": input_tokens_est,
        }

    log.info("calling %s...", model)
    res = anthropic_client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    content = "".join(b.text for b in res.content if hasattr(b, "text")).strip()

    # Pull usage for actual cost.
    usage = getattr(res, "usage", None)
    actual_input = getattr(usage, "input_tokens", input_tokens_est) if usage else input_tokens_est
    actual_output = getattr(usage, "output_tokens", output_tokens_est) if usage else output_tokens_est
    actual_cost = estimate_cost(actual_input, actual_output)
    log.info(
        "received %d output tokens (input=%d). Cost ~$%.4f.",
        actual_output, actual_input, actual_cost,
    )

    intel_as_of = intel.get("network_context", {}).get("as_of")
    if replace and existing:
        n = supersede_active_briefs(db, location_id)
        log.info("superseded %d prior active brief(s)", n)

    insert_payload = {
        "location_id": location_id,
        "model": model,
        "content": content,
        "intelligence_as_of": intel_as_of,
        "cost_usd": actual_cost,
        "status": "active",
    }
    db.table("briefs").insert(insert_payload).execute()
    log.info("wrote brief to briefs table (status=active)")

    return {
        "location": loc["internal_id"],
        "input_tokens": actual_input,
        "output_tokens": actual_output,
        "cost_usd": actual_cost,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    db = get_db()
    anthropic_client = None if args.dry_run else get_anthropic()

    target = resolve_location(db, args)
    location_ids = [target] if target else fetch_all_location_ids(db)
    log.info("generating briefs for %d location(s) with model=%s", len(location_ids), args.model)

    summaries = []
    for lid in location_ids:
        summary = generate_brief_for_location(
            db, anthropic_client, lid, args.model, args.replace, args.dry_run,
        )
        summaries.append(summary)

    log.info("=" * 60)
    if args.dry_run:
        total_cost = sum(s.get("estimated_cost_usd", 0) for s in summaries)
        log.info("DRY RUN — total estimated cost across %d locations: ~$%.4f",
                 len(summaries), total_cost)
    else:
        skipped = sum(1 for s in summaries if s.get("skipped"))
        total_cost = sum(s.get("cost_usd", 0) for s in summaries)
        log.info(
            "Done. Wrote %d briefs (%d skipped). Total cost ~$%.4f.",
            len(summaries) - skipped, skipped, total_cost,
        )


if __name__ == "__main__":
    main()
