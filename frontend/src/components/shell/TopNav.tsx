"use client";

/**
 * Application shell navigation.
 *
 * Follows the layout the operations team already knows from the Atmoce portal —
 * brand left, section tabs centre, account right — so the switch costs them no
 * relearning.
 *
 * One deliberate departure: Atmoce collapses tabs into a "..." overflow well
 * before the viewport is actually narrow, which buries Accounts and PV
 * Diagnosis on a normal laptop. Here every section stays reachable, moving to a
 * scrollable row on small screens rather than hiding behind a menu.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
}

const ICON = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none" } as const;
const STROKE = {
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const NAV: NavItem[] = [
  {
    href: "/overview",
    label: "Overview",
    icon: (
      <svg {...ICON} aria-hidden>
        <rect x="3" y="3" width="7" height="8" rx="1.5" {...STROKE} />
        <rect x="14" y="3" width="7" height="5" rx="1.5" {...STROKE} />
        <rect x="3" y="14" width="7" height="7" rx="1.5" {...STROKE} />
        <rect x="14" y="11" width="7" height="10" rx="1.5" {...STROKE} />
      </svg>
    ),
  },
  {
    href: "/map",
    label: "Map",
    icon: (
      <svg {...ICON} aria-hidden>
        <path d="M9 3 3 5.5v15L9 18l6 3 6-2.5v-15L15 6 9 3Z" {...STROKE} />
        <path d="M9 3v15M15 6v15" {...STROKE} />
      </svg>
    ),
  },
  {
    href: "/fleet",
    label: "Fleet",
    icon: (
      <svg {...ICON} aria-hidden>
        <path d="M4 6h16M4 12h16M4 18h16" {...STROKE} />
      </svg>
    ),
  },
  {
    href: "/alerts",
    label: "Alerts",
    icon: (
      <svg {...ICON} aria-hidden>
        <path d="M18 8a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7Z" {...STROKE} />
        <path d="M10.5 20a2 2 0 0 0 3 0" {...STROKE} />
      </svg>
    ),
  },
  {
    href: "/reports",
    label: "Reports & ESG",
    icon: (
      <svg {...ICON} aria-hidden>
        <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" {...STROKE} />
        <path d="M14 3v5h5M9 13h6M9 17h4" {...STROKE} />
      </svg>
    ),
  },
];

export default function TopNav({ openAlerts = 0 }: { openAlerts?: number }) {
  const pathname = usePathname();

  const isActive = (href: string) =>
    pathname === href || pathname.startsWith(`${href}/`) ||
    // A store detail page belongs to Fleet, so the tab stays lit while drilling in.
    (href === "/fleet" && pathname.startsWith("/stores"));

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface/95 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-4 px-4">
        <Link href="/overview" className="flex shrink-0 items-center gap-2.5">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-accent/15 text-accent-bright">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
              <circle cx="12" cy="12" r="4" {...STROKE} />
              <path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" {...STROKE} />
            </svg>
          </span>
          <span className="hidden text-sm font-semibold tracking-tight sm:block">
            MR.DIY <span className="text-content-muted">Solar</span>
          </span>
        </Link>

        <nav
          aria-label="Sections"
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
        >
          {NAV.map((item) => {
            const active = isActive(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`flex shrink-0 items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                  active
                    ? "bg-accent text-accent-on"
                    : "text-content-muted hover:bg-surface-3 hover:text-content"
                }`}
              >
                {item.icon}
                <span>{item.label}</span>
                {item.href === "/alerts" && openAlerts > 0 && (
                  <span
                    className={`ml-0.5 rounded px-1.5 py-0.5 text-2xs tabular-nums ${
                      active ? "bg-black/15 text-accent-on" : "bg-status-crit/15 text-status-crit"
                    }`}
                  >
                    {openAlerts}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>

        <div className="flex shrink-0 items-center gap-2">
          {/* Phase 1 runs with authentication bypassed. Saying so in the shell
              means nobody mistakes this for a signed-in session. */}
          <span
            className="hidden rounded-md border border-status-warn/30 bg-status-warn/10 px-2 py-1 text-2xs text-status-warn md:block"
            title="AUTH_MODE=mock — every request is treated as a full-access user. Phase 2 replaces this with corporate SSO."
          >
            Auth: mock
          </span>
          <span className="grid h-8 w-8 place-items-center rounded-full border border-line bg-surface-3 text-content-muted">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
              <circle cx="12" cy="8.5" r="3.5" {...STROKE} />
              <path d="M4.5 20a7.5 7.5 0 0 1 15 0" {...STROKE} />
            </svg>
          </span>
        </div>
      </div>
    </header>
  );
}
