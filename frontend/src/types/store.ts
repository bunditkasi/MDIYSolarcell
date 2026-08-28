import type { PRStatus } from "@/lib/pr-status";

/** One map pin — mirrors `StoreStatusOut` in backend/app/api/v1/schemas.py. */
export interface StorePin {
  store_id: string;
  store_code: string;
  store_name: string;
  region: string | null;
  province: string | null;
  /** Numeric strings: the API serialises NUMERIC columns as strings to avoid
   *  float rounding. Parse before arithmetic. */
  lat: string;
  lng: string;
  /** null when nobody has recorded the capacity yet. Never treat as 0. */
  installed_kwp: string | null;
  /** Capacity or position still missing — shown as a "New" badge. */
  is_incomplete: boolean;
  pr_status: PRStatus;
  performance_ratio: string | null;
  active_power_kw: string | null;
  daily_yield_kwh: string | null;
  last_seen_at: string | null;
  is_online: boolean;
  has_string_anomaly: boolean;
  open_alert_count: number;
  max_alert_severity: "CRITICAL" | "MAJOR" | "MINOR" | null;
  /** Today's kWh per installed kWp. Available wherever capacity is recorded. */
  specific_yield_kwh_per_kwp: string | null;
  /** The above as a percentage of the fleet median for the same day. When
   *  `performance_ratio` is null this is what decided `pr_status`. */
  yield_vs_peers_pct: string | null;
  has_ever_reported: boolean;
}

/**
 * One row of the fleet table.
 *
 * Deliberately not a StorePin: a pin needs a position and this does not, so the
 * fleet list can include the branches whose coordinates are still unknown.
 */
export interface FleetRow {
  store_id: string;
  /** Vendor cloud the data comes from. Null before a branch has reported. */
  source: string | null;
  pr_status: PRStatus;
  store_code: string;
  store_name: string;
  province: string | null;
  /** Grid connection date. */
  commissioned_at: string | null;
  installed_kwp: string | null;
  /** Null where the vendor publishes no battery figure — not the same as 0. */
  battery_capacity_kwh: string | null;
  active_power_kw: string | null;
  daily_yield_kwh: string | null;
  monthly_yield_kwh: string | null;
  lifetime_yield_kwh: string | null;
  last_seen_at: string | null;
  is_online: boolean;
  is_incomplete: boolean;
  has_location: boolean;
  /** False when no vendor account has ever delivered a reading for this
   *  branch — waiting on access or commissioning, not failing. */
  has_ever_reported: boolean;
  open_alert_count: number;
}

export interface FleetResponse {
  rows: FleetRow[];
  thresholds: Thresholds;
}

export interface Thresholds {
  pr_green_threshold: string;
  string_variance_threshold_pct: string;
  device_offline_after_minutes: number;
  /** Specific-yield threshold as a percentage of the fleet median, used when
   *  no irradiance baseline exists and PR% cannot be computed. */
  yield_green_threshold_pct: string;
}

export interface MapResponse {
  stores: StorePin[];
  thresholds: Thresholds;
  /** Matching stores omitted because their coordinates are unknown. */
  stores_without_location: number;
}

export interface DashboardSummary {
  fleet: {
    total_stores: number;
    active_stores: number;
    stores_without_location: number;
    incomplete_stores: number;
    total_installed_kwp: string;
  };
  live: {
    stores_online: number;
    stores_offline: number;
    total_active_power_kw: string;
    total_daily_yield_kwh: string;
  };
  status_counts: Record<PRStatus, number>;
  performance: {
    fleet_performance_ratio: string | null;
    stores_with_pr: number;
  };
  alerts: {
    open_alert_count: number;
    by_severity: { CRITICAL: number; MAJOR: number; MINOR: number };
    stores_with_string_anomaly: number;
  };
  esg: {
    co2_avoided_today_kg: string | null;
    emission_factor: string;
    emission_factor_year: number;
    standard: string;
  };
  thresholds: Thresholds;
}

/** Parse an API numeric string. Returns null for null/empty/unparseable. */
export function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}


// ---------------------------------------------------------------------------
// Additional API shapes
// ---------------------------------------------------------------------------

export interface Paged<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

/** Mirrors `StoreOut` in backend/app/api/v1/schemas.py. */
export interface StoreItem {
  store_id: string;
  store_code: string;
  retail_store_code: string | null;
  store_name: string;
  region: string | null;
  province: string | null;
  address: string | null;
  installed_kwp: string | null;
  lat: string | null;
  lng: string | null;
  rollout_phase: number | null;
  monitoring_source: string | null;
  commissioned_at: string | null;
  capex_net: string | null;
  is_active: boolean;
}

export interface DeviceItem {
  device_id: string;
  store_id: string;
  brand: string;
  model: string | null;
  serial_number: string;
  device_type: string;
  /** STRING (per-MPPT I/V available) or PANEL (per-panel power only). */
  measurement_basis: string;
  vendor_key: string | null;
  capacity_kw: string | null;
  mppt_count: number | null;
  is_active: boolean;
}

export type AlertSeverity = "CRITICAL" | "MAJOR" | "MINOR";
export type AlertStatus = "OPEN" | "ACKNOWLEDGED" | "RESOLVED";

export interface AlertItem {
  alert_id: string;
  store_id: string;
  store_code: string;
  store_name: string;
  province: string | null;
  device_id: string | null;
  alert_type: string;
  severity: AlertSeverity;
  status: AlertStatus;
  message: string;
  details: Record<string, unknown>;
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

export interface AlertCounts {
  CRITICAL: number;
  MAJOR: number;
  MINOR: number;
}

/**
 * Specific yield: kWh produced per kWp installed.
 *
 * This is the headline comparison metric rather than PR%, because it needs no
 * irradiance data. PR% requires a Solcast baseline that is not configured yet,
 * so a PR-led dashboard would read "no data" on every tile. Specific yield is
 * computable from telemetry alone and is directly comparable between sites in
 * the same climate.
 */
export function specificYield(
  dailyYieldKwh: string | null,
  installedKwp: string | null,
): number | null {
  const y = num(dailyYieldKwh);
  const k = num(installedKwp);
  if (y === null || k === null || k <= 0) return null;
  return y / k;
}


export interface EnergyBucket {
  period: string;
  produced_kwh: string | null;
  /** Devices that contributed. A bucket from 2 of 6 inverters is not
   *  comparable with a complete one. */
  device_count: number;
  /** Readings behind the bucket. A day with 4 samples is not a full day. */
  sample_count: number;
}

export interface EnergyHistory {
  granularity: "day" | "month" | "year";
  start: string;
  end: string;
  buckets: EnergyBucket[];
  total_produced_kwh: string | null;
}

export interface PanelReading {
  serial_number: string;
  mppt_index: number;
  string_index: number;
  /** "B-4" for a microinverter panel, "MPPT 1 / S2" for a string. */
  label: string;
  produced_kwh: string | null;
  avg_power_kw: string | null;
  /** Signed deviation from the peer median, in percent. */
  deviation_pct: string | null;
  is_anomalous: boolean;
}

export interface PanelArray {
  on_date: string;
  panels: PanelReading[];
  anomaly_count: number;
  variance_threshold_pct: string;
  /** False means this hardware publishes no per-panel data at all — which is
   *  different from every panel reading zero. */
  has_panel_data: boolean;
}
