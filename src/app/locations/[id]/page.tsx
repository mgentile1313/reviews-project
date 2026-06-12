import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  getLocationDetail,
  type LocationAnomaly,
} from "@/lib/data/location";
import { passLabel } from "@/lib/labels";

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const data = await getLocationDetail(id);
  return { title: data?.name ?? "Location" };
}

/**
 * Rewrite Opus's section headings to match the dashboard's "action items" /
 * "what's working" wording. Database stores pass='actions'|'working', briefs
 * are markdown frozen at generation time — so we patch the rendered text.
 */
function applyLabelRewrites(md: string): string {
  return md
    .replace(/^(#{1,6})\s*Top (\d+) actions\b/gim, "$1 Top $2 action items")
    .replace(/^(#{1,6})\s*Actions?\s*$/gim, "$1 Action items");
}

export default async function LocationDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const data = await getLocationDetail(id);
  if (!data) notFound();

  // Anomalies on the "bad" side: action items above network median
  const actionAbove = data.anomalies.filter(
    (a) => a.pass === "actions" && a.direction === "above",
  );
  // Anomalies on the "good" side: what's-working themes above network median
  const workingAbove = data.anomalies.filter(
    (a) => a.pass === "working" && a.direction === "above",
  );

  return (
    <div className="space-y-8">
      <nav className="text-xs text-muted-foreground">
        <Link href="/" className="hover:text-sky-700 transition-colors">
          ← All themes
        </Link>
      </nav>

      {/* Header */}
      <header className="space-y-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-2xl font-bold tracking-tight text-green-900">
            {data.name}
          </h1>
          <span className="font-mono text-xs text-muted-foreground">
            {data.internalId}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-6 text-sm text-muted-foreground">
          <Stat
            label="Current rating"
            value={
              data.currentRating != null
                ? `${data.currentRating.toFixed(2)}★`
                : "—"
            }
          />
          <Stat
            label="Reviews"
            value={data.totalReviews.toLocaleString()}
          />
          {data.trendOverall && (
            <>
              <Stat
                label="vs prior 90 days"
                value={
                  data.trendOverall.ratingDelta != null
                    ? `${
                        data.trendOverall.ratingDelta >= 0 ? "+" : ""
                      }${data.trendOverall.ratingDelta.toFixed(2)}★`
                    : "—"
                }
                tone={
                  data.trendOverall.ratingDelta == null
                    ? "neutral"
                    : Math.abs(data.trendOverall.ratingDelta) < 0.005
                      ? "neutral"
                      : data.trendOverall.ratingDelta > 0
                        ? "positive"
                        : "negative"
                }
              />
              <Stat
                label="Volume (90d)"
                value={`${data.trendOverall.recentN ?? 0} vs ${
                  data.trendOverall.priorN ?? 0
                }`}
              />
            </>
          )}
          {data.brief && (
            <Stat
              label="Brief generated"
              value={new Date(data.brief.generatedAt).toLocaleDateString()}
            />
          )}
        </div>
      </header>

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-[1fr_320px]">
        {/* Brief content */}
        <section>
          <Card className="border-2 border-green-900/20">
            <CardContent className="p-6">
              {data.brief ? (
                <article className="brief-prose">
                  <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>
                    {applyLabelRewrites(data.brief.content)}
                  </ReactMarkdown>
                </article>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No active brief for this location yet.
                </p>
              )}
            </CardContent>
          </Card>
        </section>

        {/* Sidebar — anomalies */}
        <aside className="space-y-6">
          <AnomalyList
            title="Action items above network median"
            subtitle="Where this location is worst-in-network"
            rows={actionAbove}
            tone="bad"
          />
          <AnomalyList
            title="Strengths above network median"
            subtitle="Where this location outperforms peers"
            rows={workingAbove}
            tone="good"
          />
        </aside>
      </div>
    </div>
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
    <div className="flex items-center gap-3">
      <span className="text-xs uppercase tracking-wider">{label}</span>
      <span className={`text-base font-semibold ${toneClass}`}>{value}</span>
    </div>
  );
}

function AnomalyList({
  title,
  subtitle,
  rows,
  tone,
}: {
  title: string;
  subtitle: string;
  rows: LocationAnomaly[];
  tone: "bad" | "good";
}) {
  const isBad = tone === "bad";
  return (
    <Card
      className={`border-2 ${
        isBad ? "border-rose-200/60" : "border-green-300/60"
      }`}
    >
      <CardContent className="space-y-3 p-5">
        <header className="space-y-1">
          <h3
            className={`text-sm font-semibold ${
              isBad ? "text-rose-800" : "text-green-800"
            }`}
          >
            {title}
          </h3>
          <p className="text-xs text-muted-foreground">{subtitle}</p>
        </header>
        {rows.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            None — this location is at or below network median on every theme in
            this category.
          </p>
        ) : (
          <ul className="space-y-2">
            {rows.slice(0, 8).map((a) => (
              <li key={a.themeId}>
                <Link
                  href={`/themes/${a.themeId}`}
                  className="block rounded p-2 transition hover:bg-sky-50"
                >
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="line-clamp-2 leading-snug text-foreground">
                      {a.label}
                    </span>
                    <span
                      className={`shrink-0 font-mono ${
                        isBad ? "text-rose-700" : "text-green-700"
                      }`}
                    >
                      +{a.zScore.toFixed(2)}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                    <Badge
                      variant="outline"
                      className={`whitespace-nowrap px-1.5 py-0 text-[10px] ${
                        a.pass === "actions"
                          ? "border-rose-200 bg-rose-50 text-rose-700"
                          : "border-green-300 bg-green-50 text-green-800"
                      }`}
                    >
                      {passLabel.singular(a.pass)}
                    </Badge>
                    <span>{(a.prevalence * 100).toFixed(1)}% of reviews</span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
