import type { Metadata } from "next";
import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import {
  getLocationsOverview,
  type LocationOverviewRow,
} from "@/lib/data/locations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Locations" };

export default async function LocationsOverviewPage() {
  const data = await getLocationsOverview();

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-green-900">
          Locations
        </h1>
        <p className="text-muted-foreground">
          Every Mavis store, ranked. Default sort is by current rating, best
          first. Click any location to see its brief, anomalies, and trend.
        </p>
      </header>

      <NetworkSummary network={data.network} />

      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-green-900">
          Leaderboard
        </h2>
        <div className="overflow-x-auto rounded-lg border-2 border-green-900/20 bg-white">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b-2 border-green-900/20 text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                <Th className="w-10 text-right">#</Th>
                <Th>Location</Th>
                <Th className="w-20 text-right">Current</Th>
                <Th className="w-20 text-right">90d avg</Th>
                <Th className="w-24 text-right">Δ vs prior</Th>
                <Th className="w-28 text-right">Volume (90d)</Th>
                <Th className="w-24 text-right">Total reviews</Th>
                <Th className="w-32">Direction</Th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row, idx) => (
                <LocationRow key={row.id} row={row} rank={idx + 1} />
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Th({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <th className={`px-3 py-2 font-semibold ${className}`}>{children}</th>
  );
}

function NetworkSummary({
  network,
}: {
  network: Awaited<ReturnType<typeof getLocationsOverview>>["network"];
}) {
  return (
    <div className="space-y-4">
      <Card className="border-2 border-green-900/20 bg-gradient-to-br from-white to-green-50/40">
        <CardContent className="grid grid-cols-2 gap-6 p-5 md:grid-cols-5">
          <Stat
            label="Locations"
            value={(network.improving + network.degrading + network.stable + 0).toString()}
          />
          <Stat label="Total reviews" value={network.totalReviews.toLocaleString()} />
          <Stat
            label="Network avg rating"
            value={
              network.avgRating != null
                ? `${network.avgRating.toFixed(2)}★`
                : "—"
            }
          />
          <Stat
            label="Improving / Stable / Degrading"
            value={`${network.improving} / ${network.stable} / ${network.degrading}`}
          />
          <Stat
            label="Locations with trend data"
            value={(network.improving + network.stable + network.degrading).toString()}
          />
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <Highlight
          label="Best rated"
          row={network.best}
          formatValue={(r) =>
            r.currentRating != null ? `${r.currentRating.toFixed(2)}★` : "—"
          }
          tone="good"
        />
        <Highlight
          label="Lowest rated"
          row={network.worst}
          formatValue={(r) =>
            r.currentRating != null ? `${r.currentRating.toFixed(2)}★` : "—"
          }
          tone="bad"
        />
        <Highlight
          label="Biggest improver (90d)"
          row={network.biggestImprover}
          formatValue={(r) =>
            r.ratingDelta != null
              ? `${r.ratingDelta >= 0 ? "+" : ""}${r.ratingDelta.toFixed(2)}★`
              : "—"
          }
          tone="good"
        />
        <Highlight
          label="Biggest decliner (90d)"
          row={network.biggestDecliner}
          formatValue={(r) =>
            r.ratingDelta != null
              ? `${r.ratingDelta >= 0 ? "+" : ""}${r.ratingDelta.toFixed(2)}★`
              : "—"
          }
          tone="bad"
        />
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className="text-xl font-bold text-green-900">{value}</span>
    </div>
  );
}

function Highlight({
  label,
  row,
  formatValue,
  tone,
}: {
  label: string;
  row: LocationOverviewRow | null;
  formatValue: (r: LocationOverviewRow) => string;
  tone: "good" | "bad";
}) {
  const borderClass =
    tone === "good" ? "border-green-300/60" : "border-rose-200/60";
  const valueClass = tone === "good" ? "text-green-800" : "text-rose-700";
  return (
    <Card className={`border-2 ${borderClass}`}>
      <CardContent className="space-y-1 p-4">
        <p className="text-[11px] uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
        {row ? (
          <Link
            href={`/locations/${row.id}`}
            className="block hover:text-sky-700"
          >
            <p className="line-clamp-2 text-sm font-semibold text-foreground">
              {row.name}
            </p>
            <p className={`text-lg font-bold ${valueClass}`}>
              {formatValue(row)}
            </p>
          </Link>
        ) : (
          <p className="text-sm text-muted-foreground">—</p>
        )}
      </CardContent>
    </Card>
  );
}

function LocationRow({
  row,
  rank,
}: {
  row: LocationOverviewRow;
  rank: number;
}) {
  return (
    <tr className="border-b border-muted last:border-b-0 hover:bg-sky-50">
      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
        {rank}
      </td>
      <td className="px-3 py-2">
        <Link
          href={`/locations/${row.id}`}
          className="font-medium text-foreground hover:text-sky-700"
        >
          {row.name}
        </Link>
        {row.city && (
          <span className="ml-2 text-muted-foreground">
            {row.city}
            {row.state ? `, ${row.state}` : ""}
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-right font-mono font-semibold text-green-900">
        {row.currentRating != null ? `${row.currentRating.toFixed(2)}★` : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono">
        {row.recentRatingMean != null
          ? `${row.recentRatingMean.toFixed(2)}★`
          : "—"}
      </td>
      <td
        className={`px-3 py-2 text-right font-mono ${
          row.ratingDelta == null || Math.abs(row.ratingDelta) < 0.005
            ? "text-muted-foreground"
            : row.ratingDelta > 0
              ? "text-green-700"
              : "text-rose-700"
        }`}
      >
        {row.ratingDelta != null
          ? `${row.ratingDelta >= 0 ? "+" : ""}${row.ratingDelta.toFixed(2)}★`
          : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
        {row.recentN ?? 0} vs {row.priorN ?? 0}
      </td>
      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
        {row.totalReviews.toLocaleString()}
      </td>
      <td className="px-3 py-2">
        <DirectionPill direction={row.direction} />
      </td>
    </tr>
  );
}

function DirectionPill({
  direction,
}: {
  direction: "improving" | "degrading" | "stable" | null;
}) {
  if (!direction) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  const tone =
    direction === "improving"
      ? "text-green-800"
      : direction === "degrading"
        ? "text-rose-700"
        : "text-muted-foreground";
  const arrow =
    direction === "improving" ? "↑" : direction === "degrading" ? "↓" : "→";
  return (
    <span className={`text-xs font-medium ${tone}`}>
      {arrow} {direction}
    </span>
  );
}
