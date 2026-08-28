"""BaseInfo import CLI.

    python -m app.baseinfo                 # dry run against BaseInfo/
    python -m app.baseinfo --apply         # write the changes
    python -m app.baseinfo --watch         # auto-import whenever the file changes
    python -m app.baseinfo --file path.xlsx

Dry run is the default on purpose: the point of an importer over a pile of
UPDATE statements is being able to see what it will do first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.baseinfo.importer import import_workbook
from app.baseinfo.reader import read_workbook
from app.baseinfo.watcher import watch_directory
from app.infrastructure.db.session import dispose_engine, session_scope

#: Where the operations team drops the workbook. Relative to the repository
#: root, mounted into the backend container at /BaseInfo.
DEFAULT_DIR = Path("/BaseInfo")
WORKBOOK_GLOB = "*.xlsx"


def _find_workbook(directory: Path) -> Path | None:
    candidates = [
        p for p in sorted(directory.glob(WORKBOOK_GLOB)) if not p.name.startswith("~$")
    ]
    if not candidates:
        return None
    # Newest wins, so dropping an updated copy alongside an old one does the
    # expected thing rather than silently importing last month's numbers.
    return max(candidates, key=lambda p: p.stat().st_mtime)


async def run_once(path: Path, *, apply: bool) -> int:
    read_result = read_workbook(path)

    async with session_scope() as session:
        report = await import_workbook(
            session, read_result, source_file=str(path), apply=apply
        )
        if not apply:
            # Nothing was written, but the diff read rows into the session;
            # roll back so a dry run leaves no trace at all.
            await session.rollback()

    print(report.render())

    if not report.succeeded:
        return 1
    # Rejected rows are a failure worth a non-zero exit, so CI or a cron wrapper
    # notices. Warnings are not — 35 stores with no coordinates is a known state.
    return 2 if report.rejected else 0


async def main() -> int:
    parser = argparse.ArgumentParser(prog="app.baseinfo", description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--file", type=Path, help="import this workbook directly")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--watch", action="store_true", help="re-import when the file changes")
    parser.add_argument("--interval", type=float, default=10.0, help="watch poll seconds")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        if args.watch:
            await watch_directory(
                args.dir,
                interval_seconds=args.interval,
                on_change=lambda p: run_once(p, apply=args.apply),
            )
            return 0

        path = args.file or _find_workbook(args.dir)
        if path is None:
            print(f"No {WORKBOOK_GLOB} found in {args.dir}. Drop the workbook there first.")
            return 1
        if not path.is_file():
            print(f"Not a file: {path}")
            return 1

        return await run_once(path, apply=args.apply)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
