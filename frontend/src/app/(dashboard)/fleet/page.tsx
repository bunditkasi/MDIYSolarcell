"use client";

/**
 * Fleet list.
 *
 * Reads `/stores/fleet`, NOT the map feed. The distinction matters: the map
 * feed can only carry branches that have coordinates, and 13 of 163 do not.
 * Dropping them from a *list* would hide real sites producing real energy and
 * quietly understate every total taken from this page.
 *
 * Columns follow the operations view the team already works from — source,
 * status, name, grid date, capacity, battery, and the four energy figures —
 * with two deliberate departures. Status is the classified colour rather than
 * the vendor's raw `health_state_3`, and Last Sync is relative rather than a
 * raw ISO string, because "4 min ago" is the question being asked.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Card, EmptyState, Notice, Skeleton, StatusDot, fmt, relativeTime } from "@/components/ui";
import { fetchFleet } from "@/lib/api";
import { downloadCsv, stampedFilename, toCsv, type CsvColumn } from "@/lib/csv";
import { STATUS_DISPLAY_ORDER, STATUS_STYLES, type PRStatus } from "@/lib/pr-status";
import { num, type FleetRow } from "@/types/store";

type SortKey =
  | "store_code"
  | "source"
  | "commissioned_at"
  | "installed_kwp"
  | "battery"
  | "power"
  | "today"
  | "month"
  | "lifetime"
  | "last_sync";

/** Human label for a vendor key. Unreported branches are not "unknown vendor". */
function sourceLabel(source: string | null): string {
  if (!source) return "—";
  return source.toUpperCase();
}

function isoDate(value: string | null): string {
  return value ?? "—";
}

