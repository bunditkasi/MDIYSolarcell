"use client";

import { STATUS_DISPLAY_ORDER, STATUS_STYLES, type PRStatus } from "@/lib/pr-status";

export interface MapLegendProps {
  counts: Record<PRStatus, number>;
  greenThreshold: number;
  offlineAfterMinutes: number;
  /** Specific-yield threshold, as a percentage of the fleet median. */
  yieldThreshold: number;
  /** True when no branch has a computable PR, so yield is deciding every pin. */
  usingYieldFallback: boolean;
  /** Active filter; null means "show everything". */
  selected: PRStatus | null;
  onSelect: (status: PRStatus | null) => void;
}

/**
 * Legend and status filter.
 *
 * A legend is not decoration here. The pins encode state in colour, and colour
 * alone is not readable for everyone — this panel is where the glyph, the
 * colour and the meaning are stated together. It doubles as the filter control
 * so the two can never disagree about what a colour means.
 */
export default function MapLegend({
  counts,
  greenThreshold,
  offlineAfterMinutes,
  yieldThreshold,
  usingYieldFallback,
  selected,
  onSelect,
}: MapLegendProps) {
  const total = STATUS_DISPLAY_ORDER.reduce((sum, status) => sum + (counts[status] ?? 0), 0);

  return (
    <div className="pointer-events-auto card p-3 shadow-xl backdrop-blur">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-content-muted">
          Site status
        </h2>
        <span className="text-2xs tabular-nums text-content-faint">{total} sites</span>
      </div>

      <ul className="mt-2 space-y-0.5">
        {STATUS_DISPLAY_ORDER.map((status) => {
          const style = STATUS_STYLES[status];
          const count = counts[status] ?? 0;
          const isSelected = selected === status;

          return (
            <li key={status}>
              <button
                type="button"
                onClick={() => onSelect(isSelected ? null : status)}
                aria-pressed={isSelected}
                title={style.description}
                className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left transition ${
                  isSelected ? "bg-surface-3 ring-1 ring-line-strong" : "hover:bg-surface-3"
                }`}
              >
                <span
                  aria-hidden
                  className="w-4 text-center text-sm leading-none"
                  style={{ color: style.color }}
                >
                  {style.glyph}
                </span>
                <span className="flex-1 text-xs font-medium text-content">
                  {style.label}
                </span>
                <span className="text-xs tabular-nums text-content-muted">{count}</span>
              </button>
            </li>
          );
        })}
      </ul>

      {selected && (
        <button
          type="button"
          onClick={() => onSelect(null)}
          className="mt-2 w-full rounded-lg border border-line px-2 py-1 text-2xs text-content-muted transition hover:bg-surface-3 hover:text-content"
        >
          Clear filter
        </button>
      )}

      <p className="mt-3 border-t border-line pt-2 text-2xs leading-relaxed text-content-faint">
        {/* Naming the wrong measure is worse than naming none: someone acting on
            “PR below 75%” would go looking for a soiling or shading problem,
            when what the pin actually says is “behind its peers today”. */}
        {usingYieldFallback ? (
          <>
            Green at or above {yieldThreshold}% of the fleet’s median kWh/kWp today.
            Performance Ratio needs an irradiance baseline that is not configured.
          </>
        ) : (
          <>Green at PR ≥ {greenThreshold}%.</>
        )}{" "}
        Critical after {offlineAfterMinutes} min without a reading — longer for
        vendors polled less often. Thresholds come from the API, not the browser.
      </p>
    </div>
  );
}
