"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";

import MapLegend from "@/components/map/MapLegend";
import StoreDetailPanel from "@/components/map/StoreDetailPanel";
import { loadFleet } from "@/lib/api";
import type { PRStatus } from "@/lib/pr-status";
import { num, type MapResponse, type StorePin } from "@/types/store";

// MapLibre touches `window` at module scope, so it cannot be server-rendered.
const StoreMap = dynamic(() => import("@/components/map/StoreMap"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-surface text-sm text-content-muted">
      Loading map…
    </div>
  ),
});

const USE_MOCK_DEFAULT = process.env.NEXT_PUBLIC_USE_MOCK_DATA === "true";

function emptyCounts(): Record<PRStatus, number> {
  return { GREEN: 0, YELLOW: 0, RED: 0, UNKNOWN: 0 };
}

export default function MapPage() {
  const [fleet, setFleet] = useState<MapResponse | null>(null);
  const [source, setSource] = useState<"api" | "mock">("api");
  const [apiError, setApiError] = useState<string | null>(null);
  const [selectedStatus, setSelectedStatus] = useState<PRStatus | null>(null);
  const [selectedStore, setSelectedStore] = useState<StorePin | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const useMock =
      USE_MOCK_DEFAULT ||
      new URLSearchParams(window.location.search).get("mock") === "1";

    void loadFleet(useMock).then((result) => {
      // Guard against a response landing after the component unmounted, which
      // would otherwise set state on a dead component.
      if (cancelled) return;
      setFleet(result.data);
      setSource(result.source);
      setApiError(result.error ?? null);
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, []);

  const counts = useMemo(() => {
    const tally = emptyCounts();
    for (const store of fleet?.stores ?? []) tally[store.pr_status] += 1;
    return tally;
  }, [fleet]);

  const visibleStores = useMemo(() => {
    const stores = fleet?.stores ?? [];
    return selectedStatus ? stores.filter((s) => s.pr_status === selectedStatus) : stores;
  }, [fleet, selectedStatus]);

  const totals = useMemo(() => {
    const stores = fleet?.stores ?? [];
    let power = 0;
    let yieldToday = 0;
    let capacity = 0;
    for (const store of stores) {
      power += num(store.active_power_kw) ?? 0;
      yieldToday += num(store.daily_yield_kwh) ?? 0;
      capacity += num(store.installed_kwp) ?? 0;
    }
    return { power, yieldToday, capacity };
  }, [fleet]);

  const greenThreshold = num(fleet?.thresholds.pr_green_threshold ?? null) ?? 75;
  const offlineAfter = fleet?.thresholds.device_offline_after_minutes ?? 15;
  const yieldThreshold = num(fleet?.thresholds.yield_green_threshold_pct ?? null) ?? 80;
  // Derived from the data rather than from a flag: if not one branch in the
  // fleet has a PR, then yield is what coloured every pin on screen, and the
  // legend must say so.
  const usingYieldFallback =
    (fleet?.stores.length ?? 0) > 0 &&
    fleet!.stores.every((s) => s.performance_ratio === null);

  return (
    <main className="relative h-[calc(100dvh-3.5rem)] w-full overflow-hidden bg-bg">
      {/* Header */}
      <header className="pointer-events-none absolute inset-x-0 top-0 z-10 flex items-start justify-between gap-3 p-3">
        <div className="flex w-64 flex-col gap-3">
        <div className="pointer-events-auto card p-3 shadow-xl backdrop-blur">
          <h1 className="text-sm font-semibold text-content">
            MR.DIY Thailand — Solar PV Fleet
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-2xs text-content-muted">
            <span>
              <strong className="tabular-nums text-content">
                {totals.capacity.toLocaleString("en-US", { maximumFractionDigits: 0 })}
              </strong>{" "}
              kWp installed
            </span>
            <span>
              <strong className="tabular-nums text-content">
                {totals.power.toLocaleString("en-US", { maximumFractionDigits: 0 })}
              </strong>{" "}
              kW live
            </span>
            <span>
              <strong className="tabular-nums text-content">
                {totals.yieldToday.toLocaleString("en-US", { maximumFractionDigits: 0 })}
              </strong>{" "}
              kWh today
            </span>
          </div>
        </div>

        {/* Legend sits directly under the KPI card. Bottom-left was tried
            first and collided with both the scale control and Next's dev
            badge, which clipped the threshold footnote. */}
        <MapLegend
          counts={counts}
          greenThreshold={greenThreshold}
          offlineAfterMinutes={offlineAfter}
          yieldThreshold={yieldThreshold}
          usingYieldFallback={usingYieldFallback}
          selected={selectedStatus}
          onSelect={setSelectedStatus}
        />
        </div>

        {selectedStore && (
          <StoreDetailPanel store={selectedStore} onClose={() => setSelectedStore(null)} />
        )}
      </header>

      {/* Data-source banner. Mock data is never shown silently — a map full of
          invented stores that looks real is worse than an empty one. */}
      {!loading && source === "mock" && (
        <div className="pointer-events-auto absolute inset-x-0 top-[5.25rem] z-10 mx-auto w-fit rounded-full border border-status-warn/40 bg-status-warn/15 px-3 py-1 text-2xs font-medium text-status-warn shadow-lg">
          {apiError
            ? `Backend unreachable (${apiError}) — showing ${
                fleet?.stores.length ?? 0
              } mock sites`
            : `Mock data — ${fleet?.stores.length ?? 0} generated sites`}
        </div>
      )}

      {!loading && source === "api" && (fleet?.stores_without_location ?? 0) > 0 && (
        <div className="pointer-events-auto absolute inset-x-0 top-[5.25rem] z-10 mx-auto w-fit rounded-full border border-line bg-surface-2 px-3 py-1 text-2xs text-content-muted shadow-lg">
          {fleet?.stores_without_location} site
          {fleet?.stores_without_location === 1 ? "" : "s"} hidden — coordinates unknown
        </div>
      )}

      <StoreMap
        stores={visibleStores}
        onSelectStore={setSelectedStore}
        className="h-full w-full"
      />
    </main>
  );
}
