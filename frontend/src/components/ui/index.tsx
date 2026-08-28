/**
 * Shared UI primitives.
 *
 * Deliberately small and in one file: these are used on every page, and the
 * whole point is that a status dot or a stat tile looks identical whether it is
 * on the map, the fleet table, or a store page. Splitting them across seven
 * files makes it easier for them to drift apart than to stay consistent.
 */

import type { ReactNode } from "react";

import { STATUS_STYLES, type PRStatus } from "@/lib/pr-status";

// --------------------------------------------------------------------------
// Card
// --------------------------------------------------------------------------

export function Card({
  children,
  className = "",
  padded = true,
}: {
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <section className={`card ${padded ? "p-4" : ""} ${className}`}>{children}</section>
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-3 flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-content">{title}</h2>
        {subtitle && <p className="mt-0.5 text-2xs text-content-muted">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

// --------------------------------------------------------------------------
// Status
// --------------------------------------------------------------------------

/**
 * Status indicator.
 *
 * Carries a glyph as well as a colour. Red/green is the pair operators most
 * need to tell apart and exactly the pair red-green colour blindness collapses,
 * which affects roughly 8% of men — a coloured dot alone is not an accessible
 * encoding for the single most important signal in the product.
 */
export function StatusDot({
  status,
  withLabel = false,
  size = "md",
}: {
  status: PRStatus;
  withLabel?: boolean;
  size?: "sm" | "md";
}) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.UNKNOWN;
  const dim = size === "sm" ? "text-[10px]" : "text-xs";

  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span aria-hidden className={`${dim} leading-none`} style={{ color: style.color }}>
        {style.glyph}
      </span>
      {withLabel ? (
        <span className="text-xs text-content">{style.label}</span>
      ) : (
        <span className="sr-only">{style.label}</span>
      )}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "crit" | "accent";
}) {
  const tones: Record<string, string> = {
    neutral: "bg-surface-3 text-content-muted border-line",
    ok: "bg-status-ok/10 text-status-ok border-status-ok/30",
    warn: "bg-status-warn/10 text-status-warn border-status-warn/30",
    crit: "bg-status-crit/10 text-status-crit border-status-crit/30",
    accent: "bg-accent/10 text-accent-bright border-accent/30",
  };
  return (
    <span
      className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-2xs font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

// --------------------------------------------------------------------------
// Stat tile
// --------------------------------------------------------------------------

/**
 * One headline number.
 *
 * `coverage` exists because of a real trap in this dataset: the map can only
 * plot 118 of 153 stores, so a figure derived from the map understates the
 * fleet by 23%. Every tile states which population it counted rather than
 * leaving the reader to assume it covered everything.
 */
export function StatTile({
  label,
  value,
  unit,
  coverage,
  tone = "default",
  hint,
}: {
  label: string;
  value: string | number | null;
  unit?: string;
  coverage?: string;
  tone?: "default" | "ok" | "warn" | "crit" | "accent";
  hint?: string;
}) {
  const tones: Record<string, string> = {
    default: "text-content",
    ok: "text-status-ok",
    warn: "text-status-warn",
    crit: "text-status-crit",
    accent: "text-accent-bright",
  };

  return (
    <div className="card card-hover p-3.5" title={hint}>
      <div className="text-2xs uppercase tracking-wide text-content-muted">{label}</div>
      <div className={`stat-value mt-1.5 text-2xl font-semibold leading-none ${tones[tone]}`}>
        {value === null ? (
          <span className="text-lg text-content-faint">no data</span>
        ) : (
          <>
            {value}
            {unit && (
              <span className="ml-1 text-xs font-normal text-content-muted">{unit}</span>
            )}
          </>
        )}
      </div>
      {coverage && <div className="mt-1.5 text-2xs text-content-faint">{coverage}</div>}
    </div>
  );
}

// --------------------------------------------------------------------------
// States
// --------------------------------------------------------------------------

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <div className="mb-3 grid h-12 w-12 place-items-center rounded-xl border border-line bg-surface-3 text-content-faint">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M4 7h16M4 12h10M4 17h7"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
      </div>
      <p className="text-sm font-medium text-content">{title}</p>
      {detail && <p className="mt-1 max-w-sm text-xs text-content-muted">{detail}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-surface-3 ${className}`} />;
}

/**
 * Banner for anything the reader must not mistake for measured data.
 *
 * The fleet currently runs on generated telemetry, and a dashboard that shows
 * invented numbers without saying so is worse than an empty one — it looks
 * right, so nobody checks.
 */
export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn";
  children: ReactNode;
}) {
  const tones = {
    info: "border-status-info/30 bg-status-info/10 text-status-info",
    warn: "border-status-warn/30 bg-status-warn/10 text-status-warn",
  };
  return (
    <div
      className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${tones[tone]}`}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        className="mt-0.5 shrink-0"
        aria-hidden
      >
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.7" />
        <path d="M12 8v5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
        <circle cx="12" cy="16.2" r="0.9" fill="currentColor" />
      </svg>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

export function fmt(
  value: number | null | undefined,
  digits = 0,
  fallback = "—",
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return fallback;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Compact form for large energy figures: 13,159 kWh reads better as 13.2 MWh. */
export function fmtEnergy(kwh: number | null | undefined): { value: string; unit: string } {
  if (kwh === null || kwh === undefined || !Number.isFinite(kwh)) {
    return { value: "—", unit: "" };
  }
  if (Math.abs(kwh) >= 1_000_000) return { value: fmt(kwh / 1_000_000, 2), unit: "GWh" };
  if (Math.abs(kwh) >= 1_000) return { value: fmt(kwh / 1_000, 2), unit: "MWh" };
  return { value: fmt(kwh, 1), unit: "kWh" };
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
