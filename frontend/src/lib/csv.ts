/**
 * CSV export.
 *
 * Two details here are not decoration:
 *
 * 1. A UTF-8 BOM is prepended. Excel on Windows — which is what this fleet will
 *    be opened in — reads a BOM-less UTF-8 file as the system codepage, and
 *    every Thai branch name arrives as mojibake. One three-byte prefix is the
 *    difference between a usable export and an unreadable one.
 *
 * 2. Numbers are written unformatted. The table shows "1,930.5" because that is
 *    easier to read; a CSV carrying that same string lands in the spreadsheet as
 *    text, and nothing downstream can sum it.
 */

const BOM = "﻿";

/** Quote a field only when it would otherwise break the row. */
function escape(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = String(value);
  if (!/[",\r\n]/.test(text)) return text;
  return `"${text.replace(/"/g, '""')}"`;
}

export interface CsvColumn<T> {
  header: string;
  /** Raw value for the cell. Return a number for anything meant to be summed. */
  value: (row: T) => string | number | null | undefined;
}

export function toCsv<T>(rows: T[], columns: CsvColumn<T>[]): string {
  const lines = [columns.map((c) => escape(c.header)).join(",")];
  for (const row of rows) {
    lines.push(columns.map((c) => escape(c.value(row))).join(","));
  }
  // CRLF: the line ending every spreadsheet on Windows expects.
  return BOM + lines.join("\r\n") + "\r\n";
}

/**
 * Hand the file to the browser.
 *
 * Revoking the object URL is not optional housekeeping — without it the blob is
 * pinned in memory for the life of the tab, and this page is one people leave
 * open all day.
 */
export function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

/** `mrdiy-solar-fleet-2026-08-28.csv` — sortable, and says when it was taken. */
export function stampedFilename(prefix: string): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  return `${prefix}-${stamp}.csv`;
}