export default function FleetPage() {
  const [rows, setRows] = useState<FleetRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<PRStatus | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [sort, setSort] = useState<SortKey>("store_code");
  const [desc, setDesc] = useState(false);
  // Branches no vendor account has ever delivered a reading for are hidden by
  // default. They are not faults — they are waiting on API access or on
  // commissioning — and a dozen permanently blank rows train people to skim
  // past a list whose whole job is to be scanned. Counted and one click away,
  // never silently dropped.
  const [showPending, setShowPending] = useState(false);

  useEffect(() => {
    // The status filter can arrive from the Overview page's status breakdown.
    const initial = new URLSearchParams(window.location.search).get("status");
    if (initial && STATUS_DISPLAY_ORDER.includes(initial as PRStatus)) {
      setStatus(initial as PRStatus);
    }

    let cancelled = false;
    void fetchFleet()
      .then((data) => {
        if (!cancelled) setRows(data.rows);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Backend unreachable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** The rows the chips describe: everything currently eligible to be listed. */
  const pool = useMemo(
    () => (rows ?? []).filter((r) => showPending || r.has_ever_reported),
    [rows, showPending],
  );

  const statusCounts = useMemo(() => {
    const tally: Record<string, number> = { GREEN: 0, YELLOW: 0, RED: 0, UNKNOWN: 0 };
    for (const row of pool) tally[row.pr_status] = (tally[row.pr_status] ?? 0) + 1;
    return tally;
  }, [pool]);

  const sourceCounts = useMemo(() => {
    const tally = new Map<string, number>();
    for (const row of pool) {
      const key = row.source ?? "—";
      tally.set(key, (tally.get(key) ?? 0) + 1);
    }
    return [...tally.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [pool]);

  const pendingCount = useMemo(
    () => (rows ?? []).filter((r) => !r.has_ever_reported).length,
    [rows],
  );

  const visible = useMemo(() => {
    let list = rows ?? [];
    if (!showPending) list = list.filter((r) => r.has_ever_reported);
    if (status) list = list.filter((r) => r.pr_status === status);
    if (source) list = list.filter((r) => (r.source ?? "—") === source);

    const needle = search.trim().toLowerCase();
    if (needle) {
      list = list.filter(
        (r) =>
          r.store_code.toLowerCase().includes(needle) ||
          r.store_name.toLowerCase().includes(needle) ||
          (r.province ?? "").toLowerCase().includes(needle),
      );
    }

    // Missing values sort last in BOTH directions rather than pretending to be
    // zero — a branch with no reading is not a branch producing nothing.
    const value = (r: FleetRow): number | string => {
      switch (sort) {
        case "source":
          return r.source ?? "zzz";
        case "commissioned_at":
          return r.commissioned_at ?? "";
        case "installed_kwp":
          return num(r.installed_kwp) ?? -1;
        case "battery":
          return num(r.battery_capacity_kwh) ?? -1;
        case "power":
          return num(r.active_power_kw) ?? -1;
        case "today":
          return num(r.daily_yield_kwh) ?? -1;
        case "month":
          return num(r.monthly_yield_kwh) ?? -1;
        case "lifetime":
          return num(r.lifetime_yield_kwh) ?? -1;
        case "last_sync":
          return r.last_seen_at ? new Date(r.last_seen_at).getTime() : -1;
        default:
          return r.store_code;
      }
    };

    return [...list].sort((a, b) => {
      const av = value(a);
      const bv = value(b);
      const cmp =
        typeof av === "string" && typeof bv === "string"
          ? av.localeCompare(bv)
          : Number(av) - Number(bv);
      return desc ? -cmp : cmp;
    });
  }, [rows, status, source, search, sort, desc, showPending]);

  const toggleSort = (key: SortKey) => {
    if (sort === key) setDesc(!desc);
    else {
      setSort(key);
      // Numeric columns are almost always wanted biggest-first on first click.
      setDesc(key !== "store_code" && key !== "source");
    }
  };

  /** Export exactly what is on screen — same filter, same order. */
  const exportCsv = () => {
    const columns: CsvColumn<FleetRow>[] = [
      { header: "Source", value: (r) => r.source ?? "" },
      { header: "Status", value: (r) => STATUS_STYLES[r.pr_status].label },
      { header: "Branch Code", value: (r) => r.store_code },
      { header: "Site Name", value: (r) => r.store_name },
      { header: "Province", value: (r) => r.province ?? "" },
      { header: "Install/Grid Date", value: (r) => r.commissioned_at ?? "" },
      { header: "Capacity kWp", value: (r) => num(r.installed_kwp) },
      { header: "Battery kWh", value: (r) => num(r.battery_capacity_kwh) },
      { header: "Current kW", value: (r) => num(r.active_power_kw) },
      { header: "Today kWh", value: (r) => num(r.daily_yield_kwh) },
      { header: "Month kWh", value: (r) => num(r.monthly_yield_kwh) },
      { header: "Lifetime kWh", value: (r) => num(r.lifetime_yield_kwh) },
      { header: "Last Sync", value: (r) => r.last_seen_at ?? "" },
      { header: "Open Alerts", value: (r) => r.open_alert_count },
    ];
    downloadCsv(stampedFilename("mrdiy-solar-fleet"), toCsv(visible, columns));
  };

  const noBattery = (rows ?? []).every((r) => r.battery_capacity_kwh === null);

  return (
    <main className="mx-auto max-w-[1600px] animate-fade-in px-4 py-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Fleet</h1>
          <p className="mt-0.5 text-xs text-content-muted">
            {rows ? `${visible.length} of ${pool.length} branches shown` : "Loading…"}
            {rows && pendingCount > 0 && !showPending && (
              <>
                {" · "}
                <button
                  type="button"
                  onClick={() => setShowPending(true)}
                  className="underline decoration-dotted underline-offset-2 transition hover:text-accent-bright"
                  title="No vendor account has ever delivered a reading for these branches"
                >
                  {pendingCount} not connected yet
                </button>
              </>
            )}
            {rows && showPending && pendingCount > 0 && (
              <>
                {" · including "}
                <button
                  type="button"
                  onClick={() => setShowPending(false)}
                  className="underline decoration-dotted underline-offset-2 transition hover:text-accent-bright"
                >
                  {pendingCount} not connected — hide
                </button>
              </>
            )}
          </p>
        </div>

        <button
          type="button"
          onClick={exportCsv}
          disabled={!rows || visible.length === 0}
          className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs font-medium text-content transition hover:border-accent hover:text-accent-bright disabled:cursor-not-allowed disabled:opacity-40"
          title="Downloads the rows currently shown, in the current order"
        >
          Export CSV
        </button>
      </div>

      {error && (
        <div className="mb-4">
          <Notice tone="warn">Backend unreachable ({error}).</Notice>
        </div>
      )}

      {/* Status filter */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <FilterChip
          label="All status"
          count={pool.length}
          active={status === null}
          onClick={() => setStatus(null)}
        />
        {STATUS_DISPLAY_ORDER.map((s) => (
          <FilterChip
            key={s}
            label={STATUS_STYLES[s].label}
            glyph={STATUS_STYLES[s].glyph}
            color={STATUS_STYLES[s].color}
            count={statusCounts[s] ?? 0}
            active={status === s}
            onClick={() => setStatus(status === s ? null : s)}
          />
        ))}

        <div className="ml-auto">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search code, name or province"
            className="w-64 rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-content placeholder:text-content-faint focus:border-accent focus:outline-none"
          />
        </div>
      </div>

      {/* Source filter — the two vendor clouds behave differently enough that
          looking at one at a time is a real diagnostic step. */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <FilterChip
          label="All sources"
          count={pool.length}
          active={source === null}
          onClick={() => setSource(null)}
        />
        {sourceCounts.map(([key, count]) => (
          <FilterChip
            key={key}
            label={key === "—" ? "Not reporting" : sourceLabel(key)}
            count={count}
            active={source === key}
            onClick={() => setSource(source === key ? null : key)}
          />
        ))}
      </div>

      {noBattery && rows !== null && (
        <div className="mb-3">
          <Notice>
            No branch reports a battery capacity. The column is kept because the
            Atmoce API publishes the figure and will fill it in as soon as a site
            with storage is added.
          </Notice>
        </div>
      )}

      <Card padded={false} className="overflow-hidden">
        {rows === null ? (
          <div className="space-y-px p-3">
            {Array.from({ length: 10 }).map((_, i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            title="No branches match"
            detail="Try clearing the status or source filter, or the search box."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1240px] text-xs">
              <thead>
                <tr className="border-b border-line text-2xs uppercase tracking-wide text-content-muted">
                  <Th sortable active={sort === "source"} desc={desc} onClick={() => toggleSort("source")}>
                    Source
                  </Th>
                  <Th>Status</Th>
                  <Th sortable active={sort === "store_code"} desc={desc} onClick={() => toggleSort("store_code")}>
                    Site name
                  </Th>
                  <Th sortable active={sort === "commissioned_at"} desc={desc} onClick={() => toggleSort("commissioned_at")}>
                    Install / grid date
                  </Th>
                  <Th align="right" sortable active={sort === "installed_kwp"} desc={desc} onClick={() => toggleSort("installed_kwp")}>
                    Capacity kWp
                  </Th>
                  <Th align="right" sortable active={sort === "battery"} desc={desc} onClick={() => toggleSort("battery")}>
                    Battery kWh
                  </Th>
                  <Th align="right" sortable active={sort === "power"} desc={desc} onClick={() => toggleSort("power")}>
                    Current kW
                  </Th>
                  <Th align="right" sortable active={sort === "today"} desc={desc} onClick={() => toggleSort("today")}>
                    Today kWh
                  </Th>
                  <Th align="right" sortable active={sort === "month"} desc={desc} onClick={() => toggleSort("month")}>
                    Month kWh
                  </Th>
                  <Th align="right" sortable active={sort === "lifetime"} desc={desc} onClick={() => toggleSort("lifetime")}>
                    Lifetime kWh
                  </Th>
                  <Th align="right" sortable active={sort === "last_sync"} desc={desc} onClick={() => toggleSort("last_sync")}>
                    Last sync
                  </Th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => (
                  <tr
                    key={row.store_id}
                    className="border-b border-line/60 transition last:border-0 hover:bg-surface-3"
                  >
                    <td className="px-3 py-2">
                      <span className="text-[10px] font-medium uppercase tracking-wide text-content-muted">
                        {sourceLabel(row.source)}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <StatusDot status={row.pr_status} />
                    </td>
                    <td className="px-3 py-2">
                      <Link href={`/stores/${row.store_code}`} className="group block">
                        <span className="font-medium text-accent-bright group-hover:underline">
                          {row.store_code}
                        </span>
                        <span className="ml-2 text-content-muted">{row.store_name}</span>
                        {row.is_incomplete && (
                          <span
                            className="ml-2 rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent-bright"
                            title="Reported by the vendor but not yet in the roster — capacity or position is still missing."
                          >
                            New
                          </span>
                        )}
                        {!row.has_ever_reported && (
                          <span
                            className="ml-2 rounded border border-line px-1.5 py-0.5 text-[10px] text-content-faint"
                            title="No vendor account has ever delivered a reading for this branch. It is waiting on API access or commissioning, not failing."
                          >
                            Not connected
                          </span>
                        )}
                        {!row.has_location && (
                          <span
                            className="ml-2 rounded border border-line px-1.5 py-0.5 text-[10px] text-content-faint"
                            title="No coordinates recorded, so this branch cannot be drawn on the map. Its data is complete."
                          >
                            No map
                          </span>
                        )}
                      </Link>
                    </td>
                    <td className="num px-3 py-2 text-content-muted">
                      {isoDate(row.commissioned_at)}
                    </td>
                    <Num value={num(row.installed_kwp)} digits={2} hint="Capacity not recorded yet" />
                    <Num
                      value={num(row.battery_capacity_kwh)}
                      digits={1}
                      hint="No battery figure published for this branch"
                    />
                    <Num value={num(row.active_power_kw)} digits={2} hint="This vendor publishes no live power" />
                    <Num value={num(row.daily_yield_kwh)} digits={1} />
                    <Num value={num(row.monthly_yield_kwh)} digits={1} />
                    <Num value={num(row.lifetime_yield_kwh)} digits={0} />
                    <td
                      className={`px-3 py-2 text-right ${
                        row.is_online ? "text-content-muted" : "text-status-crit"
                      }`}
                      title={row.last_seen_at ?? "no reading recorded"}
                    >
                      {relativeTime(row.last_seen_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </main>
  );
}

/**
 * A numeric cell.
 *
 * An absent value is a dash, never a zero. "The vendor does not publish live
 * power for this branch" and "this branch is producing 0 kW" are opposite
 * claims, and a 0 in this column would send someone to site.
 */
function Num({
  value,
  digits,
  hint,
}: {
  value: number | null;
  digits: number;
  hint?: string;
}) {
  return (
    <td className="num px-3 py-2 text-right text-content">
      {value === null ? (
        <span className="text-content-faint" title={hint}>
          —
        </span>
      ) : (
        fmt(value, digits)
      )}
    </td>
  );
}

function FilterChip({
  label,
  count,
  active,
  onClick,
  glyph,
  color,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  glyph?: string;
  color?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs transition ${
        active
          ? "border-accent bg-accent/15 text-content"
          : "border-line bg-surface-2 text-content-muted hover:border-line-strong hover:text-content"
      }`}
    >
      {glyph && (
        <span aria-hidden className="text-[10px] leading-none" style={{ color }}>
          {glyph}
        </span>
      )}
      <span>{label}</span>
      <span className="tabular-nums text-content-faint">({count})</span>
    </button>
  );
}

function Th({
  children,
  align = "left",
  sortable = false,
  active = false,
  desc = false,
  onClick,
}: {
  children?: React.ReactNode;
  align?: "left" | "right";
  sortable?: boolean;
  active?: boolean;
  desc?: boolean;
  onClick?: () => void;
}) {
  const base = `px-3 py-2 font-medium ${align === "right" ? "text-right" : "text-left"}`;
  if (!sortable) return <th className={base}>{children}</th>;
  return (
    <th className={base} aria-sort={active ? (desc ? "descending" : "ascending") : "none"}>
      <button
        type="button"
        onClick={onClick}
        className={`inline-flex items-center gap-1 transition hover:text-content ${
          active ? "text-content" : ""
        }`}
      >
        {children}
        <span aria-hidden className="text-[8px]">
          {active ? (desc ? "▼" : "▲") : "▲▼"}
        </span>
      </button>
    </th>
  );
}
