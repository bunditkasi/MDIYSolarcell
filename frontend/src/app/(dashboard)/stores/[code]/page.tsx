"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

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
  relativeTime,
} from "@/components/ui";
import EnergyReportView, { type Granularity } from "@/components/site/EnergyReportView";
import PanelArrayView from "@/components/site/PanelArrayView";
import {
  fetchAlerts,
  fetchEnergyHistory,
  fetchMapPins,
  fetchPanelArray,
  fetchStoreDevices,
} from "@/lib/api";
import { STATUS_STYLES } from "@/lib/pr-status";
import {
  num,
  specificYield,
  type AlertItem,
  type DeviceItem,
  type EnergyHistory,
  type PanelArray,
  type StorePin,
} from "@/types/store";

export default function StoreDetailPage() {
  const params = useParams<{ code: string }>();
  const code = decodeURIComponent(params.code ?? "");

  const [store, setStore] = useState<StorePin | null>(null);
  const [devices, setDevices] = useState<DeviceItem[]>([]);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);

  const [tab, setTab] = useState<TabKey>("overview");
  const [array, setArray] = useState<PanelArray | null>(null);
  const [arrayDate, setArrayDate] = useState<string>(() =>
    new Date().toISOString().slice(0, 10),
  );
  const [arrayLoading, setArrayLoading] = useState(false);
  const [history, setHistory] = useState<EnergyHistory | null>(null);
  const [granularity, setGranularity] = useState<Granularity>("day");
  const [historyLoading, setHistoryLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;

    void fetchMapPins()
      .then(async (data) => {
        const match = data.stores.find(
          (s) => s.store_code.toUpperCase() === code.toUpperCase(),
        );
        if (cancelled) return;

        if (!match) {
          setNotFound(true);
          setLoading(false);
          return;
        }

        setStore(match);
        // Devices and alerts are independent; one failing must not blank the
        // other, so they are settled rather than chained.
        const [dev, alertPage] = await Promise.allSettled([
          fetchStoreDevices(match.store_id),
          fetchAlerts({ storeId: match.store_id, limit: 50 }),
        ]);
        if (cancelled) return;
        if (dev.status === "fulfilled") setDevices(dev.value);
        if (alertPage.status === "fulfilled") setAlerts(alertPage.value.items);
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) {
          setNotFound(true);
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [code]);

  // Array and report data load only when their tab is opened. Fetching all
  // three up front would triple the work for a page most people open to read
  // the headline numbers and leave.
  useEffect(() => {
    if (!store || tab !== "array") return;
    let cancelled = false;
    setArrayLoading(true);
    void fetchPanelArray(store.store_id, arrayDate)
      .then((data) => !cancelled && setArray(data))
      .catch(() => !cancelled && setArray(null))
      .finally(() => !cancelled && setArrayLoading(false));
    return () => {
      cancelled = true;
    };
  }, [store, tab, arrayDate]);

  useEffect(() => {
    if (!store || tab !== "reports") return;
    let cancelled = false;
    setHistoryLoading(true);
    void fetchEnergyHistory(store.store_id, { granularity })
      .then((data) => !cancelled && setHistory(data))
      .catch(() => !cancelled && setHistory(null))
      .finally(() => !cancelled && setHistoryLoading(false));
    return () => {
      cancelled = true;
    };
  }, [store, tab, granularity]);

  if (loading) {
    return (
      <main className="mx-auto max-w-[1600px] px-4 py-5">
        <Skeleton className="mb-4 h-16" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[92px]" />
          ))}
        </div>
      </main>
    );
  }

  if (notFound || !store) {
    return (
      <main className="mx-auto max-w-[1600px] px-4 py-5">
        <Card>
          <EmptyState
            title={`No branch with code “${code}”`}
            detail="It may not have coordinates yet, in which case it is absent from the map feed this page reads."
            action={
              <Link
                href="/fleet"
                className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-content-muted hover:text-content"
              >
                Back to fleet
              </Link>
            }
          />
        </Card>
      </main>
    );
  }

  const style = STATUS_STYLES[store.pr_status];
  const sy = specificYield(store.daily_yield_kwh, store.installed_kwp);
  // The station aggregate is not hardware anyone can inspect — it is the row
  // the site total hangs from. It must not be counted when describing what
  // equipment the branch has, or a normal microinverter site reads as "mixed".
  const realDevices = devices.filter((d) => d.device_type !== "LOGGER");
  const hasPanelBasis = realDevices.some((d) => d.measurement_basis === "PANEL");
  const hasStringBasis = realDevices.some((d) => d.measurement_basis === "STRING");
  // A branch whose only device is the station aggregate has no inverter-level
  // data at all — the vendor account cannot read individual inverters.
  const stationOnly = devices.length > 0 && realDevices.length === 0;

  return (
    <main className="mx-auto max-w-[1600px] animate-fade-in px-4 py-5">
      {/* --- Header ------------------------------------------------------ */}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Link href="/fleet" className="text-xs text-content-muted hover:text-content">
              Fleet
            </Link>
            <span className="text-content-faint">/</span>
            <span className="text-xs text-content">{store.store_code}</span>
          </div>
          <h1 className="mt-1 flex items-center gap-2.5 text-lg font-semibold tracking-tight">
            <span aria-hidden style={{ color: style.color }}>
              {style.glyph}
            </span>
            {store.store_name}
          </h1>
          <p className="mt-0.5 text-xs text-content-muted">
            {store.province ?? "Province unknown"}
            {store.region ? ` · ${store.region}` : ""} · last reading{" "}
            {relativeTime(store.last_seen_at)}
          </p>
        </div>

        <div className="flex items-center gap-2">
          {store.is_incomplete && (
            <span
              className="rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent-bright"
              title="Reported by the vendor but not yet in the roster. Capacity or position is still missing, so this branch is excluded from capacity-weighted figures."
            >
              New
            </span>
          )}
          <span
            className="rounded-lg px-2.5 py-1 text-xs font-medium text-white"
            style={{ background: style.color }}
          >
            {style.label}
          </span>
          <Link
            href="/map"
            className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-content-muted transition hover:border-line-strong hover:text-content"
          >
            Show on map
          </Link>
        </div>
      </div>

      {/* --- KPIs -------------------------------------------------------- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Live output"
          value={fmt(num(store.active_power_kw), 1)}
          unit="kW"
          tone="accent"
        />
        <StatTile label="Energy today" value={fmt(num(store.daily_yield_kwh), 1)} unit="kWh" />
        <StatTile
          label="Specific yield"
          value={fmt(sy, 2)}
          unit="kWh/kWp"
          hint="Energy today per kWp installed. With no irradiance baseline this is what decides the branch's status, measured against the fleet median for the same day."
        />
        {store.performance_ratio ? (
          <StatTile
            label="Performance ratio"
            value={fmt(num(store.performance_ratio), 1)}
            unit="%"
            hint="Actual yield against what the measured irradiance should have produced."
          />
        ) : (
          <StatTile
            label="vs fleet median"
            value={
              store.yield_vs_peers_pct ? fmt(num(store.yield_vs_peers_pct), 0) : null
            }
            unit="%"
            hint="This branch's specific yield as a share of the fleet median today. 100% is typical; the status turns yellow below 80%. Used because Performance Ratio needs a Solcast irradiance baseline and no API key is configured."
          />
        )}
      </div>

      {/* --- Tabs -------------------------------------------------------- */}
      <div className="mt-4 flex gap-1 border-b border-line">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            aria-current={tab === t.key ? "page" : undefined}
            className={`-mb-px border-b-2 px-3 py-2 text-xs font-medium transition ${
              tab === t.key
                ? "border-accent text-content"
                : "border-transparent text-content-muted hover:text-content"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "array" && (
        <div className="mt-3">
          <PanelArrayView
            array={array}
            loading={arrayLoading}
            onDateChange={setArrayDate}
            stationOnly={stationOnly}
          />
        </div>
      )}

      {tab === "reports" && (
        <div className="mt-3">
          <EnergyReportView
            history={history}
            loading={historyLoading}
            granularity={granularity}
            onGranularityChange={setGranularity}
            storeCode={store.store_code}
          />
        </div>
      )}

      {tab === "overview" && (
      <>
      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        {/* --- Site facts ------------------------------------------------ */}
        <Card>
          <CardHeader title="Installation" />
          <dl className="space-y-2 text-xs">
            <Fact label="Branch code" value={store.store_code} />
            <Fact
              label="Capacity"
              value={
                store.installed_kwp === null
                  ? "not recorded yet"
                  : `${fmt(num(store.installed_kwp), 2)} kWp`
              }
            />
            <Fact label="Province" value={store.province ?? "—"} />
            <Fact
              label="Coordinates"
              value={
                store.lat && store.lng
                  ? `${Number(store.lat).toFixed(5)}, ${Number(store.lng).toFixed(5)}`
                  : "not recorded"
              }
            />
            <Fact
              label="Reporting"
              value={store.is_online ? "online" : "no recent reading"}
              tone={store.is_online ? "ok" : "crit"}
            />
          </dl>
        </Card>

        {/* --- Devices --------------------------------------------------- */}
        <Card className="lg:col-span-2">
          <CardHeader
            title="Devices"
            subtitle={
              devices.length === 0
                ? undefined
                : stationOnly
                  ? "Station total only — this vendor account cannot read individual inverters"
                  : hasPanelBasis && !hasStringBasis
                    ? "Microinverters — per-panel power, no per-string voltage or current exists on this hardware"
                    : hasStringBasis && !hasPanelBasis
                      ? "String inverters — per-string voltage and current available"
                      : "Mixed hardware"
            }
          />
          {devices.length === 0 ? (
            <EmptyState
              title="No devices registered"
              detail="Devices appear once vendor ingestion is connected and the first sync completes."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[520px] text-xs">
                <thead>
                  <tr className="border-b border-line text-2xs uppercase tracking-wide text-content-muted">
                    <th className="px-2 py-2 text-left font-medium">Serial</th>
                    <th className="px-2 py-2 text-left font-medium">Brand / model</th>
                    <th className="px-2 py-2 text-left font-medium">Type</th>
                    <th className="px-2 py-2 text-right font-medium">Rated</th>
                    <th className="px-2 py-2 text-right font-medium">MPPT</th>
                  </tr>
                </thead>
                <tbody>
                  {devices.map((d) => (
                    <tr key={d.device_id} className="border-b border-line/60 last:border-0">
                      <td className="px-2 py-2 font-medium text-content">
                        {d.serial_number}
                      </td>
                      <td className="px-2 py-2 text-content-muted">
                        {d.brand}
                        {d.model ? ` · ${d.model}` : ""}
                      </td>
                      <td className="px-2 py-2">
                        <Badge tone={d.measurement_basis === "PANEL" ? "accent" : "neutral"}>
                          {d.measurement_basis === "PANEL" ? "Microinverter" : "String"}
                        </Badge>
                      </td>
                      <td className="num px-2 py-2 text-right text-content">
                        {d.capacity_kw ? `${fmt(num(d.capacity_kw), 1)} kW` : "—"}
                      </td>
                      <td className="num px-2 py-2 text-right text-content-muted">
                        {d.mppt_count ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {store.has_string_anomaly && (
        <div className="mt-3">
          <Notice tone="warn">
            An anomaly is open at this branch — open the <strong>Array</strong> tab to see
            which panel or string is deviating and by how much.
          </Notice>
        </div>
      )}

      {/* --- Alerts ------------------------------------------------------ */}
      <Card className="mt-3" padded={false}>
        <div className="p-4 pb-0">
          <CardHeader title="Alerts" subtitle={`${alerts.length} for this branch`} />
        </div>
        {alerts.length === 0 ? (
          <EmptyState title="No alerts" detail="This branch is behaving." />
        ) : (
          <ul className="divide-y divide-line">
            {alerts.map((alert) => (
              <li key={alert.alert_id} className="flex items-start gap-3 px-4 py-3">
                <Badge
                  tone={
                    alert.severity === "CRITICAL"
                      ? "crit"
                      : alert.severity === "MAJOR"
                        ? "warn"
                        : "neutral"
                  }
                >
                  {alert.severity}
                </Badge>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-content">{alert.message}</p>
                  <p className="mt-0.5 text-2xs text-content-faint">
                    {alert.alert_type} · {alert.status}
                  </p>
                </div>
                <span className="shrink-0 text-2xs text-content-muted">
                  {relativeTime(alert.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      </>
      )}
    </main>
  );
}

type TabKey = "overview" | "array" | "reports";

const TABS: { key: TabKey; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "array", label: "Array" },
  { key: "reports", label: "Reports" },
];

function Fact({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "crit";
}) {
  const tones = { ok: "text-status-ok", crit: "text-status-crit" };
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-content-muted">{label}</dt>
      <dd className={`text-right ${tone ? tones[tone] : "text-content"}`}>{value}</dd>
    </div>
  );
}
