import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { getThemeDetail, type ThemeLocationRow } from "@/lib/data/theme";
import { passLabel } from "@/lib/labels";

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const data = await getThemeDetail(id);
  return { title: data?.label ?? "Theme" };
}

export default async function ThemeDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const data = await getThemeDetail(id);
  if (!data) notFound();

  const isAction = data.pass === "actions";

  const above = data.locations
    .filter((l) => l.direction === "above" && l.zScore != null)
    .sort((a, b) => (b.zScore ?? 0) - (a.zScore ?? 0))
    .slice(0, 5);
  const below = data.locations
    .filter((l) => l.direction === "below" && l.zScore != null)
    .sort((a, b) => (a.zScore ?? 0) - (b.zScore ?? 0))
    .slice(0, 5);

  const maxPrev = data.locations[0]?.prevalence ?? 0;

  return (
    <div className="space-y-8">
      <nav className="text-xs text-muted-foreground">
        <Link href="/" className="hover:text-sky-700 transition-colors">
          ← All themes
        </Link>
      </nav>

      {/* Header */}
      <header className="space-y-3">
        <div className="flex items-start justify-between gap-4">
          <h1 className="text-2xl font-bold tracking-tight text-green-900">
            {data.label}
          </h1>
          <Badge
            variant="outline"
            className={`whitespace-nowrap ${
              isAction
                ? "border-rose-200 bg-rose-50 text-rose-700"
                : "border-green-300 bg-green-50 text-green-800"
            }`}
          >
            {passLabel.singular(data.pass)}
          </Badge>
        </div>
        <div className="flex flex-wrap items-center gap-6 text-sm text-muted-foreground">
          <Stat
            label="Network prevalence"
            value={`${(data.networkPrevalence * 100).toFixed(1)}%`}
          />
          <Stat
            label="Reviews in cluster"
            value={data.memberCount.toLocaleString()}
          />
          <Stat
            label="Network median"
            value={`${(data.networkMedian * 100).toFixed(1)}%`}
          />
          <div className="flex items-center gap-3">
            <span className="text-xs uppercase tracking-wider">Direction</span>
            <span className="text-base font-semibold text-green-900">
              <span className="text-green-800">
                {data.trendSummary.improving}↑
              </span>{" "}
              <span className="text-muted-foreground">
                {data.trendSummary.stable}→
              </span>{" "}
              <span className="text-rose-700">
                {data.trendSummary.degrading}↓
              </span>
            </span>
          </div>
        </div>
      </header>

      {/* Distribution chart */}
      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-green-900">
          Per-location distribution
        </h2>
        <Card className="border-2 border-green-900/20">
          <CardContent className="p-5">
            <div className="space-y-1">
              {data.locations.map((loc) => (
                <DistributionRow
                  key={loc.locationId}
                  loc={loc}
                  maxPrev={maxPrev}
                  isAction={isAction}
                />
              ))}
            </div>
          </CardContent>
        </Card>
      </section>

      {/* Leaderboards */}
      <section className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <LeaderboardCard
          title={isAction ? "Worst on this theme" : "Strongest on this theme"}
          subtitle="Above network median, sorted by z-score"
          rows={above}
          isAction={isAction}
        />
        <LeaderboardCard
          title={isAction ? "Best on this theme" : "Weakest on this theme"}
          subtitle="Below network median, sorted by z-score"
          rows={below}
          isAction={isAction}
          flip
        />
      </section>

      {/* Evidence quotes */}
      {data.quotes.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-lg font-semibold text-green-900">
            Evidence — verbatim from customer reviews
          </h2>
          <div className="space-y-3">
            {data.quotes.map((q, idx) => (
              <Card key={idx} className="border-2 border-green-900/15">
                <CardContent className="space-y-2 p-5">
                  <p className="text-sm italic leading-relaxed text-foreground">
                    &ldquo;{q.quote}&rdquo;
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {q.source ?? "review"}
                    {q.postedAt ? `, ${q.postedAt.slice(0, 10)}` : ""}
                    {q.rating != null ? ` · ${q.rating}★` : ""}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-xs uppercase tracking-wider">{label}</span>
      <span className="text-base font-semibold text-green-900">{value}</span>
    </div>
  );
}

function DistributionRow({
  loc,
  maxPrev,
  isAction,
}: {
  loc: ThemeLocationRow;
  maxPrev: number;
  isAction: boolean;
}) {
  const widthPct = maxPrev > 0 ? (loc.prevalence / maxPrev) * 100 : 0;
  // Color intent: for action themes, high prevalence is bad → rose; for working, good → green.
  // Guarded (z=NULL): neutral gray.
  const barColor =
    loc.zScore == null
      ? "bg-slate-200"
      : isAction
        ? loc.direction === "above"
          ? "bg-rose-400"
          : "bg-rose-200"
        : loc.direction === "above"
          ? "bg-green-600"
          : "bg-green-300";
  return (
    <Link
      href={`/locations/${loc.locationId}`}
      className="grid grid-cols-[180px_1fr_70px_60px] items-center gap-3 rounded px-2 py-1.5 transition hover:bg-sky-50"
    >
      <span className="truncate text-xs text-foreground">{loc.name}</span>
      <div className="relative h-3 overflow-hidden rounded bg-muted/50">
        <div
          className={`absolute inset-y-0 left-0 ${barColor}`}
          style={{ width: `${widthPct}%` }}
        />
      </div>
      <span className="text-right text-xs font-mono text-foreground">
        {(loc.prevalence * 100).toFixed(1)}%
      </span>
      <span
        className={`text-right text-xs font-mono ${
          loc.zScore == null
            ? "text-muted-foreground"
            : (loc.zScore ?? 0) > 0
              ? isAction
                ? "text-rose-700"
                : "text-green-700"
              : isAction
                ? "text-green-700"
                : "text-rose-700"
        }`}
      >
        {loc.zScore != null
          ? `${loc.zScore >= 0 ? "+" : ""}${loc.zScore.toFixed(2)}`
          : "—"}
      </span>
    </Link>
  );
}

function LeaderboardCard({
  title,
  subtitle,
  rows,
  isAction,
  flip = false,
}: {
  title: string;
  subtitle: string;
  rows: ThemeLocationRow[];
  isAction: boolean;
  flip?: boolean;
}) {
  // "good" leaderboard = green chrome; "bad" = rose chrome
  const isBadList = isAction ? !flip : flip;
  return (
    <Card
      className={`border-2 ${
        isBadList ? "border-rose-200/60" : "border-green-300/60"
      }`}
    >
      <CardContent className="space-y-3 p-5">
        <header className="space-y-1">
          <h3
            className={`text-sm font-semibold ${
              isBadList ? "text-rose-800" : "text-green-800"
            }`}
          >
            {title}
          </h3>
          <p className="text-xs text-muted-foreground">{subtitle}</p>
        </header>
        {rows.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No locations on this side.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {rows.map((loc) => (
              <li key={loc.locationId}>
                <Link
                  href={`/locations/${loc.locationId}`}
                  className="grid grid-cols-[1fr_60px_60px] items-baseline gap-2 rounded px-2 py-1 text-xs transition hover:bg-sky-50"
                >
                  <span className="truncate">{loc.name}</span>
                  <span className="text-right font-mono text-foreground">
                    {(loc.prevalence * 100).toFixed(1)}%
                  </span>
                  <span
                    className={`text-right font-mono ${
                      isBadList ? "text-rose-700" : "text-green-700"
                    }`}
                  >
                    {loc.zScore != null
                      ? `${loc.zScore >= 0 ? "+" : ""}${loc.zScore.toFixed(2)}`
                      : "—"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
