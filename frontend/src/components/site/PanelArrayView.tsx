"use client";

/**
 * Panel / string array.
 *
 * Atmoce's equivalent shows a grid of panels each printing its daily kWh. Every
 * tile is the same blue, so telling a weak panel from a healthy one means
 * reading 48 numbers and doing the comparison in your head — and MR.DIY's own
 * account has this view unconfigured on most branches anyway.
 *
 * Here the tile is SHADED BY DEVIATION FROM ITS PEERS. The comparison the eye
 * has to make is already done: a technician sees which panel to inspect without
 * reading a single number. The kWh is still printed for anyone who wants it.
 *
 * Peer grouping follows the hardware and is decided server-side — microinverter
 * panels against the whole site, strings only against others on the same MPPT.
 */

import { useMemo, useState } from "react";

import { Badge, Card, CardHeader, EmptyState, Notice, fmt } from "@/components/ui";
import { num, type PanelArray, type PanelReading } from "@/types/store";

/**
 * Colour for a deviation.
 *
 * Diverging, centred on zero: under-performing is orange through red,
 * over-performing is blue. Both directions matter — a panel reading far ABOVE
 * its peers usually means a sensor or mapping fault rather than good news.
 * Neutral grey-green sits in the middle so a healthy array looks calm.
 */
function deviationStyle(deviationPct: number | null, isAnomalous: boolean) {
  if (deviationPct === null) {
    return { background: "var(--surface-3)", border: "var(--border)", text: "var(--text-faint)" };
  }

  const d = deviationPct;
  if (d <= -50) return { background: "#7f1d1d", border: "#dc2626", text: "#fecaca" };
  if (d <= -25) return { background: "#7c2d12", border: "#ea580c", text: "#fed7aa" };
  if (d <= -10) return { background: "#78350f", border: "#f97316", text: "#fde68a" };
  if (d < -3) return { background: "#3f3a1e", border: "#5c5330", text: "#d8d2b0" };
  if (d <= 3) return { background: "#1e3a2f", border: "#2f5c4a", text: "#a7d8c4" };
  if (d < 10) return { background: "#1e3a4a", border: "#2f5c78", text: "#a7c8d8" };
  return { background: "#1e3a8a", border: "#3b82f6", text: "#bfdbfe" };
}

const LEGEND = [
  { label: "≤ −50%", d: -60 },
  { label: "−25%", d: -30 },
  { label: "−10%", d: -12 },
  { label: "normal", d: 0 },
  { label: "+10%", d: 12 },
] as const;

