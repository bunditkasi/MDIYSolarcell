"use client";

/**
 * Energy reports — daily, monthly, lifetime annual.
 *
 * Mirrors the three modes of Atmoce's Reports tab, with one addition that
 * matters: each row carries the number of devices and readings behind it. A day
 * built from 2 of 6 inverters, or from 4 samples instead of 96, is not
 * comparable with a complete one, and a table that hides that invites somebody
 * to explain a dip that is really just missing data.
 */

import { useMemo } from "react";

import { Card, CardHeader, EmptyState, Skeleton, fmt } from "@/components/ui";
import { num, type EnergyHistory } from "@/types/store";

export type Granularity = "day" | "month" | "year";

const MODES: { value: Granularity; label: string }[] = [
  { value: "day", label: "Daily" },
  { value: "month", label: "Monthly" },
  { value: "year", label: "Annual" },
];

function formatPeriod(iso: string, granularity: Granularity): string {
  const date = new Date(`${iso}T00:00:00`);
  if (granularity === "year") return String(date.getFullYear());
  if (granularity === "month")
    return date.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
  return date.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function toCsv(history: EnergyHistory): string {
  const rows = [
    ["period", "produced_kwh", "devices", "samples"],
    ...history.buckets.map((b) => [
      b.period,
      b.produced_kwh ?? "",
      String(b.device_count),
      String(b.sample_count),
    ]),
  ];
  return rows.map((row) => row.join(",")).join("\n");
}

export default function EnergyReportView({
  history,
  loading,
  granularity,
  onGranularityChange,
  storeCode,
}: {
  history: EnergyHistory | null;
  loading: boolean;
  granularity: Granularity;
  onGranularityChange: (g: Granularity) => void;
  storeCode: string;
}) {
  /** Peak bucket, used to scale the inline bars. */
  const peak = useMemo(() => {
    const values = (history?.buckets ?? [])
      .map((b) => num(b.produced_kwh) ?? 0)
      .filter((v) => v > 0);
    return values.length ? Math.max(...values) : 0;
  }, [history]);

  const download = () => {
    if (!history) return;
    const blob = new Blob([toCsv(history)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${storeCode}-energy-${granularity}-${history.start}-to-${history.end}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card padded={false}>
      <div className="p-4 pb-3">
        <CardHeader
          title="Energy reports"
          subtitle={
            history
              ? `${history.start} → ${history.end} · ${fmt(
                  num(history.total_produced_kwh),
                  1,
                )} kWh total`
              : undefined
          }
          action={
            <div className="flex items-center gap-2">
              <div className="flex rounded-lg border border-line bg-surface-2 p-0.5">
                {MODES.map((mode) => (
                  <button
                    key={mode.value}
                    type="button"
                    onClick={() => onGranularityChange(mode.value)}
                    aria-pressed={granularity === mode.value}
                    className={`rounded-md px-2.5 py-1 text-2xs transition ${
                      granularity === mode.value
                        ? "bg-accent text-accent-on"
                        : "text-content-muted hover:text-content"
                    }`}
                  >
                    {mode.label}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={download}
                disabled={!history || history.buckets.length === 0}
                className="rounded-lg border border-line bg-surface-2 px-2.5 py-1.5 text-2xs text-content-muted transition hover:border-line-strong hover:text-content disabled:cursor-not-allowed disabled:opacity-40"
              >
                Export CSV
              </button>
            </div>
          }
        />
      </div>

      {loading ? (
        <div className="space-y-px px-4 pb-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-8" />
          ))}
        </div>
      ) : !history || history.buckets.length === 0 ? (
        <EmptyState
          title="No energy recorded in this period"
          detail="Readings appear once vendor ingestion has run for this branch."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] text-xs">
            <thead>
              <tr className="border-b border-line text-2xs uppercase tracking-wide text-content-muted">
                <th className="px-4 py-2 text-left font-medium">Period</th>
                <th className="px-3 py-2 text-right font-medium">Produced (kWh)</th>
                <th className="px-3 py-2 text-left font-medium">&nbsp;</th>
                <th className="px-3 py-2 text-right font-medium">Devices</th>
                <th className="px-4 py-2 text-right font-medium">Samples</th>
              </tr>
            </thead>
            <tbody>
              {history.buckets.map((bucket) => {
                const produced = num(bucket.produced_kwh) ?? 0;
                const width = peak > 0 ? (produced / peak) * 100 : 0;
                return (
                  <tr
                    key={bucket.period}
                    className="border-b border-line/60 last:border-0 hover:bg-surface-3"
                  >
                    <td className="px-4 py-2 text-content">
                      {formatPeriod(bucket.period, history.granularity)}
                    </td>
                    <td className="num px-3 py-2 text-right font-medium text-content">
                      {fmt(num(bucket.produced_kwh), 2)}
                    </td>
                    <td className="w-40 px-3 py-2">
                      {/* Inline bar. A table of numbers hides the shape of a
                          month; the bar restores it without a chart library. */}
                      <span className="block h-1.5 overflow-hidden rounded-full bg-surface-3">
                        <span
                          className="block h-full rounded-full bg-accent"
                          style={{ width: `${width}%` }}
                        />
                      </span>
                    </td>
                    <td className="num px-3 py-2 text-right text-content-muted">
                      {bucket.device_count}
                    </td>
                    <td className="num px-4 py-2 text-right text-content-muted">
                      {bucket.sample_count}
                    </td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr className="border-t border-line-strong bg-surface-3">
                <td className="px-4 py-2 font-semibold text-content">Total</td>
                <td className="num px-3 py-2 text-right font-semibold text-content">
                  {fmt(num(history.total_produced_kwh), 2)}
                </td>
                <td colSpan={3} />
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </Card>
  );
}
