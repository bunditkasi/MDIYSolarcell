import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "MR.DIY Solar — Fleet Monitoring",
  description:
    "Rooftop solar fleet monitoring for MR.DIY Thailand: live output, branch status, string-level fault detection and ESG reporting.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
