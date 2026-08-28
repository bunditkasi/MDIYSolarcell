"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  Badge,
  Card,
  EmptyState,
  Notice,
  Skeleton,
  StatTile,
  fmt,
  relativeTime,
} from "@/components/ui";
import { fetchAlertCounts, fetchAlerts } from "@/lib/api";
import type { AlertCounts, AlertItem, AlertSeverity } from "@/types/store";

const SEVERITIES: AlertSeverity[] = ["CRITICAL", "MAJOR", "MINOR"];

const TYPE_LABEL: Record<string, string> = {
  STRING_VARIANCE: "String / panel anomaly",
  DEVICE_OFFLINE: "Device offline",
  LOW_PR: "Low performance ratio",
  DATA_GAP: "Data gap",
  ADAPTER_FAILURE: "Ingestion failure",
};

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertItem[] | null>(null);
  const [counts, setCounts] = useState<AlertCounts | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [severity, setSeverity] = useState<AlertSeverity | null>(null);
  const [search, setSearch] = useState("");
  const [showResolved, setShowResolved] = useState(false);

  const load = useCallback(() => {
    void fetchAlerts({
      severities: severity ? [severity] : undefined,
      statuses: showResolved ? ["OPEN", "ACKNOWLEDGED", "RESOLVED"] : undefined,
      search: search.trim() || undefined,
      limit: 200,
    })
      .then((page) => {
        setAlerts(page.items);
        setTotal(page.total);
        setError(null);
      })
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "Backend unreachable"),
      );

    void fetchAlertCounts().then(setCounts).catch(() => undefined);
  }, [severity, search, showResolved]);

  useEffect(() => {
    // Debounced so typing in the search box does not fire a request per keystroke.
    const timer = setTimeout(load, search ? 300 : 0);
    return () => clearTimeout(timer);
  }, [load, search]);

  return (
    <main className="mx-auto max-w-[1600px] animate-fade-in px-4 py-5">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Alerts</h1>
          <p className="mt-0.5 text-xs text-content-muted">
            {showResolved ? "All alerts" : "Open and acknowledged"} · {total} shown
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-content-muted transition hover:border-line-strong hover:text-content"
        >
          Refresh
        </button>
      </div>

      {error && (
        <div className="mb-4">
          <Notice tone="warn">Backend unreachable ({error}).</Notice>
        </div>
      )}

      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <StatTile label="Critical" value={fmt(counts?.CRITICAL ?? null)} tone="crit" />
        <StatTile label="Major" value={fmt(counts?.MAJOR ?? null)} tone="warn" />
        <StatTile label="Minor" value={fmt(counts?.MINOR ?? null)} />
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Chip label="All" active={severity === null} onClick={() => setSeverity(null)} />
        {SEVERITIES.map((s) => (
          <Chip
            key={s}
            label={s}
            active={severity === s}
            onClick={() => setSeverity(severity === s ? null : s)}
          />
        ))}

        <label className="ml-2 flex cursor-pointer items-center gap-2 text-xs text-content-muted">
          <input
            type="checkbox"
            checked={showResolved}
            onChange={(e) => setShowResolved(e.target.checked)}
            className="h-3.5 w-3.5 accent-[var(--accent)]"
          />
          Include resolved
        </label>

        <div className="ml-auto">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search branch or message"
            className="w-64 rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-content placeholder:text-content-faint focus:border-accent focus:outline-none"
          />
        </div>
      </div>

      <Card padded={false} className="overflow-hidden">
        {alerts === null ? (
          <div className="space-y-px p-3">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        ) : alerts.length === 0 ? (
          <EmptyState
            title="Nothing to dispatch"
            detail="No alert matches the current filters."
          />
        ) : (
          <ul className="divide-y divide-line">
            {alerts.map((alert) => (
              <li key={alert.alert_id}>
                <Link
                  href={`/stores/${alert.store_code}`}
                  className="flex items-start gap-3 px-4 py-3 transition hover:bg-surface-3"
                >
                  <div className="w-20 shrink-0 pt-0.5">
                    <Badge
                      tone={
                        alert.severity === "CRITICAL"
                          ? "crit"
                          : alert.severity === "MAJOR"
                            ? "warn"
                            : "neutral"
                      }
                    >
                      {alert.severity}
                    </Badge>
                  </div>

                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="text-xs font-medium text-accent-bright">
                        {alert.store_code}
                      </span>
                      <span className="truncate text-xs text-content">
                        {alert.store_name}
                      </span>
                      {alert.province && (
                        <span className="text-2xs text-content-faint">
                          · {alert.province}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-2xs text-content-muted">{alert.message}</p>
                    <div className="mt-1 flex items-center gap-2">
                      <span className="text-2xs text-content-faint">
                        {TYPE_LABEL[alert.alert_type] ?? alert.alert_type}
                      </span>
                      {alert.status !== "OPEN" && (
                        <Badge tone="neutral">{alert.status}</Badge>
                      )}
                    </div>
                  </div>

                  <div className="shrink-0 text-right">
                    <div className="text-2xs text-content-muted">
                      {relativeTime(alert.created_at)}
                    </div>
                    <div className="mt-0.5 text-2xs text-content-faint">
                      {new Date(alert.created_at).toLocaleString("en-GB", {
                        day: "2-digit",
                        month: "short",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </div>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </main>
  );
}

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-lg border px-2.5 py-1.5 text-xs transition ${
        active
          ? "border-accent bg-accent/15 text-content"
          : "border-line bg-surface-2 text-content-muted hover:border-line-strong hover:text-content"
      }`}
    >
      {label}
    </button>
  );
}
