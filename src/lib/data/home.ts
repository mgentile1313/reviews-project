/**
 * Server-side data fetchers for the home page.
 *
 * Returns:
 *  - stats: a few aggregate numbers for the stats bar
 *  - themes: network themes with computed prevalence, top-3 above-median
 *    locations, and recent trend direction
 *
 * One Supabase round-trip per logical query. Server Components call these
 * directly; no client-side data fetching is involved.
 */

import { createServiceClient } from "@/lib/supabase";

export type HomeStats = {
  totalReviews: number;
  totalLocations: number;
  networkAvgRating: number | null;
  ratingDelta: number | null;
  improving: number;
  stable: number;
  degrading: number;
  briefGeneratedAt: string | null;
};

export type HomeTheme = {
  id: string;
  label: string;
  pass: "actions" | "working";
  memberCount: number;
  prevalence: number; // fraction within the same pass network-wide
  topLocations: { internalId: string; name: string; zScore: number }[];
  recentDirection: "improving" | "degrading" | "stable" | null;
};

export async function getHomeStats(): Promise<HomeStats> {
  const db = createServiceClient();

  const [reviews, locations, overallTrends, briefs] = await Promise.all([
    db.from("reviews").select("id", { count: "exact", head: true }),
    db.from("locations").select("id", { count: "exact", head: true }),
    db
      .from("trends")
      .select("recent_value, prior_value, delta, direction")
      .eq("scope", "overall"),
    db
      .from("briefs")
      .select("generated_at")
      .eq("status", "active")
      .order("generated_at", { ascending: false })
      .limit(1),
  ]);

  const overall = overallTrends.data ?? [];
  const ratings = overall
    .map((r) => r.recent_value as number | null)
    .filter((v): v is number => v != null);
  const deltas = overall
    .map((r) => r.delta as number | null)
    .filter((v): v is number => v != null);

  return {
    totalReviews: reviews.count ?? 0,
    totalLocations: locations.count ?? 0,
    networkAvgRating:
      ratings.length > 0
        ? ratings.reduce((a, b) => a + b, 0) / ratings.length
        : null,
    ratingDelta:
      deltas.length > 0
        ? deltas.reduce((a, b) => a + b, 0) / deltas.length
        : null,
    improving: overall.filter((r) => r.direction === "improving").length,
    stable: overall.filter((r) => r.direction === "stable").length,
    degrading: overall.filter((r) => r.direction === "degrading").length,
    briefGeneratedAt: briefs.data?.[0]?.generated_at ?? null,
  };
}

export async function getHomeThemes(): Promise<HomeTheme[]> {
  const db = createServiceClient();

  // Network specific themes
  const themesRes = await db
    .from("themes")
    .select("id, label, pass, member_count")
    .eq("scope", "network")
    .eq("specific", true);
  const themes = themesRes.data ?? [];

  // For "prevalence within pass": divide member_count by total reviews in
  // that pass network-wide. We need network-wide pass totals (sum of
  // member_count across all network themes per pass, including generic).
  const allNetworkRes = await db
    .from("themes")
    .select("pass, member_count")
    .eq("scope", "network");
  const passTotals: Record<string, number> = {};
  for (const t of allNetworkRes.data ?? []) {
    passTotals[t.pass] = (passTotals[t.pass] ?? 0) + (t.member_count ?? 0);
  }

  // All location_theme_scores in one go; group locally.
  const scoresRes = await db
    .from("location_theme_scores")
    .select("theme_id, location_id, z_score, direction")
    .not("z_score", "is", null);

  // Locations metadata for display
  const locsRes = await db
    .from("locations")
    .select("id, internal_id, name");
  const locById = new Map(
    (locsRes.data ?? []).map((l) => [l.id, { internalId: l.internal_id, name: l.name }]),
  );

  // Trends (theme scope) — group direction by theme. If a theme has more
  // "degrading" rows than "improving" or "stable", call it degrading; etc.
  const trendsRes = await db
    .from("trends")
    .select("theme_id, direction")
    .eq("scope", "theme")
    .not("direction", "is", null);
  const dirByTheme: Record<string, Record<string, number>> = {};
  for (const t of trendsRes.data ?? []) {
    const key = t.theme_id as string;
    dirByTheme[key] ??= {};
    const d = t.direction as string;
    dirByTheme[key][d] = (dirByTheme[key][d] ?? 0) + 1;
  }

  // For each theme, pick top-3 above-median locations by z_score
  const topByTheme: Record<string, { locId: string; zScore: number }[]> = {};
  for (const s of scoresRes.data ?? []) {
    if (s.direction !== "above") continue;
    const key = s.theme_id as string;
    topByTheme[key] ??= [];
    topByTheme[key].push({ locId: s.location_id as string, zScore: s.z_score as number });
  }
  for (const key in topByTheme) {
    topByTheme[key].sort((a, b) => b.zScore - a.zScore);
    topByTheme[key] = topByTheme[key].slice(0, 3);
  }

  // Assemble
  const out: HomeTheme[] = themes.map((t) => {
    const passTotal = passTotals[t.pass] ?? 0;
    const prevalence = passTotal > 0 ? (t.member_count ?? 0) / passTotal : 0;
    const topRaw = topByTheme[t.id] ?? [];
    const top = topRaw
      .map((r) => {
        const m = locById.get(r.locId);
        return m ? { internalId: m.internalId, name: m.name, zScore: r.zScore } : null;
      })
      .filter((v): v is { internalId: string; name: string; zScore: number } => v != null);

    const dir = dirByTheme[t.id] ?? {};
    let recent: "improving" | "degrading" | "stable" | null = null;
    const entries = Object.entries(dir);
    if (entries.length > 0) {
      entries.sort((a, b) => b[1] - a[1]);
      recent = entries[0][0] as "improving" | "degrading" | "stable";
    }

    return {
      id: t.id as string,
      label: (t.label as string) ?? "",
      pass: t.pass as "actions" | "working",
      memberCount: t.member_count as number,
      prevalence,
      topLocations: top,
      recentDirection: recent,
    };
  });

  // Sort by prevalence desc by default
  out.sort((a, b) => b.prevalence - a.prevalence);
  return out;
}
