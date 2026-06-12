/**
 * Server-side data fetcher for the location detail page.
 *
 * Returns the location's profile, latest active brief markdown, overall trend
 * window (rating + volume), and the location's anomaly themes ranked by |z|.
 */

import { createServiceClient } from "@/lib/supabase";
import type { Pass } from "@/lib/labels";

export type LocationAnomaly = {
  themeId: string;
  label: string;
  pass: Pass;
  prevalence: number;
  zScore: number;
  direction: "above" | "below";
};

export type LocationDetail = {
  id: string;
  internalId: string;
  name: string;
  city: string | null;
  state: string | null;
  currentRating: number | null;
  totalReviews: number;
  brief: {
    id: string;
    content: string;
    model: string;
    generatedAt: string;
    intelligenceAsOf: string | null;
  } | null;
  trendOverall: {
    recentRatingMean: number | null;
    priorRatingMean: number | null;
    ratingDelta: number | null;
    recentN: number | null;
    priorN: number | null;
    direction: "improving" | "degrading" | "stable" | null;
  } | null;
  anomalies: LocationAnomaly[];          // sorted by |z| desc
};

export async function getLocationDetail(
  locationId: string,
): Promise<LocationDetail | null> {
  const db = createServiceClient();

  const locRes = await db
    .from("locations")
    .select("id, internal_id, name, city, state")
    .eq("id", locationId)
    .single();
  if (locRes.error || !locRes.data) return null;
  const loc = locRes.data;

  // Review count + average — single aggregate query
  const reviewsRes = await db
    .from("reviews")
    .select("rating", { count: "exact" })
    .eq("location_id", locationId);
  const totalReviews = reviewsRes.count ?? 0;
  const ratings = (reviewsRes.data ?? [])
    .map((r) => r.rating as number | null)
    .filter((v): v is number => v != null);
  const currentRating =
    ratings.length > 0
      ? ratings.reduce((a, b) => a + b, 0) / ratings.length
      : null;

  // Latest active brief
  const briefRes = await db
    .from("briefs")
    .select("id, content, model, generated_at, intelligence_as_of")
    .eq("location_id", locationId)
    .eq("status", "active")
    .order("generated_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  const brief = briefRes.data
    ? {
        id: briefRes.data.id as string,
        content: briefRes.data.content as string,
        model: briefRes.data.model as string,
        generatedAt: briefRes.data.generated_at as string,
        intelligenceAsOf: briefRes.data.intelligence_as_of as string | null,
      }
    : null;

  // Overall trend (rating + volume window). Columns are recent_value/prior_value/delta;
  // scope='overall' rows hold mean rating in those numeric fields.
  const trendRes = await db
    .from("trends")
    .select("recent_value, prior_value, delta, recent_n, prior_n, direction")
    .eq("location_id", locationId)
    .eq("scope", "overall")
    .maybeSingle();
  const trendOverall = trendRes.data
    ? {
        recentRatingMean: trendRes.data.recent_value as number | null,
        priorRatingMean: trendRes.data.prior_value as number | null,
        ratingDelta: trendRes.data.delta as number | null,
        recentN: trendRes.data.recent_n as number | null,
        priorN: trendRes.data.prior_n as number | null,
        direction: trendRes.data.direction as
          | "improving"
          | "degrading"
          | "stable"
          | null,
      }
    : null;

  // Anomalies — themes where this location's z_score is non-null and direction set
  const scoresRes = await db
    .from("location_theme_scores")
    .select("theme_id, prevalence, z_score, direction")
    .eq("location_id", locationId)
    .not("direction", "is", null)
    .not("z_score", "is", null);
  const themeIds = (scoresRes.data ?? []).map((s) => s.theme_id as string);
  let themeMeta = new Map<string, { label: string; pass: Pass }>();
  if (themeIds.length > 0) {
    // Only surface specific network themes — catch-all clusters (specific=false,
    // label NULL) would otherwise show as blank rows in the anomaly side panel.
    const themesRes = await db
      .from("themes")
      .select("id, label, pass")
      .in("id", themeIds)
      .eq("specific", true);
    themeMeta = new Map(
      (themesRes.data ?? []).map((t) => [
        t.id as string,
        { label: t.label as string, pass: t.pass as Pass },
      ]),
    );
  }
  const anomalies: LocationAnomaly[] = (scoresRes.data ?? [])
    .map((s) => {
      const meta = themeMeta.get(s.theme_id as string);
      if (!meta) return null;
      return {
        themeId: s.theme_id as string,
        label: meta.label,
        pass: meta.pass,
        prevalence: (s.prevalence as number) ?? 0,
        zScore: s.z_score as number,
        direction: s.direction as "above" | "below",
      };
    })
    .filter((v): v is LocationAnomaly => v != null)
    .sort((a, b) => Math.abs(b.zScore) - Math.abs(a.zScore));

  return {
    id: loc.id as string,
    internalId: loc.internal_id as string,
    name: loc.name as string,
    city: loc.city as string | null,
    state: loc.state as string | null,
    currentRating,
    totalReviews,
    brief,
    trendOverall,
    anomalies,
  };
}
