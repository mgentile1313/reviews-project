/**
 * Locations overview / leaderboard data fetcher.
 *
 * Combines the `review_summary` view (per-location-per-source counts + means)
 * with the trends table (overall 90-day window) and the locations table for
 * profile data. No theme dimension — this is the location-header view.
 */

import { createServiceClient } from "@/lib/supabase";

export type LocationOverviewRow = {
  id: string;
  internalId: string;
  name: string;
  city: string | null;
  state: string | null;
  region: string | null;
  totalReviews: number;
  googleReviews: number;
  yelpReviews: number;
  currentRating: number | null;       // all-time weighted avg
  recentRatingMean: number | null;    // last 90 days
  priorRatingMean: number | null;     // prior 90 days
  ratingDelta: number | null;
  recentN: number | null;
  priorN: number | null;
  direction: "improving" | "degrading" | "stable" | null;
};

export type LocationsOverview = {
  rows: LocationOverviewRow[];
  network: {
    totalReviews: number;
    avgRating: number | null;
    improving: number;
    degrading: number;
    stable: number;
    best: LocationOverviewRow | null;
    worst: LocationOverviewRow | null;
    biggestImprover: LocationOverviewRow | null;
    biggestDecliner: LocationOverviewRow | null;
  };
};

export async function getLocationsOverview(): Promise<LocationsOverview> {
  const db = createServiceClient();

  const [locsRes, summaryRes, trendsRes] = await Promise.all([
    db.from("locations").select("id, internal_id, name, city, state, region"),
    db
      .from("review_summary")
      .select("internal_id, source, review_count, avg_rating"),
    db
      .from("trends")
      .select(
        "location_id, recent_value, prior_value, delta, recent_n, prior_n, direction",
      )
      .eq("scope", "overall"),
  ]);

  // Group summary rows by internal_id, combining google + yelp
  type Roll = {
    google: number;
    yelp: number;
    weightedSum: number;
    weightCount: number;
  };
  const byInternal = new Map<string, Roll>();
  for (const s of summaryRes.data ?? []) {
    const key = s.internal_id as string;
    const cur = byInternal.get(key) ?? {
      google: 0,
      yelp: 0,
      weightedSum: 0,
      weightCount: 0,
    };
    const n = (s.review_count as number) ?? 0;
    const avg = (s.avg_rating as number) ?? 0;
    if (s.source === "google") cur.google += n;
    else if (s.source === "yelp") cur.yelp += n;
    cur.weightedSum += avg * n;
    cur.weightCount += n;
    byInternal.set(key, cur);
  }

  const trendByLocId = new Map<
    string,
    {
      recent: number | null;
      prior: number | null;
      delta: number | null;
      recentN: number | null;
      priorN: number | null;
      direction: "improving" | "degrading" | "stable" | null;
    }
  >();
  for (const t of trendsRes.data ?? []) {
    trendByLocId.set(t.location_id as string, {
      recent: t.recent_value as number | null,
      prior: t.prior_value as number | null,
      delta: t.delta as number | null,
      recentN: t.recent_n as number | null,
      priorN: t.prior_n as number | null,
      direction: t.direction as "improving" | "degrading" | "stable" | null,
    });
  }

  const rows: LocationOverviewRow[] = (locsRes.data ?? []).map((l) => {
    const roll = byInternal.get(l.internal_id as string);
    const trend = trendByLocId.get(l.id as string);
    const total = (roll?.google ?? 0) + (roll?.yelp ?? 0);
    const currentRating =
      roll && roll.weightCount > 0 ? roll.weightedSum / roll.weightCount : null;
    return {
      id: l.id as string,
      internalId: l.internal_id as string,
      name: l.name as string,
      city: l.city as string | null,
      state: l.state as string | null,
      region: l.region as string | null,
      totalReviews: total,
      googleReviews: roll?.google ?? 0,
      yelpReviews: roll?.yelp ?? 0,
      currentRating,
      recentRatingMean: trend?.recent ?? null,
      priorRatingMean: trend?.prior ?? null,
      ratingDelta: trend?.delta ?? null,
      recentN: trend?.recentN ?? null,
      priorN: trend?.priorN ?? null,
      direction: trend?.direction ?? null,
    };
  });

  // Default sort: current rating desc, then total reviews desc for ties.
  rows.sort((a, b) => {
    const ar = a.currentRating ?? -1;
    const br = b.currentRating ?? -1;
    if (ar !== br) return br - ar;
    return b.totalReviews - a.totalReviews;
  });

  // Network summary
  const totalReviews = rows.reduce((acc, r) => acc + r.totalReviews, 0);
  const totalWeightedSum = rows.reduce(
    (acc, r) =>
      acc + (r.currentRating != null ? r.currentRating * r.totalReviews : 0),
    0,
  );
  const avgRating = totalReviews > 0 ? totalWeightedSum / totalReviews : null;
  const ranked = rows.filter((r) => r.currentRating != null);
  const best = ranked[0] ?? null;
  const worst = ranked[ranked.length - 1] ?? null;

  // Biggest movers (require a non-null delta)
  const withDelta = rows.filter((r) => r.ratingDelta != null);
  const biggestImprover =
    withDelta.length > 0
      ? withDelta.reduce((a, b) =>
          (a.ratingDelta ?? 0) > (b.ratingDelta ?? 0) ? a : b,
        )
      : null;
  const biggestDecliner =
    withDelta.length > 0
      ? withDelta.reduce((a, b) =>
          (a.ratingDelta ?? 0) < (b.ratingDelta ?? 0) ? a : b,
        )
      : null;

  return {
    rows,
    network: {
      totalReviews,
      avgRating,
      improving: rows.filter((r) => r.direction === "improving").length,
      degrading: rows.filter((r) => r.direction === "degrading").length,
      stable: rows.filter((r) => r.direction === "stable").length,
      best,
      worst,
      biggestImprover:
        biggestImprover && (biggestImprover.ratingDelta ?? 0) > 0
          ? biggestImprover
          : null,
      biggestDecliner:
        biggestDecliner && (biggestDecliner.ratingDelta ?? 0) < 0
          ? biggestDecliner
          : null,
    },
  };
}
