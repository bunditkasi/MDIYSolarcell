/**
 * Deterministic mock fleet — 200 stores across Thailand.
 *
 * Lets the map be developed and demonstrated before the backend has telemetry.
 * Enable with NEXT_PUBLIC_USE_MOCK_DATA=true, or `?mock=1` on the map page.
 *
 * DETERMINISTIC ON PURPOSE. A seeded generator means the same 200 sites with
 * the same statuses appear on every render and every reload. With Math.random()
 * the fleet would reshuffle on each React re-render, pins would jump, and it
 * would be impossible to tell a real rendering bug from noise.
 *
 * Positions are scattered around real Thai population centres rather than
 * uniformly across the bounding box, so clustering behaviour under zoom
 * resembles the real fleet instead of an even grid.
 */

import type { PRStatus } from "@/lib/pr-status";
import type { MapResponse, StorePin } from "@/types/store";

/** mulberry32 — small, fast, seeded PRNG. */
function makeRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Hub {
  name: string;
  region: string;
  lat: number;
  lng: number;
  /** Rough spread in degrees; bigger hubs sprawl further. */
  spread: number;
  /** Relative share of the fleet. */
  weight: number;
}

const HUBS: Hub[] = [
  { name: "Bangkok", region: "Central", lat: 13.7563, lng: 100.5018, spread: 0.35, weight: 34 },
  { name: "Samut Prakan", region: "Central", lat: 13.5991, lng: 100.5998, spread: 0.18, weight: 10 },
  { name: "Nonthaburi", region: "Central", lat: 13.8591, lng: 100.5217, spread: 0.15, weight: 8 },
  { name: "Chon Buri", region: "East", lat: 13.3611, lng: 100.9847, spread: 0.3, weight: 12 },
  { name: "Rayong", region: "East", lat: 12.6814, lng: 101.2816, spread: 0.25, weight: 7 },
  { name: "Chiang Mai", region: "North", lat: 18.7883, lng: 98.9853, spread: 0.35, weight: 12 },
  { name: "Phitsanulok", region: "North", lat: 16.8211, lng: 100.2659, spread: 0.3, weight: 6 },
  { name: "Khon Kaen", region: "Northeast", lat: 16.4419, lng: 102.836, spread: 0.35, weight: 11 },
  { name: "Nakhon Ratchasima", region: "Northeast", lat: 14.9799, lng: 102.0977, spread: 0.4, weight: 12 },
  { name: "Udon Thani", region: "Northeast", lat: 17.4138, lng: 102.7877, spread: 0.3, weight: 8 },
  { name: "Ubon Ratchathani", region: "Northeast", lat: 15.2448, lng: 104.8473, spread: 0.3, weight: 6 },
  { name: "Nakhon Si Thammarat", region: "South", lat: 8.4304, lng: 99.9631, spread: 0.3, weight: 8 },
  { name: "Surat Thani", region: "South", lat: 9.14, lng: 99.3331, spread: 0.3, weight: 7 },
  { name: "Songkhla", region: "South", lat: 7.1897, lng: 100.5951, spread: 0.28, weight: 7 },
  { name: "Phuket", region: "South", lat: 7.8804, lng: 98.3923, spread: 0.15, weight: 6 },
  { name: "Nakhon Pathom", region: "Central", lat: 13.8199, lng: 100.0621, spread: 0.25, weight: 6 },
];

const LETTERS = "ABCDEFGHIJKLMNPQRSTUVWXYZ";

function pickHub(random: () => number): Hub {
  const total = HUBS.reduce((sum, hub) => sum + hub.weight, 0);
  let ticket = random() * total;
  for (const hub of HUBS) {
    ticket -= hub.weight;
    if (ticket <= 0) return hub;
  }
  return HUBS[0]!;
}

