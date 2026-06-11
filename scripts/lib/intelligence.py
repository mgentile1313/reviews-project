"""The keystone intelligence layer for the reviews project.

`get_location_intelligence(location_id)` returns everything a brief
generator, dashboard view, or MCP server tool needs to answer questions
about one location. Pure function — id in, structured dict out. No
computation, just structured assembly from the precomputed tables:

  - themes (per-location + network)
  - location_theme_scores (prevalence, z_score, direction)
  - trends (overall + per-theme, 90d vs prior 90d)
  - locations (facts and metadata)

Every surface that wraps a brief or a query reads through this. The schema
of the returned dict is the contract between this layer and everything
above it.
"""

from __future__ import annotations

import logging
from statistics import median as _median

from .db import get_client as get_db

log = logging.getLogger(__name__)

# When the action pass produces this few or fewer SPECIFIC themes, the
# intelligence packet includes a `raw_action_reviews` payload so the brief
# generator can read negative reviews directly. Threshold of 2 means
# Lakewood-style locations (0 specific action themes) and locations with
# only 1-2 specific action themes get the fallback. Locations with 3+
# specific action themes are considered well-clustered and don't.
RAW_ACTION_FALLBACK_THRESHOLD = 2

# How many raw reviews to surface when the fallback fires. The brief
# generator wants enough to spot patterns but not so many that it drowns.
RAW_ACTION_TOP_N = 10


def get_location_intelligence(location_id: str, db=None) -> dict:
    """Assemble the intelligence packet for one location.

    Returns a dict with the following shape:

        {
          "location":         {id, internal_id, name, brand, city, state, ...},
          "local_themes":     [theme rows, sorted by pass then prevalence desc],
          "anomalies":        [score rows joined with theme metadata,
                               only z-scored rows, sorted by |z_score| desc],
          "overall_trend":    {recent_value, prior_value, delta, direction, ...}
                              or None if not computed,
          "theme_trends":     [trend rows joined with theme metadata,
                               only direction-non-null rows, sorted by |delta| desc],
          "network_context":  {total_locations, as_of, window_days},
        }

    Pass `db` to reuse an existing client; one is created if omitted.
    """
    db = db or get_db()

    location = _fetch_location(db, location_id)
    if location is None:
        raise ValueError(f"location {location_id} not found")

    network_themes_by_id = _fetch_network_themes_by_id(db)
    local_themes = _fetch_local_themes(db, location_id)

    return {
        "location": location,
        "local_themes": local_themes,
        "raw_action_reviews": _maybe_fetch_raw_action_reviews(db, location_id, local_themes),
        "anomalies": _fetch_anomalies(db, location_id, network_themes_by_id),
        "overall_trend": _fetch_overall_trend(db, location_id),
        "theme_trends": _fetch_theme_trends(db, location_id, network_themes_by_id),
        "network_context": _fetch_network_context(db, network_themes_by_id),
    }


# ---------------------------------------------------------------------------
# Internal fetchers — each one queries one table, returns plain structures.
# ---------------------------------------------------------------------------

