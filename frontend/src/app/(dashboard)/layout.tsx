"use client";

import { useEffect, useState } from "react";

import TopNav from "@/components/shell/TopNav";
import { fetchAlertCounts } from "@/lib/api";

/**
 * Dashboard shell.
 *
 * The alert count lives here rather than in the Alerts page so the badge is
 * visible from every section — a dispatcher should not have to open a tab to
 * discover there is something to dispatch.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const [openAlerts, setOpenAlerts] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const load = () =>
      void fetchAlertCounts()
        .then((counts) => {
          if (!cancelled) setOpenAlerts(counts.CRITICAL + counts.MAJOR + counts.MINOR);
        })
        .catch(() => {
          // A failed badge fetch must never blank the page the user asked for.
        });

    load();
    const timer = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="min-h-dvh bg-bg">
      <TopNav openAlerts={openAlerts} />
      {children}
    </div>
  );
}
