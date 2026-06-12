/**
 * Heatmap data fetcher — returns 34 locations × 10 specific network themes
 * with prevalence + z_score for each cell.
 */

import { createServiceClient } from "@/lib/supabase";
import type { Pass } from "@/lib/labels";

export type HeatmapLocation = {
  id: string;
  internalId: string;
  name: string;
};

export type HeatmapTheme = {
  id: string;
  label: string;
  pass: Pass;
};

export type HeatmapCell = {
  prevalence: number;
  zScore: number | null;
  direction: "above" | "below" | null;
};

export type HeatmapData = {
  locations: HeatmapLocation[];
  themes: HeatmapTheme[];
  // cells[locationId][themeId] -> cell
  cells: Record<string, Record<string, HeatmapCell>>;
};

export async function getHeatmapData(): Promise<HeatmapData> {
  const db = createServiceClient();

  const [locsRes, themesRes, scoresRes] = await Promise.all([
    db.from("locations").select("id, internal_id, name").order("name"),
    db
      .from("themes")
      .select("id, label, pass, member_count")
      .eq("scope", "network")
      .eq("specific", true),
    db
      .from("location_theme_scores")
      .select("location_id, theme_id, prevalence, z_score, direction"),
  ]);

  const locations: HeatmapLocation[] = (locsRes.data ?? []).map((l) => ({
    id: l.id as string,
    internalId: l.internal_id as string,
    name: l.name as string,
  }));

  // Sort themes: action items first (by member_count desc), then what's working (by member_count desc).
  // Keeps the heatmap split visually mirroring the home page.
  const themesSorted = (themesRes.data ?? []).sort((a, b) => {
    if (a.pass !== b.pass) return a.pass === "actions" ? -1 : 1;
    return (b.member_count ?? 0) - (a.member_count ?? 0);
  });
  const themes: HeatmapTheme[] = themesSorted.map((t) => ({
    id: t.id as string,
    label: t.label as string,
    pass: t.pass as Pass,
  }));

  const cells: Record<string, Record<string, HeatmapCell>> = {};
  for (const s of scoresRes.data ?? []) {
    const locId = s.location_id as string;
    const themeId = s.theme_id as string;
    if (!cells[locId]) cells[locId] = {};
    cells[locId][themeId] = {
      prevalence: (s.prevalence as number) ?? 0,
      zScore: s.z_score as number | null,
      direction: s.direction as "above" | "below" | null,
    };
  }

  return { locations, themes, cells };
}
