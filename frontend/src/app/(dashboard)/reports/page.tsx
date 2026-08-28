"use client";

import { useEffect, useState } from "react";

import {
  Card,
  CardHeader,
  Notice,
  Skeleton,
  StatTile,
  fmt,
  fmtEnergy,
} from "@/components/ui";
import { fetchDashboardSummary } from "@/lib/api";
import { num, type DashboardSummary } from "@/types/store";

/**
 * Reports & ESG.
 *
 * The carbon figures here are real arithmetic on the TGO factor and can be
 * relied on once telemetry is real. The financial figures deliberately are NOT
 * shown as numbers: the tariff table holds on-peak rates only, so any savings
 * figure computed today would be wrong by however much off-peak consumption
 * there is — and wrong quietly, which is the dangerous kind. The blocked state
 * says exactly what is missing instead.
 */
export default function ReportsPage() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchDashboardSummary()
      .then((s) => !cancelled && setSummary(s))
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Backend unreachable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const co2Today = num(summary?.esg.co2_avoided_today_kg ?? null);
  const energyToday = num(summary?.live.total_daily_yield_kwh ?? null);
  const capacity = num(summary?.fleet.total_installed_kwp ?? null);

  // Annualised from today alone, and labelled as such. A single day extrapolated
  // to a year is a rough indication, not a forecast, and presenting it as one
  // would be misleading.
  const co2AnnualTonnes = co2Today === null ? null : (co2Today * 365) / 1000;

  return (
    <main className="mx-auto max-w-[1600px] animate-fade-in px-4 py-5">
      <div className="mb-4">
        <h1 className="text-lg font-semibold tracking-tight">Reports &amp; ESG</h1>
        <p className="mt-0.5 text-xs text-content-muted">
          Carbon avoidance on the Thailand TGO standard, and the financial figures still
          waiting on tariff data
        </p>
      </div>

      {error && (
        <div className="mb-4">
          <Notice tone="warn">Backend unreachable ({error}).</Notice>
        </div>
      )}

      {summary === null && !error ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[92px]" />
          ))}
        </div>
      ) : (
        <>
          {/* --- ESG ---------------------------------------------------- */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="Energy today"
              value={fmtEnergy(energyToday).value}
              unit={fmtEnergy(energyToday).unit}
              coverage={`${summary?.live.stores_online ?? 0} branches reporting`}
            />
            <StatTile
              label="CO₂ avoided today"
              value={fmt(co2Today, 1)}
              unit="kg"
              tone="ok"
              coverage="measured generation × TGO factor"
            />
            <StatTile
              label="Annualised CO₂"
              value={fmt(co2AnnualTonnes, 1)}
              unit="tCO₂e"
              tone="ok"
              coverage="today × 365 — indicative only"
              hint="A straight extrapolation from a single day. Replace with a 12-month rolling figure once history exists."
            />
            <StatTile
              label="Installed capacity"
              value={fmt(capacity, 0)}
              unit="kWp"
              coverage={`${summary?.fleet.total_stores ?? 0} branches`}
            />
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <Card>
              <CardHeader
                title="Carbon accounting basis"
                subtitle="Every figure above is reproducible from these inputs"
              />
              <dl className="space-y-2 text-xs">
                <Row label="Standard" value={summary?.esg.standard ?? "—"} />
                <Row
                  label="Grid emission factor"
                  value={`${summary?.esg.emission_factor ?? "—"} kgCO₂e/kWh`}
                />
                <Row
                  label="Factor year"
                  value={String(summary?.esg.emission_factor_year ?? "—")}
                />
                <Row
                  label="Energy counted"
                  value={`${fmt(energyToday, 1)} kWh`}
                />
              </dl>
              <p className="mt-3 border-t border-line pt-3 text-2xs text-content-muted">
                TGO revises the emission factor periodically. The year is recorded
                alongside the value so a report re-run for a previous year reproduces that
                year&apos;s numbers rather than today&apos;s.
              </p>
            </Card>

            {/* --- Financial: blocked, and honest about why -------------- */}
            <Card>
              <CardHeader
                title="Financial savings"
                subtitle="Not available yet — and deliberately not estimated"
              />
              <Notice tone="warn">
                The tariff table holds <strong>on-peak rates only</strong>. Off-peak rates
                and demand charges are blank, so a savings figure calculated now would be
                wrong by however much consumption falls outside peak hours — and wrong
                silently, which is worse than absent.
              </Notice>

              <div className="mt-3 space-y-2 text-xs">
                <Row label="On-peak rates" value="loaded from the workbook" tone="ok" />
                <Row label="Off-peak rates" value="missing" tone="warn" />
                <Row label="Demand charges" value="missing" tone="warn" />
              </div>

              <p className="mt-3 border-t border-line pt-3 text-2xs text-content-muted">
                To unblock: enter the official PEA and MEA published rates into the
                <code className="mx-1 rounded bg-surface-3 px-1 py-0.5">tariffs</code>
                table. TOU savings, demand-charge optimisation and payback per branch all
                become available at that point.
              </p>
            </Card>
          </div>

          {/* --- Coverage caveat --------------------------------------- */}
          {(summary?.fleet.stores_without_location ?? 0) > 0 && (
            <div className="mt-3">
              <Notice>
                {summary?.fleet.stores_without_location} of{" "}
                {summary?.fleet.total_stores} branches have no coordinates recorded. They
                are included in every figure on this page but do not appear on the map.
              </Notice>
            </div>
          )}
        </>
      )}
    </main>
  );
}

function Row({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn";
}) {
  const tones = { ok: "text-status-ok", warn: "text-status-warn" };
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-content-muted">{label}</dt>
      <dd className={`text-right ${tone ? tones[tone] : "text-content"}`}>{value}</dd>
    </div>
  );
}
