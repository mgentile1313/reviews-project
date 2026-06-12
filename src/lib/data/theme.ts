/**
 * Server-side data fetcher for the theme detail page.
 *
 * Returns the network theme's metadata, a per-location distribution
 * (prevalence + z-score), the evidence quotes with attribution, and
 * an aggregate trend signal across locations.
 */

import { createServiceClient } from "@/lib/supabase";
import type { Pass } from "@/lib/labels";

export type ThemeLocationRow = {
  locationId: string;
  internalId: string;
  name: string;
  prevalence: number;
  zScore: number | null;
  direction: "above" | "below" | null;
};

export type ThemeQuote = {
  quote: string;
  source: string | null;
  postedAt: string | null;
  rating: number | null;
};

export type ThemeDetail = {
  id: string;
  label: string;
  pass: Pass;
  memberCount: number;
  // Computed:
  networkPrevalence: number;     // member_count / total in same pass
  networkMedian: number;          // median of per-location prevalences
  locations: ThemeLocationRow[];  // sorted by prevalence desc
  quotes: ThemeQuote[];
  trendSummary: {
    improving: number;
    stable: number;
    degrading: number;
    avgDelta: number | null;       // mean of per-location deltas (signed)
  };
};

export async function getThemeDetail(themeId: string): Promise<ThemeDetail | null> {
  const db = createServiceClient();

  // Theme metadata — only specific network themes have first-class detail pages.
  // Catch-all clusters (specific=false, label NULL) 404 instead of rendering blank.
  const themeRes = await db
    .from("themes")
    .select(
      "id, label, pass, member_count, evidence_quotes, scope, specific",
    )
    .eq("id", themeId)
    .eq("scope", "network")
    .eq("specific", true)
    .maybeSingle();
  if (themeRes.error || !themeRes.data) return null;
  const theme = themeRes.data;

  // Total reviews in this pass network-wide for prevalence denominator
  const allPassRes = await db
    .from("themes")
    .select("member_count")
    .eq("scope", "network")
    .eq("pass", theme.pass);
  const passTotal = (allPassRes.data ?? []).reduce(
    (acc, t) => acc + (t.member_count ?? 0),
    0,
  );
  const networkPrevalence = passTotal > 0 ? (theme.member_count ?? 0) / passTotal : 0;

  // Per-location distribution from location_theme_scores
  const scoresRes = await db
    .from("location_theme_scores")
    .select("location_id, prevalence, z_score, direction")
    .eq("theme_id", themeId);
  const locsRes = await db
    .from("locations")
    .select("id, internal_id, name");
  const locById = new Map(
    (locsRes.data ?? []).map((l) => [l.id, { internalId: l.internal_id, name: l.name }]),
  );

  const locations: ThemeLocationRow[] = (scoresRes.data ?? [])
    .map((s) => {
      const meta = locById.get(s.location_id);
      if (!meta) return null;
      return {
        locationId: s.location_id as string,
        internalId: meta.internalId,
        name: meta.name,
        prevalence: (s.prevalence as number) ?? 0,
        zScore: s.z_score as number | null,
        direction: s.direction as "above" | "below" | null,
      };
    })
    .filter((v): v is ThemeLocationRow => v != null)
    .sort((a, b) => b.prevalence - a.prevalence);

  // Network median (across all locations' prevalence values)
  const prevs = locations.map((l) => l.prevalence).sort((a, b) => a - b);
  const networkMedian =
    prevs.length === 0
      ? 0
      : prevs.length % 2 === 0
        ? (prevs[prevs.length / 2 - 1] + prevs[prevs.length / 2]) / 2
        : prevs[Math.floor(prevs.length / 2)];

  // Evidence quotes — pull source review metadata in one go
  const rawQuotes = Array.isArray(theme.evidence_quotes) ? theme.evidence_quotes : [];
  const quoteIds = rawQuotes
    .map((q: unknown) =>
      typeof q === "object" && q !== null && "source_review_id" in q
        ? (q as { source_review_id?: string }).source_review_id
        : null,
    )
    .filter((id): id is string => !!id);
  let quoteMeta = new Map<string, { source: string; posted_at: string; rating: number }>();
  if (quoteIds.length > 0) {
    const reviewsRes = await db
      .from("reviews")
      .select("id, source, posted_at, rating")
      .in("id", quoteIds);
    quoteMeta = new Map(
      (reviewsRes.data ?? []).map((r) => [
        r.id as string,
        {
          source: r.source as string,
          posted_at: r.posted_at as string,
          rating: r.rating as number,
        },
      ]),
    );
  }
  const quotes: ThemeQuote[] = rawQuotes
    .map((q: unknown) => {
      if (typeof q !== "object" || q === null) return null;
      const obj = q as { quote?: string; source_review_id?: string };
      if (!obj.quote || !obj.source_review_id) return null;
      const meta = quoteMeta.get(obj.source_review_id);
      return {
        quote: obj.quote,
        source: meta?.source ?? null,
        postedAt: meta?.posted_at ?? null,
        rating: meta?.rating ?? null,
      };
    })
    .filter((v): v is ThemeQuote => v != null);

  // Aggregate trend across locations
  const trendsRes = await db
    .from("trends")
    .select("direction, delta")
    .eq("theme_id", themeId)
    .eq("scope", "theme");
  const trends = trendsRes.data ?? [];
  const deltas = trends
    .map((t) => t.delta as number | null)
    .filter((v): v is number => v != null);
  const trendSummary = {
    improving: trends.filter((t) => t.direction === "improving").length,
    stable: trends.filter((t) => t.direction === "stable").length,
    degrading: trends.filter((t) => t.direction === "degrading").length,
    avgDelta:
      deltas.length > 0
        ? deltas.reduce((a, b) => a + b, 0) / deltas.length
        : null,
  };

  return {
    id: theme.id as string,
    label: (theme.label as string) ?? "",
    pass: theme.pass as Pass,
    memberCount: theme.member_count as number,
    networkPrevalence,
    networkMedian,
    locations,
    quotes,
    trendSummary,
  };
}
