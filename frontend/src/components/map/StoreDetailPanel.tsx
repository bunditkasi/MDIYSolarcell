"use client";

import { STATUS_STYLES } from "@/lib/pr-status";
import { num, type StorePin } from "@/types/store";

export interface StoreDetailPanelProps {
  store: StorePin;
  onClose: () => void;
}

function Metric({
  label,
  value,
  unit,
}: {
  label: string;
  value: number | null;
  unit: string;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface-3 px-2.5 py-2">
      <dt className="text-2xs uppercase tracking-wide text-content-muted">{label}</dt>
      <dd className="mt-0.5 text-sm font-semibold tabular-nums text-content">
        {value === null ? (
          <span className="text-content-faint">no data</span>
        ) : (
          <>
            {value.toLocaleString("en-US", { maximumFractionDigits: 1 })}
            <span className="ml-0.5 text-2xs font-normal text-content-muted">{unit}</span>
          </>
        )}
      </dd>
    </div>
  );
}

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

/** Detail shown on pin click. Phase 2 replaces this with a full site page. */
export default function StoreDetailPanel({ store, onClose }: StoreDetailPanelProps) {
  const style = STATUS_STYLES[store.pr_status];

  return (
    <aside className="pointer-events-auto card w-80 shadow-2xl backdrop-blur">
      <header className="flex items-start justify-between gap-2 border-b border-line p-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span aria-hidden style={{ color: style.color }} className="text-sm leading-none">
              {style.glyph}
            </span>
            <span
              className="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white"
              style={{ backgroundColor: style.color }}
            >
              {style.label}
            </span>
          </div>
          <h2 className="mt-1.5 truncate text-sm font-semibold text-content">
            {store.store_name}
          </h2>
          <p className="text-2xs text-content-muted">
            {store.store_code}
            {store.province ? ` · ${store.province}` : ""}
            {store.region ? ` · ${store.region}` : ""}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close site detail"
          className="rounded p-1 text-content-faint transition hover:bg-surface-3 hover:text-content"
        >
          ✕
        </button>
      </header>

      <dl className="grid grid-cols-2 gap-2 p-3">
        <Metric label="Live power" value={num(store.active_power_kw)} unit="kW" />
        <Metric label="Yield today" value={num(store.daily_yield_kwh)} unit="kWh" />
        <Metric label="Capacity" value={num(store.installed_kwp)} unit="kWp" />
        {/* PR is the better measure but needs irradiance data. Where it is
            absent, showing an empty "Performance ratio" tells the reader
            nothing about why the pin is the colour it is — so the fallback
            that DID decide it takes the slot instead. */}
        {store.performance_ratio !== null ? (
          <Metric label="Performance ratio" value={num(store.performance_ratio)} unit="%" />
        ) : (
          <Metric
            label="vs fleet median"
            value={num(store.yield_vs_peers_pct)}
            unit="%"
          />
        )}
      </dl>

      <div className="space-y-1 border-t border-line px-3 py-2 text-2xs">
        <div className="flex justify-between">
          <span className="text-content-muted">Last reading</span>
          <span className="font-medium text-content">{relativeTime(store.last_seen_at)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-content-muted">Open alerts</span>
          <span
            className={`font-medium ${
              store.open_alert_count > 0 ? "text-status-crit" : "text-content"
            }`}
          >
            {store.open_alert_count}
          </span>
        </div>
        {store.has_string_anomaly && (
          <p className="mt-1 rounded-lg border border-status-warn/30 bg-status-warn/10 px-2 py-1.5 text-2xs text-status-warn">
            String anomaly: at least one string deviates from its peers on the same MPPT.
          </p>
        )}
        {store.pr_status === "UNKNOWN" && (
          <p className="mt-1 rounded-lg border border-line bg-surface-3 px-2 py-1.5 text-2xs text-content-muted">
            No irradiance baseline for this site yet, so PR cannot be computed. This is not a
            fault indication.
          </p>
        )}
      </div>
    </aside>
  );
}
