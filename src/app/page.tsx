import Link from "next/link";
import { getHomeStats, getHomeThemes, type HomeTheme } from "@/lib/data/home";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { passLabel } from "@/lib/labels";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const [stats, themes] = await Promise.all([getHomeStats(), getHomeThemes()]);
  const actionThemes = themes.filter((t) => t.pass === "actions");
  const workingThemes = themes.filter((t) => t.pass === "working");

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-green-900">
          Network themes
        </h1>
        <p className="text-muted-foreground">
          What customers across the chain are talking about — sorted by
          prevalence. Click any theme to see who&apos;s an outlier and what&apos;s
          changing.
        </p>
      </header>

      <StatsBar stats={stats} />

      <ThemeSection
        title="Action items"
        subtitle={`${actionThemes.length} themes from negative reviews`}
        tone="bad"
        themes={actionThemes}
      />

      <hr className="border-green-900/15" />

      <ThemeSection
        title="What's working"
        subtitle={`${workingThemes.length} themes from positive reviews`}
        tone="good"
        themes={workingThemes}
      />
    </div>
  );
}

function ThemeSection({
  title,
  subtitle,
  tone,
  themes,
}: {
  title: string;
  subtitle: string;
  tone: "bad" | "good";
  themes: HomeTheme[];
}) {
  const headingClass =
    tone === "bad" ? "text-rose-800" : "text-green-800";
  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className={`text-xl font-semibold ${headingClass}`}>{title}</h2>
        <p className="text-xs text-muted-foreground">{subtitle}</p>
      </div>
      {themes.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No themes in this category.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {themes.map((t) => (
            <ThemeCard key={t.id} theme={t} />
          ))}
        </div>
      )}
    </section>
  );
}

function StatsBar({
  stats,
}: {
  stats: Awaited<ReturnType<typeof getHomeStats>>;
}) {
  return (
    <Card className="border-2 border-green-900/20 bg-gradient-to-br from-white to-green-50/40">
      <CardContent className="grid grid-cols-2 gap-6 p-5 md:grid-cols-6">
        <Stat label="Reviews analyzed" value={stats.totalReviews.toLocaleString()} />
        <Stat label="Locations" value={stats.totalLocations.toString()} />
        <Stat
          label="Network avg rating"
          value={
            stats.networkAvgRating != null
              ? `${stats.networkAvgRating.toFixed(2)}★`
              : "—"
          }
        />
        <Stat
          label="vs prior 90 days"
          value={
            stats.ratingDelta != null
              ? `${stats.ratingDelta >= 0 ? "+" : ""}${stats.ratingDelta.toFixed(2)}★`
              : "—"
          }
          tone={
            stats.ratingDelta == null
              ? "neutral"
              : Math.abs(stats.ratingDelta) < 0.005
                ? "neutral"
                : stats.ratingDelta > 0
                  ? "positive"
                  : "negative"
          }
        />
        <Stat
          label="Direction"
          value={`${stats.improving}↑ ${stats.stable}→ ${stats.degrading}↓`}
        />
        <Stat
          label="Briefs current as of"
          value={
            stats.briefGeneratedAt
              ? new Date(stats.briefGeneratedAt).toLocaleDateString()
              : "—"
          }
        />
      </CardContent>
    </Card>
  );
}

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "positive" | "negative" | "neutral";
}) {
  const toneClass =
    tone === "positive"
      ? "text-green-800"
      : tone === "negative"
        ? "text-rose-700"
        : "text-green-900";
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className={`text-xl font-bold ${toneClass}`}>{value}</span>
    </div>
  );
}

function ThemeCard({ theme }: { theme: HomeTheme }) {
  const isAction = theme.pass === "actions";
  return (
    <Link href={`/themes/${theme.id}`} className="block">
      <Card className="h-full border-2 transition hover:border-sky-600/40 hover:shadow-md">
        <CardContent className="space-y-3 p-5">
          <div className="flex items-start justify-between gap-3">
            <h3 className="text-sm font-medium leading-snug">{theme.label}</h3>
            <Badge
              variant="outline"
              className={`whitespace-nowrap ${
                isAction
                  ? "border-rose-200 bg-rose-50 text-rose-700"
                  : "border-green-300 bg-green-50 text-green-800"
              }`}
            >
              {passLabel.singular(theme.pass)}
            </Badge>
          </div>

          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            <span>
              <strong className="text-foreground">
                {(theme.prevalence * 100).toFixed(1)}%
              </strong>{" "}
              of {isAction ? "negative" : "positive"} reviews
            </span>
            <span>{theme.memberCount.toLocaleString()} reviews</span>
            {theme.recentDirection && (
              <DirectionPill direction={theme.recentDirection} />
            )}
          </div>

          {theme.topLocations.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs uppercase tracking-wider text-muted-foreground">
                Top above network median
              </p>
              <ul className="space-y-0.5 text-xs text-muted-foreground">
                {theme.topLocations.map((loc) => (
                  <li key={loc.internalId} className="flex justify-between gap-3">
                    <span className="truncate">{loc.name}</span>
                    <span className="font-mono">z=+{loc.zScore.toFixed(2)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </CardContent>
      </Card>
    </Link>
  );
}

function DirectionPill({
  direction,
}: {
  direction: "improving" | "degrading" | "stable";
}) {
  // direction_for_theme in compute_trends already encodes "good vs bad for
  // the manager" — so we color by the word, not the pass.
  const tone =
    direction === "improving"
      ? "text-green-800"
      : direction === "degrading"
        ? "text-rose-700"
        : "text-muted-foreground";
  const arrow =
    direction === "improving" ? "↑" : direction === "degrading" ? "↓" : "→";
  return (
    <span className={`font-medium ${tone}`}>
      {arrow} {direction}
    </span>
  );
}