export default function PanelArrayView({
  array,
  loading,
  onDateChange,
  stationOnly = false,
}: {
  array: PanelArray | null;
  loading: boolean;
  onDateChange: (isoDate: string) => void;
  /** True when the branch reports only a station total — no inverter detail. */
  stationOnly?: boolean;
}) {
  const [selected, setSelected] = useState<PanelReading | null>(null);

  /** Group by inverter so a multi-inverter site reads as separate blocks. */
  const groups = useMemo(() => {
    const map = new Map<string, PanelReading[]>();
    for (const panel of array?.panels ?? []) {
      const list = map.get(panel.serial_number) ?? [];
      list.push(panel);
      map.set(panel.serial_number, list);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [array]);

  const threshold = num(array?.variance_threshold_pct ?? null) ?? 10;

  return (
    <Card>
      <CardHeader
        title="Panel & string array"
        subtitle={
          array
            ? `${array.panels.length} measured · shaded by deviation from peers · anomaly at ±${threshold}%`
            : undefined
        }
        action={
          array && (
            <input
              type="date"
              value={array.on_date}
              onChange={(e) => onDateChange(e.target.value)}
              className="rounded-lg border border-line bg-surface-2 px-2 py-1 text-2xs text-content focus:border-accent focus:outline-none"
            />
          )
        }
      />

      {loading ? (
        <div className="grid grid-cols-6 gap-2">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg bg-surface-3" />
          ))}
        </div>
      ) : !array?.has_panel_data ? (
        <EmptyState
          title="No per-panel data for this branch"
          detail={
            stationOnly
              ? "This branch reports a station total only. Its Huawei northbound account is not permitted to read individual inverters, so there is no per-string voltage or current to compare — panel-level fault detection cannot run here. Energy and power figures are unaffected."
              : "Either ingestion has not run for this date, or the hardware publishes nothing below device level. An empty array is not a fault reading."
          }
        />
      ) : (
        <>
          {array.anomaly_count > 0 && (
            <div className="mb-3">
              <Notice tone="warn">
                {array.anomaly_count} of {array.panels.length} deviate from their peers by
                more than {threshold}%. Shaded orange to red below — worst first.
              </Notice>
            </div>
          )}

          <div className="space-y-4">
            {groups.map(([serial, panels]) => (
              <div key={serial}>
                {groups.length > 1 && (
                  <div className="mb-1.5 text-2xs text-content-muted">{serial}</div>
                )}
                <div className="grid grid-cols-[repeat(auto-fill,minmax(74px,1fr))] gap-2">
                  {panels.map((panel) => {
                    const dev = num(panel.deviation_pct);
                    const style = deviationStyle(dev, panel.is_anomalous);
                    const isSelected =
                      selected?.label === panel.label &&
                      selected?.serial_number === panel.serial_number;

                    return (
                      <button
                        key={`${panel.serial_number}-${panel.label}`}
                        type="button"
                        onClick={() => setSelected(isSelected ? null : panel)}
                        title={`${panel.label} · ${fmt(num(panel.produced_kwh), 3)} kWh${
                          dev === null ? "" : ` · ${fmt(dev, 1)}% vs peers`
                        }`}
                        className={`relative rounded-lg border p-2 text-left transition ${
                          isSelected ? "ring-2 ring-accent" : ""
                        }`}
                        style={{
                          background: style.background,
                          borderColor: panel.is_anomalous ? "var(--crit)" : style.border,
                          borderWidth: panel.is_anomalous ? 2 : 1,
                        }}
                      >
                        {/* Shape as well as colour: the anomaly must be findable
                            without relying on hue alone. */}
                        {panel.is_anomalous && (
                          <span
                            aria-hidden
                            className="absolute right-1 top-1 text-[9px] font-bold text-status-crit"
                          >
                            ▲
                          </span>
                        )}
                        <div
                          className="text-xs font-semibold tabular-nums"
                          style={{ color: style.text }}
                        >
                          {fmt(num(panel.produced_kwh), 2)}
                        </div>
                        <div className="mt-0.5 text-[9px] tabular-nums" style={{ color: style.text }}>
                          {dev === null ? "—" : `${dev > 0 ? "+" : ""}${fmt(dev, 0)}%`}
                        </div>
                        <div className="mt-1 truncate text-[9px] text-content-faint">
                          {panel.label}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>

          {/* Legend */}
          <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-3">
            <span className="text-2xs text-content-muted">Deviation from peers</span>
            <div className="flex items-center gap-1">
              {LEGEND.map((entry) => {
                const s = deviationStyle(entry.d, false);
                return (
                  <div key={entry.label} className="flex items-center gap-1">
                    <span
                      className="inline-block h-3 w-5 rounded border"
                      style={{ background: s.background, borderColor: s.border }}
                    />
                    <span className="text-[9px] text-content-faint">{entry.label}</span>
                  </div>
                );
              })}
            </div>
            <span className="ml-auto flex items-center gap-1 text-2xs text-content-muted">
              <span aria-hidden className="text-status-crit">▲</span> outside threshold
            </span>
          </div>

          {selected && (
            <div className="mt-3 rounded-lg border border-line bg-surface-3 p-3">
              <div className="flex items-baseline justify-between">
                <span className="text-xs font-semibold text-content">{selected.label}</span>
                {selected.is_anomalous && <Badge tone="crit">Anomaly</Badge>}
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-2xs sm:grid-cols-4">
                <Detail label="Produced" value={`${fmt(num(selected.produced_kwh), 3)} kWh`} />
                <Detail label="Mean power" value={`${fmt(num(selected.avg_power_kw), 3)} kW`} />
                <Detail
                  label="vs peers"
                  value={
                    num(selected.deviation_pct) === null
                      ? "too few peers"
                      : `${fmt(num(selected.deviation_pct), 2)}%`
                  }
                />
                <Detail label="Inverter" value={selected.serial_number} />
              </dl>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-content-muted">{label}</dt>
      <dd className="mt-0.5 font-medium text-content">{value}</dd>
    </div>
  );
}
