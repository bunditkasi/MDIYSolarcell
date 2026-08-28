/**
 * Map pin status — the frontend half of the classification rule.
 *
 * Mirrors `backend/app/domain/status.py`. The backend is authoritative: it
 * sends `pr_status` already computed on every pin, and this module only decides
 * how to DRAW it. The classification below exists for mock data and for
 * client-side re-evaluation, and it reads its threshold from the API response
 * rather than hard-coding 75 — the number lives in one place, the backend's
 * settings, so the two cannot drift.
 *
 * Specification section 3:
 *   GREEN  = PR >= 75%, inverter online
 *   YELLOW = PR < 75%, or a string anomaly
 *   RED    = inverter offline / disconnected for more than 15 minutes
 *
 * With no Solcast key configured, PR% cannot be computed for any branch, which
 * would leave every pin UNKNOWN and the map unable to tell a healthy site from
 * a failing one. The fallback is specific yield (kWh/kWp) measured against the
 * fleet median for the same day — no irradiance data required, and it
 * self-calibrates for time of day and weather because every branch in the
 * comparison sits under the same sky.
 */

export const PR_STATUSES = ["GREEN", "YELLOW", "RED", "UNKNOWN"] as const;
export type PRStatus = (typeof PR_STATUSES)[number];

export interface StatusStyle {
  /** Fill colour used by the MapLibre circle layer. */
  color: string;
  /** Ring colour, for contrast against both light and dark basemap areas. */
  stroke: string;
  radius: number;
  label: string;
  /**
   * Shape cue shown in the legend and, for RED, on the map itself.
   *
   * Colour alone is not an accessible encoding: red-green colour blindness
   * affects roughly 8% of men, and green/red are exactly the two states an
   * operator most needs to tell apart. Size and the "!" glyph give a second,
   * non-colour channel.
   */
  glyph: string;
  description: string;
}

export const STATUS_STYLES: Record<PRStatus, StatusStyle> = {
  GREEN: {
    color: "#16a34a",
    stroke: "#ffffff",
    radius: 6,
    label: "Normal",
    glyph: "●",
    description: "At or above threshold, inverter online",
  },
  YELLOW: {
    color: "#f97316",
    stroke: "#ffffff",
    radius: 7.5,
    label: "Warning",
    glyph: "◆",
    description: "Below threshold, or a string anomaly detected",
  },
  RED: {
    color: "#dc2626",
    stroke: "#ffffff",
    radius: 9.5,
    label: "Critical",
    glyph: "▲",
    description: "Inverter offline, or a critical alert is open",
  },
  UNKNOWN: {
    color: "#64748b",
    stroke: "#ffffff",
    radius: 5,
    label: "No data",
    glyph: "○",
    description: "Neither PR nor peer-relative yield can be computed",
  },
};

/** Order used for legends and summary rows: worst first. */
export const STATUS_DISPLAY_ORDER: PRStatus[] = ["RED", "YELLOW", "GREEN", "UNKNOWN"];

export interface ClassifyInput {
  performanceRatio: number | null;
  isOnline: boolean;
  hasStringAnomaly: boolean;
  hasCriticalAlert: boolean;
  greenThreshold: number;
  /** Specific yield as a percentage of the fleet median for the same day. */
  yieldVsPeersPct?: number | null;
  /** Threshold for the above, from the API's `yield_green_threshold_pct`. */
  yieldGreenThreshold?: number | null;
}

/** Which measure decided a pin's colour, so the UI can show its evidence. */
export type StatusBasis = "pr" | "yield" | "offline" | "anomaly" | "none";

export function statusBasis(input: ClassifyInput): StatusBasis {
  if (!input.isOnline || input.hasCriticalAlert) return "offline";
  if (input.hasStringAnomaly) return "anomaly";
  if (input.performanceRatio !== null) return "pr";
  if (
    input.yieldVsPeersPct !== null &&
    input.yieldVsPeersPct !== undefined &&
    input.yieldGreenThreshold !== null &&
    input.yieldGreenThreshold !== undefined
  ) {
    return "yield";
  }
  return "none";
}

/**
 * Classify a site. Order matters and matches the backend exactly.
 *
 * RED is checked first: an offline site reports no fresh telemetry, so any PR
 * value still attached to it is stale. Letting a good yesterday paint the pin
 * green would hide a dead site.
 */
export function classifyPRStatus({
  performanceRatio,
  isOnline,
  hasStringAnomaly,
  hasCriticalAlert,
  greenThreshold,
  yieldVsPeersPct,
  yieldGreenThreshold,
}: ClassifyInput): PRStatus {
  if (!isOnline || hasCriticalAlert) return "RED";
  if (hasStringAnomaly) return "YELLOW";
  if (performanceRatio !== null) {
    return performanceRatio >= greenThreshold ? "GREEN" : "YELLOW";
  }
  if (
    yieldVsPeersPct !== null &&
    yieldVsPeersPct !== undefined &&
    yieldGreenThreshold !== null &&
    yieldGreenThreshold !== undefined
  ) {
    return yieldVsPeersPct >= yieldGreenThreshold ? "GREEN" : "YELLOW";
  }
  return "UNKNOWN";
}

/**
 * MapLibre data-driven paint expression mapping the `pr_status` feature
 * property onto a colour.
 *
 * Building this from STATUS_STYLES rather than writing the literals inline
 * keeps the map, the legend and any chart using the same source of truth.
 */
export function statusColorExpression(): unknown[] {
  const expression: unknown[] = ["match", ["get", "pr_status"]];
  for (const status of PR_STATUSES) {
    expression.push(status, STATUS_STYLES[status].color);
  }
  expression.push(STATUS_STYLES.UNKNOWN.color); // fallback
  return expression;
}

export function statusRadiusExpression(): unknown[] {
  const expression: unknown[] = ["match", ["get", "pr_status"]];
  for (const status of PR_STATUSES) {
    expression.push(status, STATUS_STYLES[status].radius);
  }
  expression.push(STATUS_STYLES.UNKNOWN.radius);
  return expression;
}