/** Box-Muller: clusters points near the hub centre instead of a uniform square. */
function gaussian(random: () => number): number {
  const u = Math.max(random(), Number.EPSILON);
  const v = random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function storeCode(index: number, random: () => number): string {
  const pick = () => LETTERS[Math.floor(random() * LETTERS.length)]!;
  return `P${pick()}${pick()}${pick()}`;
}

export interface MockOptions {
  count?: number;
  seed?: number;
  greenThreshold?: number;
}

export function generateMockFleet({
  count = 200,
  seed = 20260827,
  greenThreshold = 75,
}: MockOptions = {}): MapResponse {
  const random = makeRandom(seed);
  const stores: StorePin[] = [];
  const usedCodes = new Set<string>();

  for (let i = 0; i < count; i += 1) {
    const hub = pickHub(random);

    const lat = hub.lat + gaussian(random) * hub.spread;
    const lng = hub.lng + gaussian(random) * hub.spread;

    let code = storeCode(i, random);
    while (usedCodes.has(code)) code = storeCode(i, random);
    usedCodes.add(code);

    const installedKwp = Math.round((20 + random() * 80) * 100) / 100;

    // Fleet health mix chosen to look like a real estate: mostly healthy, a
    // meaningful warning tail, a few genuine outages, and some sites not yet
    // reporting irradiance.
    const roll = random();
    let status: PRStatus;
    if (roll < 0.68) status = "GREEN";
    else if (roll < 0.87) status = "YELLOW";
    else if (roll < 0.95) status = "RED";
    else status = "UNKNOWN";

    const isOnline = status !== "RED";
    const hasStringAnomaly = status === "YELLOW" && random() < 0.45;

    let performanceRatio: number | null;
    if (status === "UNKNOWN") {
      performanceRatio = null;
    } else if (status === "GREEN") {
      performanceRatio = greenThreshold + random() * (98 - greenThreshold);
    } else if (status === "YELLOW" && !hasStringAnomaly) {
      performanceRatio = 45 + random() * (greenThreshold - 45);
    } else if (status === "YELLOW") {
      // Anomaly present but headline PR still healthy — the exact case that a
      // PR-only view would miss.
      performanceRatio = greenThreshold + random() * 15;
    } else {
      performanceRatio = null; // offline: no fresh reading
    }

    // Rough diurnal shape so "live kW" is not implausible for the time of day.
    const hour = new Date().getHours() + new Date().getMinutes() / 60;
    const daylight = Math.max(0, Math.sin(((hour - 6) / 12) * Math.PI));
    const activePower = isOnline
      ? Math.round(installedKwp * daylight * (0.55 + random() * 0.4) * 1000) / 1000
      : 0;
    const dailyYield = isOnline
      ? Math.round(installedKwp * (2.4 + random() * 2.0) * (hour / 24) * 1000) / 1000
      : 0;

    const openAlerts =
      status === "RED"
        ? 1 + Math.floor(random() * 3)
        : hasStringAnomaly
          ? 1
          : status === "YELLOW"
            ? Math.floor(random() * 2)
            : 0;

    const lastSeenMinutesAgo = isOnline ? Math.floor(random() * 10) : 20 + Math.floor(random() * 600);

    stores.push({
      store_id: `mock-${String(i).padStart(4, "0")}`,
      store_code: code,
      store_name: `MR.DIY ${hub.name} ${i + 1}`,
      region: hub.region,
      province: hub.name,
      lat: lat.toFixed(6),
      lng: lng.toFixed(6),
      installed_kwp: installedKwp.toFixed(2),
      pr_status: status,
      performance_ratio: performanceRatio === null ? null : performanceRatio.toFixed(2),
      specific_yield_kwh_per_kwp: (dailyYield / installedKwp).toFixed(3),
      // Mock branches are generated around a healthy median, so the ratio is
      // expressed against that rather than recomputed across the sample.
      yield_vs_peers_pct: ((dailyYield / installedKwp / 4.2) * 100).toFixed(1),
      active_power_kw: activePower.toFixed(3),
      daily_yield_kwh: dailyYield.toFixed(3),
      last_seen_at: new Date(Date.now() - lastSeenMinutesAgo * 60_000).toISOString(),
      is_online: isOnline,
      has_string_anomaly: hasStringAnomaly,
      open_alert_count: openAlerts,
      // Generated branches always carry a capacity and a position.
      is_incomplete: false,
      has_ever_reported: true,
      max_alert_severity:
        status === "RED" ? "CRITICAL" : openAlerts > 0 ? "MAJOR" : null,
    });
  }

  return {
    stores,
    thresholds: {
      pr_green_threshold: greenThreshold.toFixed(1),
      string_variance_threshold_pct: "10.0",
      device_offline_after_minutes: 15,
      yield_green_threshold_pct: "80.0",
    },
    stores_without_location: 0,
  };
}
