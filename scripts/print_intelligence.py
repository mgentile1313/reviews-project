"""Driver script: pretty-print get_location_intelligence(location_id) output,
or run an ad-hoc semantic search.

For Day 7 validation — eyeball the assembled intelligence packet for each of
the 3 ground-truth locations and judge whether it's rich enough to drive a
brief from.

Usage:
    python -m scripts.print_intelligence --location-id <uuid>
    python -m scripts.print_intelligence --internal-id mavis_019
    python -m scripts.print_intelligence --internal-id mavis_019 --json
    python -m scripts.print_intelligence --search "engine failure after oil change"
    python -m scripts.print_intelligence --internal-id mavis_002 --search "rude manager"
"""

from __future__ import annotations

import argparse
import json
import sys

from scripts.lib.db import get_client as get_db
from scripts.lib.intelligence import get_location_intelligence
from scripts.lib.search import search_reviews


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--location-id", help="Location UUID.")
    g.add_argument("--internal-id", help="Resolve internal_id (e.g. mavis_019) → UUID.")
    p.add_argument("--json", action="store_true", help="Dump the raw JSON instead of pretty-print.")
    p.add_argument("--search", metavar="QUERY",
                   help="Run semantic search instead of printing intelligence. "
                        "Scoped to --location-id/--internal-id if given, otherwise network-wide.")
    p.add_argument("--top-n", type=int, default=10, help="Top N results for --search (default 10).")
    p.add_argument("--min-similarity", type=float, default=0.0,
                   help="Floor on cosine similarity in [0,1] (default 0.0).")
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


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."


def pretty_print(intel: dict) -> None:
    loc = intel["location"]
    ctx = intel["network_context"]
    print()
    print("=" * 90)
    print(f"{loc['name']} ({loc['internal_id']})")
    print(f"{loc.get('city','?')}, {loc.get('state','?')}  |  region={loc.get('region','?')}")
    print("=" * 90)

    ot = intel.get("overall_trend")
    if ot:
        print()
        print(f"OVERALL TREND ({ot['window_days']}d vs prior {ot['window_days']}d, as_of {ot['as_of'][:10]}):")
        print(
            f"  recent={ot['recent_value']:.2f}★ (n={ot['recent_n']})  "
            f"prior={ot['prior_value']:.2f}★ (n={ot['prior_n']})  "
            f"Δ={ot['delta']:+.2f}  →  {ot['direction'] or 'no call'}"
        )

    # Local themes
    print()
    print(f"LOCAL THEMES ({len(intel['local_themes'])}):")
    for t in intel["local_themes"]:
        mark = "✓" if t["specific"] else "✗"
        label = t["label"] or "[generic]"
        print(
            f"  {mark} [{t['pass']:7s}]  n={t['member_count']:3d}  "
            f"prev={(t.get('prevalence') or 0)*100:5.1f}%  {label}"
        )
        if t["specific"] and t.get("evidence_quote"):
            print(f"      \"{_truncate(t['evidence_quote'], 140)}\"")

    # Raw action reviews (fallback when action clustering was thin)
    raw = intel.get("raw_action_reviews") or []
    if raw:
        print()
        print(f"RAW ACTION REVIEWS — fallback fired ({len(raw)} reviews, brief generator reads these directly):")
        for r in raw:
            print(
                f"  [{r.get('source') or '?':6s} {r.get('rating') or '?'}★ "
                f"{(r.get('posted_at') or '----')[:10]}] "
                f"{_truncate(r.get('text'), 200)}"
            )

    # Anomalies
    print()
    print(f"ANOMALIES vs NETWORK ({len(intel['anomalies'])}, sorted by |z|):")
    for a in intel["anomalies"][:8]:
        d = a.get("direction") or "·"
        arrow = "⬆️" if d == "above" else ("⬇️" if d == "below" else "·")
        label = a["theme_label"] or "[generic]"
        print(
            f"  {arrow} [{a['theme_pass']:7s}]  z={a['z_score']:+5.2f}  "
            f"prev={(a.get('prevalence') or 0)*100:5.1f}%  "
            f"(median {(a.get('network_median') or 0)*100:5.1f}%)  {label[:60]}"
        )

    # Theme trends
    print()
    print(f"THEME TRENDS ({len(intel['theme_trends'])}, sorted by |Δ|):")
    for t in intel["theme_trends"][:8]:
        label = t["theme_label"] or "[generic]"
        print(
            f"  [{t['theme_pass']:7s}]  recent={(t.get('recent_value') or 0)*100:5.1f}%  "
            f"prior={(t.get('prior_value') or 0)*100:5.1f}%  "
            f"Δ={(t.get('delta') or 0)*100:+5.1f}pp  →  {t['direction']}  {label[:50]}"
        )

    print()
    print(f"NETWORK CONTEXT: {ctx['total_locations']} locations, "
          f"{ctx['specific_network_themes']}/{ctx['total_network_themes']} specific network themes")
    print()


def pretty_print_search(query: str, location_id: str | None, results: list[dict]) -> None:
    scope = f"location_id={location_id}" if location_id else "network-wide"
    print()
    print("=" * 90)
    print(f"SEARCH: \"{query}\"  ({scope})")
    print("=" * 90)
    if not results:
        print("  (no matches)")
        return
    for r in results:
        sim = r.get("similarity") or 0
        print()
        print(
            f"  [{r.get('source') or '?':6s} {r.get('rating') or '?'}★ "
            f"{(r.get('posted_at') or '----')[:10]}]  similarity={sim:.3f}"
        )
        print(f"  {_truncate(r.get('text'), 400)}")
    print()


def main() -> None:
    args = parse_args()
    db = get_db()
    location_id = resolve_location(db, args)

    if args.search:
        results = search_reviews(
            query=args.search,
            location_id=location_id,
            top_n=args.top_n,
            min_similarity=args.min_similarity,
            db=db,
        )
        if args.json:
            print(json.dumps(results, indent=2, default=str))
        else:
            pretty_print_search(args.search, location_id, results)
        return

    if not location_id:
        sys.exit("must provide --location-id or --internal-id (or use --search)")

    intel = get_location_intelligence(location_id, db=db)
    if args.json:
        print(json.dumps(intel, indent=2, default=str))
    else:
        pretty_print(intel)


if __name__ == "__main__":
    main()
