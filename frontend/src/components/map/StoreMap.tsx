"use client";

/**
 * Fleet GIS map — MapLibre GL JS over OpenFreeMap vector tiles.
 *
 * THE PERFORMANCE DECISION THAT MATTERS
 * -------------------------------------
 * All 200+ stores are ONE GeoJSON source rendered by circle layers with
 * data-driven styling. They are deliberately NOT `new maplibregl.Marker()` per
 * store: markers are real DOM nodes that the browser must reposition on every
 * frame of every pan and zoom. At 200 pins that is 200 elements being laid out
 * continuously and the map visibly stutters. Circle layers are drawn by the GPU
 * in a single pass and stay smooth into the thousands.
 *
 * The same reasoning applies to the tooltip: ONE Popup instance is created up
 * front and re-pointed on hover. Constructing a Popup inside the mousemove
 * handler would allocate and tear down DOM on every pointer move.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import maplibregl, {
  type ExpressionSpecification,
  type GeoJSONSource,
  type LngLatLike,
  type MapGeoJSONFeature,
  type MapLayerMouseEvent,
  type MapMouseEvent,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import {
  STATUS_STYLES,
  statusColorExpression,
  statusRadiusExpression,
  type PRStatus,
} from "@/lib/pr-status";
import { num, type StorePin } from "@/types/store";

const SOURCE_ID = "stores";
const CLUSTER_LAYER = "store-clusters";
const CLUSTER_COUNT_LAYER = "store-cluster-count";
const CLUSTER_ALERT_LAYER = "store-cluster-alert";
const PIN_LAYER = "store-pins";
const PIN_ALERT_LAYER = "store-pin-alert";

const MAP_STYLE =
  process.env.NEXT_PUBLIC_MAP_STYLE_URL ??
  "https://tiles.openfreemap.org/styles/liberty";

const DEFAULT_CENTER: LngLatLike = [
  Number(process.env.NEXT_PUBLIC_MAP_CENTER_LNG ?? 100.5018),
  Number(process.env.NEXT_PUBLIC_MAP_CENTER_LAT ?? 13.7563),
];
const DEFAULT_ZOOM = Number(process.env.NEXT_PUBLIC_MAP_DEFAULT_ZOOM ?? 5.2);

export interface StoreMapProps {
  stores: StorePin[];
  onSelectStore?: (store: StorePin) => void;
  className?: string;
}

function toFeatureCollection(stores: StorePin[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: stores.flatMap((store) => {
      const lat = num(store.lat);
      const lng = num(store.lng);
      // A pin with no coordinate cannot be drawn. The backend already filters
      // these out and reports the count separately; this is belt and braces so
      // a bad row can never produce a NaN geometry that breaks the whole layer.
      if (lat === null || lng === null) return [];

      return [
        {
          type: "Feature" as const,
          geometry: { type: "Point" as const, coordinates: [lng, lat] },
          properties: {
            store_id: store.store_id,
            store_code: store.store_code,
            store_name: store.store_name,
            province: store.province ?? "",
            region: store.region ?? "",
            pr_status: store.pr_status,
            performance_ratio: store.performance_ratio ?? "",
            // With no irradiance baseline these are what actually decided
            // pr_status, so the popup must be able to show them.
            specific_yield_kwh_per_kwp: store.specific_yield_kwh_per_kwp ?? "",
            yield_vs_peers_pct: store.yield_vs_peers_pct ?? "",
            active_power_kw: store.active_power_kw ?? "",
            daily_yield_kwh: store.daily_yield_kwh ?? "",
            installed_kwp: store.installed_kwp,
            is_online: store.is_online,
            has_string_anomaly: store.has_string_anomaly,
            open_alert_count: store.open_alert_count,
            is_incomplete: store.is_incomplete,
            has_ever_reported: store.has_ever_reported,
            last_seen_at: store.last_seen_at ?? "",
            is_critical: store.pr_status === "RED",
          },
        },
      ];
    }),
  };
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatNumber(value: string | number | null, digits = 1, suffix = ""): string {
  const parsed = typeof value === "number" ? value : num(value as string | null);
  if (parsed === null) return "—";
  return `${parsed.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}${suffix}`;
}

function tooltipHtml(props: Record<string, unknown>): string {
  const status = String(props.pr_status ?? "UNKNOWN") as PRStatus;
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.UNKNOWN;
  const pr = props.performance_ratio ? String(props.performance_ratio) : null;

  const anomalyRow = props.has_string_anomaly
    ? `<div class="mt-1 text-[11px] font-medium text-status-warn">String anomaly detected</div>`
    : "";

  const alertCount = Number(props.open_alert_count ?? 0);
  const alertRow =
    alertCount > 0
      ? `<div class="mt-1 text-[11px] font-medium text-status-crit">${alertCount} open alert${
          alertCount === 1 ? "" : "s"
        }</div>`
      : "";

  return `
    <div class="min-w-[210px] font-sans">
      <div class="flex items-center gap-2">
        <span style="color:${style.color}" class="text-sm leading-none">${style.glyph}</span>
        <span class="text-sm font-semibold text-content">${escapeHtml(
          String(props.store_name ?? ""),
        )}</span>
      </div>
      <div class="mt-0.5 text-[11px] text-content-muted">
        ${escapeHtml(String(props.store_code ?? ""))}${
          props.province ? ` · ${escapeHtml(String(props.province))}` : ""
        }
      </div>

      <dl class="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <dt class="text-content-muted">Live power</dt>
        <dd class="text-right font-medium text-content">${formatNumber(
          String(props.active_power_kw ?? ""),
          1,
          " kW",
        )}</dd>

        <dt class="text-content-muted">Yield today</dt>
        <dd class="text-right font-medium text-content">${formatNumber(
          String(props.daily_yield_kwh ?? ""),
          1,
          " kWh",
        )}</dd>

        <dt class="text-content-muted">Capacity</dt>
        <dd class="text-right font-medium text-content">${formatNumber(
          String(props.installed_kwp ?? ""),
          2,
          " kWp",
        )}</dd>

        <dt class="text-content-muted">PR</dt>
        <dd class="text-right font-medium text-content">${
          pr ? formatNumber(pr, 1, "%") : "no data"
        }</dd>
      </dl>

      ${anomalyRow}
      ${alertRow}
      <div class="mt-2 border-t border-line pt-1 text-[10px] text-content-faint">
        ${style.label} · click for detail
      </div>
    </div>
  `;
}

export default function StoreMap({ stores, onSelectStore, className }: StoreMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const [styleReady, setStyleReady] = useState(false);

  // Kept in a ref so the click handler, registered once, always sees the
  // current callback without the layer having to be re-bound on every render.
  const onSelectRef = useRef(onSelectStore);
  useEffect(() => {
    onSelectRef.current = onSelectStore;
  }, [onSelectStore]);

  const handleClick = useCallback((event: MapMouseEvent) => {
    const map = mapRef.current;
    if (!map) return;

    const features = map.queryRenderedFeatures(event.point, {
      layers: [PIN_LAYER],
    });
    const feature = features[0] as MapGeoJSONFeature | undefined;
    if (!feature) return;

    const props = feature.properties ?? {};
    onSelectRef.current?.({
      store_id: String(props.store_id ?? ""),
      store_code: String(props.store_code ?? ""),
      store_name: String(props.store_name ?? ""),
      region: props.region ? String(props.region) : null,
      province: props.province ? String(props.province) : null,
      lat: String((feature.geometry as GeoJSON.Point).coordinates[1]),
      lng: String((feature.geometry as GeoJSON.Point).coordinates[0]),
      installed_kwp: String(props.installed_kwp ?? "0"),
      pr_status: String(props.pr_status ?? "UNKNOWN") as PRStatus,
      performance_ratio: props.performance_ratio ? String(props.performance_ratio) : null,
      specific_yield_kwh_per_kwp: props.specific_yield_kwh_per_kwp
        ? String(props.specific_yield_kwh_per_kwp)
        : null,
      yield_vs_peers_pct: props.yield_vs_peers_pct
        ? String(props.yield_vs_peers_pct)
        : null,
      active_power_kw: props.active_power_kw ? String(props.active_power_kw) : null,
      daily_yield_kwh: props.daily_yield_kwh ? String(props.daily_yield_kwh) : null,
      last_seen_at: props.last_seen_at ? String(props.last_seen_at) : null,
      is_online: Boolean(props.is_online),
      has_string_anomaly: Boolean(props.has_string_anomaly),
      open_alert_count: Number(props.open_alert_count ?? 0),
      is_incomplete: Boolean(props.is_incomplete),
      has_ever_reported: Boolean(props.has_ever_reported),
      max_alert_severity: null,
    });
  }, []);

  // --- Map construction. Runs once. ---------------------------------------
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
      attributionControl: { compact: true },
    });
    mapRef.current = map;

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

    // One popup, reused for the lifetime of the map.
    popupRef.current = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 14,
      maxWidth: "280px",
      className: "store-tooltip",
    });

    map.on("load", () => {
      map.addSource(SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
        cluster: true,
        clusterMaxZoom: 11,
        clusterRadius: 46,
        // Aggregated at cluster level so a collapsed group can still show that
        // it contains failures. Without this an executive looking at Thailand
        // zoomed out would see only neutral bubbles and miss every outage.
        clusterProperties: {
          critical_count: ["+", ["case", ["get", "is_critical"], 1, 0]],
        },
      });

      map.addLayer({
        id: CLUSTER_LAYER,
        type: "circle",
        source: SOURCE_ID,
        filter: ["has", "point_count"],
        paint: {
          "circle-color": "#2563eb",
          "circle-radius": [
            "step",
            ["get", "point_count"],
            16,
            10,
            22,
            30,
            28,
          ] as unknown as ExpressionSpecification,
          "circle-opacity": 0.85,
          // A cluster containing failures gets a red RING, not a red body.
          // Filling it red instead was the first thing tried and it made a
          // 98-site cluster holding one outage look like 98 outages — at
          // country zoom nearly every cluster went red and the signal was lost.
          // The ring says "there is something in here" without overstating how
          // much, and the count badge below gives the actual number.
          "circle-stroke-width": [
            "case",
            [">", ["get", "critical_count"], 0],
            3.5,
            2,
          ] as unknown as ExpressionSpecification,
          "circle-stroke-color": [
            "case",
            [">", ["get", "critical_count"], 0],
            STATUS_STYLES.RED.color,
            "#ffffff",
          ] as unknown as ExpressionSpecification,
        },
      });

      map.addLayer({
        id: CLUSTER_COUNT_LAYER,
        type: "symbol",
        source: SOURCE_ID,
        filter: ["has", "point_count"],
        layout: {
          "text-field": ["get", "point_count_abbreviated"],
          "text-font": ["Noto Sans Bold"],
          "text-size": 12,
          "text-allow-overlap": true,
        },
        paint: { "text-color": "#ffffff" },
      });

      // Number of critical sites hidden inside the cluster. Without this the
      // ring says "a problem exists" but not how big it is, and an operator
      // has to zoom into every ringed cluster to find out.
      map.addLayer({
        id: CLUSTER_ALERT_LAYER,
        type: "symbol",
        source: SOURCE_ID,
        filter: ["all", ["has", "point_count"], [">", ["get", "critical_count"], 0]],
        layout: {
          "text-field": ["concat", "!", ["to-string", ["get", "critical_count"]]],
          "text-font": ["Noto Sans Bold"],
          "text-size": 10,
          "text-offset": [0, -1.9],
          "text-allow-overlap": true,
          "text-ignore-placement": true,
        },
        paint: {
          "text-color": "#ffffff",
          "text-halo-color": STATUS_STYLES.RED.color,
          "text-halo-width": 2.5,
        },
      });

      map.addLayer({
        id: PIN_LAYER,
        type: "circle",
        source: SOURCE_ID,
        filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-color": statusColorExpression() as unknown as ExpressionSpecification,
          "circle-radius": statusRadiusExpression() as unknown as ExpressionSpecification,
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
          "circle-opacity": 0.92,
        },
      });

      // Second, non-colour channel for the critical state. Red/green is the
      // pair operators most need to distinguish and the pair red-green colour
      // blindness collapses, so criticals also carry a glyph.
      map.addLayer({
        id: PIN_ALERT_LAYER,
        type: "symbol",
        source: SOURCE_ID,
        filter: ["all", ["!", ["has", "point_count"]], ["==", ["get", "pr_status"], "RED"]],
        layout: {
          "text-field": "!",
          "text-font": ["Noto Sans Bold"],
          "text-size": 11,
          "text-allow-overlap": true,
          "text-ignore-placement": true,
        },
        paint: { "text-color": "#ffffff" },
      });

      setStyleReady(true);
    });

    // --- Hover tooltip ----------------------------------------------------
    const handleEnter = (event: MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      if (!feature || !popupRef.current) return;

      map.getCanvas().style.cursor = "pointer";
      popupRef.current
        .setLngLat((feature.geometry as GeoJSON.Point).coordinates as [number, number])
        .setHTML(tooltipHtml(feature.properties ?? {}))
        .addTo(map);
    };

    const handleLeave = () => {
      map.getCanvas().style.cursor = "";
      popupRef.current?.remove();
    };

    map.on("mouseenter", PIN_LAYER, handleEnter);
    map.on("mousemove", PIN_LAYER, handleEnter);
    map.on("mouseleave", PIN_LAYER, handleLeave);
    map.on("click", PIN_LAYER, handleClick);

    // Clicking a cluster zooms into it rather than doing nothing.
    map.on("click", CLUSTER_LAYER, (event) => {
      const feature = map.queryRenderedFeatures(event.point, {
        layers: [CLUSTER_LAYER],
      })[0];
      if (!feature) return;

      const clusterId = feature.properties?.cluster_id as number | undefined;
      if (clusterId === undefined) return;

      const source = map.getSource(SOURCE_ID) as GeoJSONSource;
      void source.getClusterExpansionZoom(clusterId).then((zoom) => {
        map.easeTo({
          center: (feature.geometry as GeoJSON.Point).coordinates as [number, number],
          zoom,
        });
      });
    });

    map.on("mouseenter", CLUSTER_LAYER, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", CLUSTER_LAYER, () => {
      map.getCanvas().style.cursor = "";
    });

    return () => {
      // Without this the WebGL context and its tile caches leak on every
      // hot-reload, and after a few edits the browser refuses new contexts.
      popupRef.current?.remove();
      popupRef.current = null;
      map.remove();
      mapRef.current = null;
      setStyleReady(false);
    };
  }, [handleClick]);

  // --- Data updates. Replaces source data instead of rebuilding the map. ---
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;

    const source = map.getSource(SOURCE_ID) as GeoJSONSource | undefined;
    source?.setData(toFeatureCollection(stores));
  }, [stores, styleReady]);

  return (
    <div
      ref={containerRef}
      className={className ?? "h-full w-full"}
      role="application"
      aria-label="Map of MR.DIY solar sites across Thailand"
    />
  );
}
