import type { Metadata } from "next";
import Link from "next/link";
import { getHeatmapData, type HeatmapCell } from "@/lib/data/heatmap";
import type { Pass } from "@/lib/labels";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Heatmap" };

export default async function HeatmapPage() {
  const data = await getHeatmapData();

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-green-900">
          Heatmap
        </h1>
        <p className="text-muted-foreground">
          Every location against every specific network theme. Cell color =
          z-score relative to network median (darker = stronger outlier).
          For action items, red means worse than peers; for what&apos;s working,
          green means stronger than peers.
        </p>
      </header>

      <Legend />

      <div className="overflow-auto rounded-lg border-2 border-green-900/20 bg-white">
        <table className="border-separate border-spacing-0 text-xs">
          <thead>
            <tr>
              <th
                className="sticky left-0 top-0 z-30 min-w-[220px] border-b border-r border-muted bg-white px-3 py-2 text-left font-semibold text-green-900"
              >
                Location
              </th>
              {data.themes.map((t) => (
                <th
                  key={t.id}
                  title={t.label}
                  className={`sticky top-0 z-20 h-32 min-w-[60px] max-w-[60px] border-b border-r border-muted bg-white align-bottom ${
                    t.pass === "actions" ? "text-rose-800" : "text-green-800"
                  }`}
                >
                  <Link
                    href={`/themes/${t.id}`}
                    className="block h-full hover:underline"
                  >
                    <div
                      className="flex h-full items-end justify-center pb-2"
                    >
                      <span
                        className="line-clamp-5 inline-block max-h-28 max-w-[110px] whitespace-normal text-left text-[10px] font-medium leading-tight"
                        style={{
                          writingMode: "vertical-rl",
                          transform: "rotate(180deg)",
                        }}
                      >
                        {t.label}
                      </span>
                    </div>
                  </Link>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.locations.map((loc) => (
              <tr key={loc.id}>
                <th
                  scope="row"
                  title={loc.name}
                  className="sticky left-0 z-10 border-b border-r border-muted bg-white px-3 py-1.5 text-left font-medium"
                >
                  <Link
                    href={`/locations/${loc.id}`}
                    className="block max-w-[260px] truncate text-foreground hover:text-sky-700"
                  >
                    {loc.name}
                  </Link>
                </th>
                {data.themes.map((t) => {
                  const cell = data.cells[loc.id]?.[t.id];
                  return (
                    <Cell key={t.id} cell={cell} pass={t.pass} location={loc.name} theme={t.label} />
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Cell({
  cell,
  pass,
  location,
  theme,
}: {
  cell: HeatmapCell | undefined;
  pass: Pass;
  location: string;
  theme: string;
}) {
  if (!cell) {
    return <td className="h-7 border-b border-r border-muted bg-slate-50" />;
  }
  const z = cell.zScore;
  const bg = cellColor(pass, z);
  const tooltip =
    `${location} · ${theme}\n` +
    `Prevalence: ${(cell.prevalence * 100).toFixed(1)}%\n` +
    `z-score: ${z != null ? z.toFixed(2) : "n/a (below sample-size guard)"}`;
  return (
    <td
      title={tooltip}
      className={`h-7 border-b border-r border-muted text-center align-middle ${bg}`}
    >
      <span
        className={`text-[10px] font-mono ${
          z == null ? "text-muted-foreground" : "text-foreground/70"
        }`}
      >
        {z != null ? z.toFixed(1) : "—"}
      </span>
    </td>
  );
}

/**
 * Discrete color bins by z-score. Polarity inverts by pass:
 *   - action item: positive z is bad (rose), negative z is good (green hint)
 *   - what's working: positive z is good (green), negative z is bad (rose hint)
 * Classes are written explicitly so Tailwind's JIT picks them all up.
 */
function cellColor(pass: Pass, z: number | null): string {
  if (z == null) return "bg-slate-100";
  if (pass === "actions") {
    if (z >= 2) return "bg-rose-500/80";
    if (z >= 1) return "bg-rose-400/70";
    if (z >= 0) return "bg-rose-200/60";
    if (z >= -1) return "bg-green-100/60";
    return "bg-green-200/70";
  }
  // working
  if (z >= 2) return "bg-green-500/70";
  if (z >= 1) return "bg-green-400/70";
  if (z >= 0) return "bg-green-200/60";
  if (z >= -1) return "bg-rose-100/60";
  return "bg-rose-200/70";
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-6 text-xs">
      <LegendRow
        title="Action items"
        bins={[
          { className: "bg-green-200/70", label: "z ≤ −1 (better than peers)" },
          { className: "bg-green-100/60", label: "−1 < z < 0" },
          { className: "bg-rose-200/60", label: "0 ≤ z < 1" },
          { className: "bg-rose-400/70", label: "1 ≤ z < 2" },
          { className: "bg-rose-500/80", label: "z ≥ 2 (worst outlier)" },
        ]}
      />
      <LegendRow
        title="What's working"
        bins={[
          { className: "bg-rose-200/70", label: "z ≤ −1 (weakest)" },
          { className: "bg-rose-100/60", label: "−1 < z < 0" },
          { className: "bg-green-200/60", label: "0 ≤ z < 1" },
          { className: "bg-green-400/70", label: "1 ≤ z < 2" },
          { className: "bg-green-500/70", label: "z ≥ 2 (strongest)" },
        ]}
      />
      <LegendRow
        title="Guarded"
        bins={[{ className: "bg-slate-100", label: "n below sample-size guard" }]}
      />
    </div>
  );
}

function LegendRow({
  title,
  bins,
}: {
  title: string;
  bins: { className: string; label: string }[];
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </span>
      <div className="flex items-center gap-1">
        {bins.map((b) => (
          <span
            key={b.label}
            title={b.label}
            className={`inline-block h-4 w-5 rounded-sm border border-muted ${b.className}`}
          />
        ))}
      </div>
    </div>
  );
}
