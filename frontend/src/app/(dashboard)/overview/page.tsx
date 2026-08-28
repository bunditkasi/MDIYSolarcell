"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  Notice,
  Skeleton,
  StatTile,
  StatusDot,
  fmt,
  fmtEnergy,
  relativeTime,
} from "@/components/ui";
import { fetchAlerts, fetchDashboardSummary, fetchMapPins } from "@/lib/api";
import { STATUS_DISPLAY_ORDER, STATUS_STYLES, type PRStatus } from "@/lib/pr-status";
import {
  num,
  specificYield,
  type AlertItem,
  type DashboardSummary,
  type MapResponse,
  type StorePin,
} from "@/types/store";

interface State {
  summary: DashboardSummary | null;
  fleet: MapResponse | null;
  alerts: AlertItem[];
  loading: boolean;
  error: string | null;
}

export default function OverviewPage() {
  const [state, setState] = useState<State>({
    summary: null,
    fleet: null,
    alerts: [],
    loading: true,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;

    void Promise.allSettled([
      fetchDashboardSummary(),
      fetchMapPins(),
      fetchAlerts({ limit: 8 }),
    ]).then(([summaryR, fleetR, alertsR]) => {
      if (cancelled) return;
      setState({
        summary: summaryR.status === "fulfilled" ? summaryR.value : null,
        fleet: fleetR.status === "fulfilled" ? fleetR.value : null,
        alerts: alertsR.status === "fulfilled" ? alertsR.value.items : [],
        loading: false,
        // Only the summary failing is worth showing as an error; the page is
        // still useful with a missing alert list.
        error:
          summaryR.status === "rejected"
            ? summaryR.reason instanceof Error
              ? summaryR.reason.message
              : "Backend unreachable"
            : null,
      });
    });

    return () => {
      cancelled = true;
    };
  }, []);

  const { summary, fleet, alerts, loading, error } = state;

  /** Fleet-wide specific yield, the headline that works without Solcast. */
  const fleetYield = useMemo(() => {
    const kwh = num(summary?.live.total_daily_yield_kwh ?? null);
    const kwp = num(summary?.fleet.total_installed_kwp ?? null);
    if (kwh === null || kwp === null || kwp <= 0) return null;
    return kwh / kwp;
  }, [summary]);

  /** Worst performers today, by specific yield — the "go look at these" list. */
  const worst = useMemo(() => {
    const stores = fleet?.stores ?? [];
    return stores
      .map((s) => ({ store: s, sy: specificYield(s.daily_yield_kwh, s.installed_kwp) }))
      .filter((x) => x.sy !== null && x.store.is_online)
      .sort((a, b) => (a.sy ?? 0) - (b.sy ?? 0))
      .slice(0, 6);
  }, [fleet]);

  const statusCounts = summary?.status_counts;
  const totalStores = summary?.fleet.total_stores ?? 0;

  if (loading) {
    return (
      <main className="mx-auto max-w-[1600px] px-4 py-5">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-[92px]" />
          ))}
        </div>
        <div className="mt-4 grid gap-3 lg:grid-cols-3">
          <Skeleton className="h-64 lg:col-span-2" />
          <Skeleton className="h-64" />
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1600px] animate-fade-in px-4 py-5">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Fleet overview</h1>
          <p className="mt-0.5 text-xs text-content-muted">
            {totalStores} branches · updated {new Date().toLocaleTimeString("en-GB")}
          </p>
        </div>
        <Link
          href="/map"
          className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-content-muted transition hover:border-line-strong hover:text-content"
        >
          Open map →
        </Link>
      </div>

      {error && (
        <div className="mb-4">
          <Notice tone="warn">
            Backend unreachable ({error}). Figures below may be missing or stale.
          </Notice>
        </div>
      )}

      {/* Status is being decided by the fallback measure, not by PR. Anyone
          reading a colour needs to know which question it answers: “behind its
          peers today” is not the same claim as “below its expected output for
          the light it received”. */}
      {(summary?.performance.stores_with_pr ?? 0) === 0 && (
        <div className="mb-4">
          <Notice>
            Branch status is ranked by <strong>specific yield</strong> — today’s
            kWh per kWp against the median of the whole fleet, so a branch is
            flagged only when it falls behind others under the same sky.{" "}
            <strong>Performance Ratio is not available</strong>: it needs a Solcast
            irradiance baseline and no API key is configured. Energy and power
            figures below are measured and correct.
          </Notice>
        </div>
      )}

      {/* --- KPI row ---------------------------------------------------- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <StatTile
          label="Installed capacity"
          value={fmt(num(summary?.fleet.total_installed_kwp ?? null), 0)}
          unit="kWp"
          coverage={`all ${totalStores} branches`}
        />
        <StatTile
          label="Live output"
          value={fmt(num(summary?.live.total_active_power_kw ?? null), 0)}
          unit="kW"
          tone="accent"
          coverage={`${summary?.live.stores_online ?? 0} reporting`}
        />
        <StatTile
          label="Energy today"
          value={fmtEnergy(num(summary?.live.total_daily_yield_kwh ?? null)).value}
          unit={fmtEnergy(num(summary?.live.total_daily_yield_kwh ?? null)).unit}
          coverage={`${summary?.live.stores_online ?? 0} reporting`}
        />
        <StatTile
          label="Specific yield"
          value={fmt(fleetYield, 2)}
          unit="kWh/kWp"
          hint="Energy today divided by installed capacity. Comparable between branches without needing irradiance data."
          coverage="fleet average today"
        />
        <StatTile
          label="CO₂ avoided today"
          value={fmt(num(summary?.esg.co2_avoided_today_kg ?? null), 0)}
          unit="kg"
          tone="ok"
          coverage={`TGO ${summary?.esg.emission_factor ?? "—"} kg/kWh`}
        />
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        {/* --- Status breakdown ----------------------------------------- */}
        <Card className="lg:col-span-1">
          <CardHeader
            title="Branch status"
            subtitle={`${totalStores} branches`}
            action={
              <Link href="/fleet" className="text-2xs text-accent-bright hover:underline">
                View all
              </Link>
            }
          />
          <ul className="space-y-1">
            {STATUS_DISPLAY_ORDER.map((status: PRStatus) => {
              const count = statusCounts?.[status] ?? 0;
              const pct = totalStores > 0 ? (count / totalStores) * 100 : 0;
              const style = STATUS_STYLES[status];
              return (
                <li key={status}>
                  <Link
                    href={`/fleet?status=${status}`}
                    className="flex items-center gap-3 rounded-lg px-2 py-2 transition hover:bg-surface-3"
                  >
                    <StatusDot status={status} />
                    <span className="w-20 shrink-0 text-xs text-content">{style.label}</span>
                    <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-3">
                      <span
                        className="block h-full rounded-full transition-all"
                        style={{ width: `${pct}%`, background: style.color }}
                      />
                    </span>
                    <span className="w-9 shrink-0 text-right text-xs tabular-nums text-content">
                      {count}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>

          <div className="mt-3 space-y-1.5 border-t border-line pt-3 text-2xs text-content-muted">
            <div className="flex justify-between">
              <span>Reporting now</span>
              <span className="tabular-nums text-content">
                {summary?.live.stores_online ?? 0} / {totalStores}
              </span>
            </div>
            <div className="flex justify-between">
              <span>String / panel anomalies</span>
              <span className="tabular-nums text-content">
                {summary?.alerts.stores_with_string_anomaly ?? 0}
              </span>
            </div>
            {(summary?.fleet.incomplete_stores ?? 0) > 0 && (
              <div className="flex justify-between">
                <span>New — awaiting roster data</span>
                <span className="tabular-nums text-accent-bright">
                  {summary?.fleet.incomplete_stores}
                </span>
              </div>
            )}
            {(summary?.fleet.stores_without_location ?? 0) > 0 && (
              <div className="flex justify-between">
                <span>No coordinates (not on map)</span>
                <span className="tabular-nums text-status-warn">
                  {summary?.fleet.stores_without_location}
                </span>
              </div>
            )}
          </div>
        </Card>

        {/* --- Needs attention ------------------------------------------ */}
        <Card className="lg:col-span-2">
          <CardHeader
            title="Needs attention"
            subtitle="Open alerts, worst first"
            action={
              <Link href="/alerts" className="text-2xs text-accent-bright hover:underline">
                All alerts
              </Link>
            }
          />
          {alerts.length === 0 ? (
            <EmptyState title="No open alerts" detail="Every reporting branch is behaving." />
          ) : (
            <ul className="divide-y divide-line">
              {alerts.map((alert) => (
                <li key={alert.alert_id}>
                  <Link
                    href={`/stores/${alert.store_code}`}
                    className="flex items-start gap-3 py-2.5 transition hover:bg-surface-3"
                  >
                    <Badge tone={alert.severity === "CRITICAL" ? "crit" : "warn"}>
                      {alert.severity}
                    </Badge>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="text-xs font-medium text-content">
                          {alert.store_code}
                        </span>
                        <span className="truncate text-2xs text-content-muted">
                          {alert.store_name}
                        </span>
                      </div>
                      <p className="mt-0.5 truncate text-2xs text-content-muted">
                        {alert.message}
                      </p>
                    </div>
                    <span className="shrink-0 text-2xs text-content-faint">
                      {relativeTime(alert.created_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* --- Lowest yield today ----------------------------------------- */}
      <Card className="mt-3">
        <CardHeader
          title="Lowest specific yield today"
          subtitle="Reporting branches only, ranked by kWh per kWp — the fairest like-for-like comparison available without irradiance data"
        />
        {worst.length === 0 ? (
          <EmptyState
            title="Nothing to rank yet"
            detail="No branch has reported both a yield and a capacity today."
          />
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {worst.map(({ store, sy }) => (
              <StoreYieldCard key={store.store_id} store={store} specificYield={sy} />
            ))}
          </div>
        )}
      </Card>
    </main>
  );
}

function StoreYieldCard({
  store,
  specificYield: sy,
}: {
  store: StorePin;
  specificYield: number | null;
}) {
  return (
    <Link
      href={`/stores/${store.store_code}`}
      className="card card-hover flex items-center gap-3 p-3"
    >
      <StatusDot status={store.pr_status} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="text-xs font-medium text-content">{store.store_code}</span>
          <span className="truncate text-2xs text-content-muted">{store.store_name}</span>
        </div>
        <div className="mt-0.5 text-2xs text-content-faint">
          {fmt(num(store.installed_kwp), 1)} kWp
          {store.province ? ` · ${store.province}` : ""}
        </div>
      </div>
      <div className="shrink-0 text-right">
        <div className="text-sm font-semibold tabular-nums text-content">{fmt(sy, 2)}</div>
        <div className="text-2xs text-content-faint">kWh/kWp</div>
      </div>
    </Link>
  );
}