def _fetch_location(db, location_id: str) -> dict | None:
    res = (
        db.table("locations")
        .select("id, internal_id, brand, name, city, state, zip, region, "
                "performance_signal, google_url, yelp_url, notes")
        .eq("id", location_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _maybe_fetch_raw_action_reviews(
    db,
    location_id: str,
    local_themes: list[dict],
) -> list[dict]:
    """Fallback signal: when the action pass produced too few specific themes,
    return the top N negative reviews so the brief generator can read them
    directly. Otherwise returns empty list.

    Selection: 1-3★ reviews at this location, sorted by rating ascending then
    text length descending, excluding any review already chosen as a cluster
    representative (so the brief generator gets incremental evidence, not
    duplicates of what's already in `local_themes`).
    """
    specific_action_count = sum(
        1 for t in local_themes
        if t.get("pass") == "actions" and t.get("specific")
    )
    if specific_action_count > RAW_ACTION_FALLBACK_THRESHOLD:
        return []

    # Build exclusion set from existing local themes' representative IDs.
    excluded: set[str] = set()
    for t in local_themes:
        for rid in (t.get("representative_review_ids") or []):
            excluded.add(rid)

    res = (
        db.table("reviews")
        .select("id, rating, source, posted_at, text")
        .eq("location_id", location_id)
        .in_("rating", [1, 2, 3])
        .filter("text", "not.is", "null")
        .execute()
    )
    rows = res.data or []
    # Sort: rating asc (1★ before 2★ before 3★), then text length desc.
    rows.sort(key=lambda r: (r.get("rating") or 5, -len(r.get("text") or "")))
    # Exclude cluster representatives.
    filtered = [r for r in rows if r["id"] not in excluded]
    return filtered[:RAW_ACTION_TOP_N]


def _fetch_local_themes(db, location_id: str) -> list[dict]:
    """All themes for this location, sorted by pass then prevalence desc.

    Specific themes are enriched with `candidate_quotes` — a list of up to
    3 verbatim quotes from the cluster, each with full attribution
    (source, posted_at, rating, review_id). These come from the
    `evidence_quotes` jsonb column populated at labeling time by Stage 3,
    and the source_review_id stored alongside each quote lets us attach
    attribution by direct lookup (no substring matching).

    The brief generator picks 1-3 of these per evidence section.
    """
    res = (
        db.table("themes")
        .select(
            "id, pass, specific, label, evidence_quote, evidence_quotes, "
            "rejection_reason, representative_review_ids, prevalence, "
            "avg_rating, member_count, status, first_seen_at, last_seen_at"
        )
        .eq("location_id", location_id)
        .eq("scope", "location")
        .execute()
    )
    themes = res.data or []
    themes.sort(key=lambda t: (t["pass"], -(t.get("prevalence") or 0)))
    _attach_candidate_quotes(db, themes)
    return themes


def _attach_candidate_quotes(db, themes: list[dict]) -> None:
    """For each specific theme, expand `evidence_quotes` (jsonb stored at
    labeling time) into `candidate_quotes` — each item carries the quote
    text plus the source review's source/date/rating, looked up via the
    stored source_review_id.

    Modifies themes in place. Sets candidate_quotes=[] when a theme has
    no evidence_quotes payload (e.g., older themes labeled before Stage 3
    shipped, or themes where Stage 3 produced nothing).
    """
    # Gather every source_review_id referenced across all themes.
    all_source_ids: set[str] = set()
    for t in themes:
        if not t.get("specific"):
            continue
        for q in (t.get("evidence_quotes") or []):
            rid = q.get("source_review_id") if isinstance(q, dict) else None
            if rid:
                all_source_ids.add(rid)

    if not all_source_ids:
        for t in themes:
            t["candidate_quotes"] = []
        return

    res = (
        db.table("reviews")
        .select("id, source, posted_at, rating")
        .in_("id", list(all_source_ids))
        .execute()
    )
    by_id = {r["id"]: r for r in (res.data or [])}

    for t in themes:
        cqs: list[dict] = []
        for q in (t.get("evidence_quotes") or []):
            if not isinstance(q, dict):
                continue
            quote_text = q.get("quote")
            rid = q.get("source_review_id")
            if not quote_text or not rid:
                continue
            meta = by_id.get(rid)
            cqs.append({
                "quote": quote_text,
                "review_id": rid,
                "source": meta.get("source") if meta else None,
                "posted_at": meta.get("posted_at") if meta else None,
                "rating": meta.get("rating") if meta else None,
            })
        t["candidate_quotes"] = cqs


def _fetch_network_themes_by_id(db) -> dict[str, dict]:
    """All specific + generic network themes keyed by id, for joining."""
    res = (
        db.table("themes")
        .select("id, pass, specific, label")
        .eq("scope", "network")
        .execute()
    )
    return {t["id"]: t for t in res.data or []}


def _fetch_anomalies(
    db,
    location_id: str,
    network_themes_by_id: dict[str, dict],
) -> list[dict]:
    """Score rows for this location, joined with theme metadata.

    Excludes rows where z_score is NULL (the guarded thin-pool locations).
    Sorted by absolute z_score descending — biggest outliers (both above and
    below network median) come first.
    """
    res = (
        db.table("location_theme_scores")
        .select("theme_id, prevalence, z_score, direction")
        .eq("location_id", location_id)
        .execute()
    )
    scores = res.data or []

    # Compute network medians per theme so we can return that as context.
    medians = _fetch_network_medians(db)

    out: list[dict] = []
    for s in scores:
        if s.get("z_score") is None:
            continue
        theme = network_themes_by_id.get(s["theme_id"])
        if not theme:
            continue
        out.append({
            "theme_id": s["theme_id"],
            "theme_label": theme.get("label"),
            "theme_pass": theme.get("pass"),
            "theme_specific": theme.get("specific"),
            "prevalence": s.get("prevalence"),
            "z_score": s.get("z_score"),
            "direction": s.get("direction"),
            "network_median": medians.get(s["theme_id"]),
        })
    out.sort(key=lambda r: -abs(r["z_score"] or 0))
    return out


def _fetch_network_medians(db) -> dict[str, float]:
    """Per-theme median of prevalence across all 34 locations.

    Computed live from location_theme_scores. Cheap (one table scan, ~544 rows).
    """
    res = db.table("location_theme_scores").select("theme_id, prevalence").execute()
    rows = res.data or []
    by_theme: dict[str, list[float]] = {}
    for r in rows:
        by_theme.setdefault(r["theme_id"], []).append(r.get("prevalence") or 0.0)
    return {tid: _median(vals) for tid, vals in by_theme.items() if vals}


def _fetch_overall_trend(db, location_id: str) -> dict | None:
    res = (
        db.table("trends")
        .select(
            "recent_value, prior_value, recent_n, prior_n, delta, direction, "
            "window_days, as_of"
        )
        .eq("location_id", location_id)
        .eq("scope", "overall")
        .execute()
    )
    return res.data[0] if res.data else None


def _fetch_theme_trends(
    db,
    location_id: str,
    network_themes_by_id: dict[str, dict],
) -> list[dict]:
    """Per-theme trend rows for this location, joined with theme metadata.

    Excludes rows where direction is NULL (the thin-window guarded ones).
    Sorted by |delta| descending — most-moved themes first.
    """
    res = (
        db.table("trends")
        .select(
            "theme_id, recent_value, prior_value, recent_n, prior_n, delta, "
            "direction, window_days, as_of"
        )
        .eq("location_id", location_id)
        .eq("scope", "theme")
        .execute()
    )
    rows = res.data or []
    out: list[dict] = []
    for r in rows:
        if r.get("direction") is None:
            continue
        theme = network_themes_by_id.get(r["theme_id"])
        if not theme:
            continue
        out.append({
            "theme_id": r["theme_id"],
            "theme_label": theme.get("label"),
            "theme_pass": theme.get("pass"),
            "theme_specific": theme.get("specific"),
            "recent_value": r.get("recent_value"),
            "prior_value": r.get("prior_value"),
            "recent_n": r.get("recent_n"),
            "prior_n": r.get("prior_n"),
            "delta": r.get("delta"),
            "direction": r.get("direction"),
        })
    out.sort(key=lambda r: -abs(r.get("delta") or 0))
    return out


def _fetch_network_context(
    db,
    network_themes_by_id: dict[str, dict],
) -> dict:
    """Counts and bookkeeping that consumers might want alongside the
    location-specific data."""
    # Total locations in the network. We could count distinct location_ids in
    # reviews instead, but the locations table is the canonical answer.
    loc_res = db.table("locations").select("id").execute()
    overall_res = (
        db.table("trends")
        .select("as_of, window_days")
        .eq("scope", "overall")
        .limit(1)
        .execute()
    )
    overall = (overall_res.data or [{}])[0]
    return {
        "total_locations": len(loc_res.data or []),
        "total_network_themes": len(network_themes_by_id),
        "specific_network_themes": sum(
            1 for t in network_themes_by_id.values() if t.get("specific")
        ),
        "as_of": overall.get("as_of"),
        "window_days": overall.get("window_days"),
    }
