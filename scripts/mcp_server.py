"""Reviews Intelligence MCP server.

Exposes the Mavis review-intelligence dataset (locations, reviews, themes,
briefs, anomalies, trends) as tools an MCP client can call.

Transport: stdio (Claude Desktop launches this as a subprocess).
Run standalone for debugging:
    python -m scripts.mcp_server         # stdio mode, mostly silent
    npx @modelcontextprotocol/inspector \
        python -m scripts.mcp_server     # interactive tool tester

All tools read from the same Supabase project the dashboard uses. No
writes — this is a read-only intelligence surface.

Tool design principles (worth keeping in mind as we add more):
  - One tool = one well-scoped job. No god-tools that take a `mode` arg.
  - Docstrings are written for the model, not for humans. They tell the
    model WHEN to use the tool, what each arg means, and what comes back.
  - Errors are raised, not returned in a `{"error": ...}` envelope. The
    SDK turns them into messages the model reads and recovers from.
  - location_id and theme_id are UUIDs from list_locations / list_themes.
    We also accept internal_id ('mavis_001') for locations since the
    model may have it in context.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .lib.db import get_client as get_db
from .lib.search import search_reviews as _semantic_search

# Stderr-only logging so Claude Desktop doesn't try to parse log lines as
# MCP protocol messages on stdout.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reviews-intelligence-mcp")

mcp = FastMCP("reviews-intelligence")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_location_id(location_id: str) -> str:
    """Return the canonical UUID for a location, accepting either a UUID
    string or an internal_id like 'mavis_001'."""
    db = get_db()
    if location_id.startswith("mavis_"):
        res = (
            db.table("locations")
            .select("id")
            .eq("internal_id", location_id)
            .single()
            .execute()
        )
        return res.data["id"]
    # Assume it's already a UUID; let downstream queries validate it.
    return location_id


# ---------------------------------------------------------------------------
# Tool 1: list_locations
# ---------------------------------------------------------------------------

@mcp.tool()
def list_locations() -> list[dict]:
    """List every Mavis store in the network as a directory.

    Use this FIRST when the user mentions a store by name (e.g. "Park Rd",
    "Lakewood") and you need its UUID to call any other tool. Also useful
    as a sanity check on how many stores exist (~34) and where they are.

    Returns one dict per location with:
      - id: UUID, used by every other location-scoped tool
      - internal_id: stable short code like 'mavis_001' (also accepted as
        an ID by other tools)
      - name: full Mavis brand + location string, e.g.
        'Mavis Tires & Brakes - Lovejoy GA'
      - city, state: geographic
    """
    db = get_db()
    res = (
        db.table("locations")
        .select("id, internal_id, name, city, state")
        .order("name")
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Tool 2: list_themes
# ---------------------------------------------------------------------------

@mcp.tool()
def list_themes(pass_filter: Literal["actions", "working"] | None = None) -> list[dict]:
    """List the network-wide specific themes that customers are talking about.

    A 'theme' here is a cluster of reviews about the same recurring topic
    (e.g. "Staff replaces unauthorized tires and charges inflated price").
    Only 'specific' network-level themes are returned — these are the ones
    distinct enough to act on. There are ~10 of them: roughly 6 action
    items (from negative reviews) and 4 what's-working themes (from
    positive reviews).

    Use this when the user asks about a category of complaint or strength,
    or before calling get_theme_prevalence (you need a theme_id).

    Args:
      pass_filter: 'actions' for negative-review themes, 'working' for
        positive-review themes, or omit for both.

    Returns one dict per theme with:
      - id: UUID, used by get_theme_prevalence
      - label: human-readable theme name
      - pass: 'actions' or 'working'
      - member_count: how many reviews network-wide are in this theme
    """
    db = get_db()
    q = (
        db.table("themes")
        .select("id, label, pass, member_count")
        .eq("scope", "network")
        .eq("specific", True)
    )
    if pass_filter is not None:
        q = q.eq("pass", pass_filter)
    res = q.order("member_count", desc=True).execute()
    return res.data or []


# ---------------------------------------------------------------------------
# Tool 3: search_reviews
# ---------------------------------------------------------------------------

@mcp.tool()
def search_reviews(
    query: str,
    location_id: str | None = None,
    min_similarity: float = 0.5,
    limit: int = 10,
) -> list[dict]:
    """Search the customer-review corpus by semantic similarity.

    Use this when the user asks about a TOPIC or EXPERIENCE — anything
    where keyword search would miss because customers phrase the same
    complaint many ways. Example queries that work well:
      - "safety issues with brakes"
      - "manager was rude during checkout"
      - "wait time was unexpectedly long"
      - "customer felt pressured into extra services"

    The query is embedded with the same OpenAI model used at ingest time;
    reviews are ranked by cosine similarity to it. Returns up to `limit`
    reviews above `min_similarity` (0-1), sorted most similar first.

    IMPORTANT: if no reviews exceed min_similarity, the result will be an
    empty list. That means the topic is NOT discussed in customer reviews
    — answer the user honestly, do NOT invent reviews or lower the bar.

    Args:
      query: a topic, complaint, or experience phrased naturally
      location_id: scope to one store (UUID or internal_id); omit for
        network-wide search
      min_similarity: floor in [0, 1]. 0.5 (default) returns honest
        empties; 0.3 returns more loosely related; 0.7 only very close
        matches.
      limit: max reviews returned (default 10)

    Returns one dict per review with:
      - id, location_id: identifiers
      - rating: 1-5 stars
      - source: 'google' or 'yelp'
      - posted_at: ISO date
      - text: full review text
      - similarity: 0-1, how close to the query
    """
    resolved = _resolve_location_id(location_id) if location_id else None
    return _semantic_search(
        query,
        location_id=resolved,
        top_n=limit,
        min_similarity=min_similarity,
    )


# ---------------------------------------------------------------------------
# Tool 4: get_location_stats
# ---------------------------------------------------------------------------

@mcp.tool()
def get_location_stats(location_id: str) -> dict:
    """Quantitative profile of one Mavis location.

    Use this for any "how is store X doing?" question that's about
    numbers, not themes or specific reviews. Returns the same header
    metrics the dashboard shows on the location page: current rating,
    review counts, and a last-90-days-vs-prior-90-days trend window.

    Args:
      location_id: UUID or internal_id of the location

    Returns:
      - name, internal_id, city, state
      - total_reviews: lifetime
      - google_reviews, yelp_reviews: source breakdown
      - current_rating: lifetime weighted average (0-5)
      - recent_rating_90d, prior_rating_90d: window means
      - rating_delta: recent - prior, signed
      - recent_n, prior_n: review counts per window
      - direction: 'improving' | 'stable' | 'degrading' | None (None if
        the prior window had too few reviews to call a trend)
    """
    db = get_db()
    loc_id = _resolve_location_id(location_id)

    # Location profile
    loc = (
        db.table("locations")
        .select("internal_id, name, city, state")
        .eq("id", loc_id)
        .single()
        .execute()
    ).data

    # Source counts + avg rating via review_summary view
    summary = (
        db.table("review_summary")
        .select("source, review_count, avg_rating")
        .eq("internal_id", loc["internal_id"])
        .execute()
    ).data or []
    google = next((r for r in summary if r["source"] == "google"), None)
    yelp = next((r for r in summary if r["source"] == "yelp"), None)
    g_n = google["review_count"] if google else 0
    y_n = yelp["review_count"] if yelp else 0
    total_n = g_n + y_n
    weighted = (
        (google["avg_rating"] * g_n if google else 0)
        + (yelp["avg_rating"] * y_n if yelp else 0)
    )
    current_rating = weighted / total_n if total_n > 0 else None

    # Overall trend
    trend = (
        db.table("trends")
        .select("recent_value, prior_value, delta, recent_n, prior_n, direction")
        .eq("location_id", loc_id)
        .eq("scope", "overall")
        .maybe_single()
        .execute()
    ).data

    return {
        "internal_id": loc["internal_id"],
        "name": loc["name"],
        "city": loc["city"],
        "state": loc["state"],
        "total_reviews": total_n,
        "google_reviews": g_n,
        "yelp_reviews": y_n,
        "current_rating": current_rating,
        "recent_rating_90d": trend["recent_value"] if trend else None,
        "prior_rating_90d": trend["prior_value"] if trend else None,
        "rating_delta": trend["delta"] if trend else None,
        "recent_n": trend["recent_n"] if trend else None,
        "prior_n": trend["prior_n"] if trend else None,
        "direction": trend["direction"] if trend else None,
    }


# ---------------------------------------------------------------------------
# Tool 5: get_location_brief
# ---------------------------------------------------------------------------

@mcp.tool()
def get_location_brief(location_id: str) -> dict:
    """Return the active manager brief for one Mavis location.

    The brief is a Claude-generated markdown report written for the store
    manager: top action items with concrete interventions, a watchlist,
    what's working, and labeler notes. It's the richest single artifact
    per location.

    Use this when the user asks broadly "what's going on at store X" or
    "what should the manager focus on." For specific numerical questions,
    prefer get_location_stats. For specific complaint topics, prefer
    search_reviews. For a structured outlier list, prefer
    get_location_anomalies.

    Args:
      location_id: UUID or internal_id of the location

    Returns:
      - location_name: full Mavis name
      - content: full markdown brief
      - generated_at: ISO datetime
      - intelligence_as_of: ISO date — snapshot the brief was built from
    """
    db = get_db()
    loc_id = _resolve_location_id(location_id)

    loc = (
        db.table("locations")
        .select("name")
        .eq("id", loc_id)
        .single()
        .execute()
    ).data

    brief = (
        db.table("briefs")
        .select("content, generated_at, intelligence_as_of")
        .eq("location_id", loc_id)
        .eq("status", "active")
        .order("generated_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    ).data

    if brief is None:
        raise ValueError(f"No active brief for location {location_id}")

    return {
        "location_name": loc["name"],
        "content": brief["content"],
        "generated_at": brief["generated_at"],
        "intelligence_as_of": brief["intelligence_as_of"],
    }


# ---------------------------------------------------------------------------
# Tool 6: get_location_anomalies
# ---------------------------------------------------------------------------

@mcp.tool()
def get_location_anomalies(location_id: str) -> list[dict]:
    """Themes where one location is statistically above or below the
    network median.

    Use this when the user asks "what is store X uniquely worse/better at
    compared to other stores?" — i.e., where the location is an outlier
    in either direction. Anomalies are based on z-scores of the
    location's prevalence relative to the rest of the network.

    Locations with too few reviews in the relevant pass (action vs
    working) get NO entries here — the sample is too thin for an honest
    anomaly call. That's not a bug; respect the silence.

    Args:
      location_id: UUID or internal_id of the location

    Returns one dict per anomaly theme, sorted by |z_score| descending:
      - theme_id, theme_label, pass ('actions' or 'working')
      - prevalence: 0-1, share of same-pass reviews this theme covers at
        this location
      - z_score: signed, > 0 means above network median
      - direction: 'above' or 'below'

    Interpreting direction × pass:
      - actions + above = WORSE than peers on this complaint
      - actions + below = better than peers on this complaint
      - working + above = STRONGER than peers on this strength
      - working + below = weaker than peers on this strength
    """
    db = get_db()
    loc_id = _resolve_location_id(location_id)

    scores = (
        db.table("location_theme_scores")
        .select("theme_id, prevalence, z_score, direction")
        .eq("location_id", loc_id)
        .not_.is_("direction", "null")
        .not_.is_("z_score", "null")
        .execute()
    ).data or []

    if not scores:
        return []

    theme_ids = [s["theme_id"] for s in scores]
    themes = (
        db.table("themes")
        .select("id, label, pass")
        .in_("id", theme_ids)
        .eq("specific", True)
        .execute()
    ).data or []
    by_id = {t["id"]: t for t in themes}

    out = []
    for s in scores:
        t = by_id.get(s["theme_id"])
        if t is None:
            continue
        out.append({
            "theme_id": s["theme_id"],
            "theme_label": t["label"],
            "pass": t["pass"],
            "prevalence": s["prevalence"],
            "z_score": s["z_score"],
            "direction": s["direction"],
        })
    out.sort(key=lambda r: abs(r["z_score"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Tool 7: get_location_reviews_recent
# ---------------------------------------------------------------------------

@mcp.tool()
def get_location_reviews_recent(location_id: str, limit: int = 20) -> list[dict]:
    """Most recent reviews at one location, in chronological order.

    Use this when the user wants the latest customer voice — "what are
    people saying RIGHT NOW at store X" — rather than searching by topic.
    For topical search, use search_reviews instead.

    Args:
      location_id: UUID or internal_id of the location
      limit: max reviews to return (default 20, max 100)

    Returns one dict per review, posted_at descending:
      - id
      - rating: 1-5
      - source: 'google' or 'yelp'
      - posted_at: ISO date
      - author: author display name (may be null)
      - text: review body (may be null for rating-only reviews)
      - owner_response: store response if any (may be null)
    """
    db = get_db()
    loc_id = _resolve_location_id(location_id)
    capped = max(1, min(limit, 100))
    res = (
        db.table("reviews")
        .select("id, rating, source, posted_at, author, text, owner_response")
        .eq("location_id", loc_id)
        .order("posted_at", desc=True)
        .limit(capped)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Tool 8: get_theme_prevalence
# ---------------------------------------------------------------------------

@mcp.tool()
def get_theme_prevalence(theme_id: str) -> dict:
    """How prevalent is one theme across every location in the network?

    Use this when the user asks "which stores are worst (or best) on
    theme X" — the inverse of get_location_anomalies. Returns the
    theme's profile plus a per-location distribution sorted by prevalence.

    Args:
      theme_id: UUID from list_themes

    Returns:
      - theme_label, pass ('actions' or 'working'), member_count: theme profile
      - network_prevalence: 0-1, share of same-pass reviews this theme
        covers network-wide
      - locations: list of {location_id, name, internal_id, prevalence,
        z_score, direction}, sorted by prevalence descending

      For each location:
        - prevalence: share of this location's same-pass reviews in this theme
        - z_score: NULL if guarded (n too small); signed otherwise
        - direction: 'above' or 'below' median, or NULL if guarded
    """
    db = get_db()
    theme = (
        db.table("themes")
        .select("label, pass, member_count")
        .eq("id", theme_id)
        .eq("scope", "network")
        .eq("specific", True)
        .single()
        .execute()
    ).data

    # Network prevalence denominator = total reviews in same pass across
    # all specific network themes
    all_pass = (
        db.table("themes")
        .select("member_count")
        .eq("scope", "network")
        .eq("pass", theme["pass"])
        .execute()
    ).data or []
    pass_total = sum(t["member_count"] or 0 for t in all_pass)
    network_prev = theme["member_count"] / pass_total if pass_total else 0

    scores = (
        db.table("location_theme_scores")
        .select("location_id, prevalence, z_score, direction")
        .eq("theme_id", theme_id)
        .execute()
    ).data or []

    locs = (
        db.table("locations")
        .select("id, internal_id, name")
        .execute()
    ).data or []
    by_id = {l["id"]: l for l in locs}

    distribution = []
    for s in scores:
        l = by_id.get(s["location_id"])
        if l is None:
            continue
        distribution.append({
            "location_id": s["location_id"],
            "internal_id": l["internal_id"],
            "name": l["name"],
            "prevalence": s["prevalence"],
            "z_score": s["z_score"],
            "direction": s["direction"],
        })
    distribution.sort(key=lambda r: r["prevalence"] or 0, reverse=True)

    return {
        "theme_label": theme["label"],
        "pass": theme["pass"],
        "member_count": theme["member_count"],
        "network_prevalence": network_prev,
        "locations": distribution,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("starting reviews-intelligence MCP server on stdio")
    mcp.run()  # default transport: stdio
