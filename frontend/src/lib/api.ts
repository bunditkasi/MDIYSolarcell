/**
 * Backend client.
 *
 * One module owns the base URL, the error shape, and the auth header, so
 * switching hosts or turning on SSO in Phase 2 is a change here and nowhere
 * else.
 */

import { generateMockFleet } from "@/lib/mock-data";
import type {
  AlertCounts,
  AlertItem,
  DashboardSummary,
  DeviceItem,
  EnergyHistory,
  FleetResponse,
  MapResponse,
  PanelArray,
  Paged,
  StoreItem,
} from "@/types/store";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...init?.headers,
      // PHASE 2: attach the SSO bearer token here. Every endpoint already
      // depends on get_current_user server-side, so this is the only client
      // change needed when real authentication is switched on.
    },
    cache: "no-store",
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Not JSON; the status text is the best message available.
    }
    throw new ApiError(detail, response.status);
  }

  return (await response.json()) as T;
}

function qs(params: Record<string, string | number | boolean | undefined | string[]>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, item);
    } else {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// --------------------------------------------------------------------------
// Stores
// --------------------------------------------------------------------------

export interface MapPinQuery {
  search?: string;
  regions?: string[];
  bbox?: { minLat: number; maxLat: number; minLng: number; maxLng: number };
}

export async function fetchMapPins(query: MapPinQuery = {}): Promise<MapResponse> {
  return request<MapResponse>(
    `/stores/map-pins${qs({
      search: query.search,
      region: query.regions,
      min_lat: query.bbox?.minLat,
      max_lat: query.bbox?.maxLat,
      min_lng: query.bbox?.minLng,
      max_lng: query.bbox?.maxLng,
    })}`,
  );
}

/**
 * Rows for the fleet table.
 *
 * A different endpoint from the map feed, not a filter on it: this one returns
 * branches with no coordinates too.
 */
export async function fetchFleet(query: { search?: string } = {}): Promise<FleetResponse> {
  return request<FleetResponse>(`/stores/fleet${qs({ search: query.search })}`);
}

export async function fetchStores(params: {
  search?: string;
  regions?: string[];
  sortBy?: string;
  descending?: boolean;
  limit?: number;
  offset?: number;
}): Promise<Paged<StoreItem>> {
  return request<Paged<StoreItem>>(
    `/stores${qs({
      search: params.search,
      region: params.regions,
      sort_by: params.sortBy,
      descending: params.descending,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
    })}`,
  );
}

export async function fetchStore(storeId: string): Promise<StoreItem> {
  return request<StoreItem>(`/stores/${storeId}`);
}

export async function fetchStoreDevices(storeId: string): Promise<DeviceItem[]> {
  return request<DeviceItem[]>(`/stores/${storeId}/devices`);
}

export async function fetchEnergyHistory(
  storeId: string,
  params: { granularity: "day" | "month" | "year"; start?: string; end?: string },
): Promise<EnergyHistory> {
  return request<EnergyHistory>(
    `/stores/${storeId}/energy${qs({
      granularity: params.granularity,
      start: params.start,
      end: params.end,
    })}`,
  );
}

export async function fetchPanelArray(
  storeId: string,
  onDate?: string,
): Promise<PanelArray> {
  return request<PanelArray>(`/stores/${storeId}/array${qs({ on_date: onDate })}`);
}

// --------------------------------------------------------------------------
// Alerts
// --------------------------------------------------------------------------

export async function fetchAlerts(params: {
  severities?: string[];
  statuses?: string[];
  alertTypes?: string[];
  storeId?: string;
  search?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<Paged<AlertItem>> {
  return request<Paged<AlertItem>>(
    `/alerts${qs({
      severity: params.severities,
      status: params.statuses,
      alert_type: params.alertTypes,
      store_id: params.storeId,
      search: params.search,
      limit: params.limit ?? 100,
      offset: params.offset ?? 0,
    })}`,
  );
}

export async function fetchAlertCounts(): Promise<AlertCounts> {
  return request<AlertCounts>("/alerts/counts");
}

// --------------------------------------------------------------------------
// Dashboard
// --------------------------------------------------------------------------

export async function fetchDashboardSummary(): Promise<DashboardSummary> {
  return request<DashboardSummary>("/dashboard/summary");
}

// --------------------------------------------------------------------------
// Fleet loading with mock fallback
// --------------------------------------------------------------------------

/**
 * Load the fleet.
 *
 * Generated data is only ever substituted when it was ASKED for — the
 * `?mock=1` switch or NEXT_PUBLIC_USE_MOCK_DATA — or when a development build
 * cannot reach its backend, which is the case the generator was written for.
 *
 * A production build never fabricates. `process.env.NODE_ENV` is inlined at
 * build time, so on a hosted deployment the generator is not merely skipped but
 * absent from the bundle. A map quietly showing 200 invented MR.DIY sites is
 * worse than an empty one: the banner saying so does not survive a screenshot,
 * and nothing about the pins themselves looks wrong.
 */
export async function loadFleet(
  useMock: boolean,
): Promise<{ data: MapResponse | null; source: "api" | "mock"; error?: string }> {
  if (useMock) return { data: generateMockFleet(), source: "mock" };

  try {
    return { data: await fetchMapPins(), source: "api" };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    if (process.env.NODE_ENV === "development") {
      return { data: generateMockFleet(), source: "mock", error: message };
    }
    return { data: null, source: "api", error: message };
  }
}
