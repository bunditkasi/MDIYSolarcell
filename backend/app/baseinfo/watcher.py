"""Watch BaseInfo/ and re-import when the workbook changes.

Polls rather than using filesystem events. Polling is unglamorous but it is the
option that actually works here: the directory is a Docker bind mount from a
Windows host, and inotify events do not cross that boundary reliably. A ten
second poll on one small directory costs nothing.

TWO PROBLEMS THIS SOLVES THAT A NAIVE WATCHER DOES NOT
-----------------------------------------------------
1. PARTIAL WRITES. Excel does not save atomically — it writes a temporary file,
   then renames. Importing the instant an mtime changes can read a half-written
   workbook. The watcher therefore waits for the size and mtime to stop moving
   before it acts.

2. EXCEL LOCK FILES. An open workbook produces a sibling `~$Name.xlsx`. Those
   are not workbooks and must never be imported.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["file_fingerprint", "find_workbook", "watch_directory"]

WORKBOOK_GLOB = "*.xlsx"

#: Consecutive identical readings required before a file counts as settled.
#: Two at a ten-second interval means a save is picked up within ~20s while
#: never reading a file Excel is still writing.
STABLE_READINGS_REQUIRED = 2

#: Only the first megabyte is hashed. Enough to notice any real edit, and it
#: keeps the poll cheap on a 400KB-and-growing workbook.
HASH_BYTES = 1_000_000


def find_workbook(directory: Path) -> Path | None:
    """Newest real workbook in the directory, ignoring Excel lock files."""
    candidates = [
        p
        for p in directory.glob(WORKBOOK_GLOB)
        if p.is_file() and not p.name.startswith("~$")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def file_fingerprint(path: Path) -> str:
    """Identify a file version by size, mtime and a hash of its head.

    mtime alone is not enough: copying a file over another can preserve it, and
    then a real change looks like no change at all.
    """
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(HASH_BYTES))
    return f"{stat.st_size}:{int(stat.st_mtime)}:{digest.hexdigest()[:16]}"


async def watch_directory(
    directory: Path,
    *,
    interval_seconds: float = 10.0,
    on_change: Callable[[Path], Awaitable[int]],
    max_iterations: int | None = None,
) -> None:
    """Poll ``directory`` and call ``on_change`` once the workbook settles.

    ``max_iterations`` exists so tests can run the loop a fixed number of times;
    leave it None in production.
    """
    if not directory.is_dir():
        logger.error("BaseInfo directory does not exist: %s", directory)
        return

    logger.info(
        "Watching %s for %s every %.0fs. Drop the workbook there to import it.",
        directory,
        WORKBOOK_GLOB,
        interval_seconds,
    )

    last_imported: str | None = None
    pending: str | None = None
    stable_count = 0
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            path = find_workbook(directory)
            if path is None:
                pending, stable_count = None, 0
            else:
                fingerprint = file_fingerprint(path)

                if fingerprint == last_imported:
                    pending, stable_count = None, 0

                elif fingerprint == pending:
                    stable_count += 1
                    if stable_count >= STABLE_READINGS_REQUIRED:
                        logger.info("Detected a new version of %s — importing.", path.name)
                        try:
                            exit_code = await on_change(path)
                        except Exception:
                            # A bad workbook must not kill the watcher; the next
                            # save should get another chance.
                            logger.exception("Import failed for %s", path.name)
                            exit_code = 1

                        # Marked as handled either way. Retrying a file that
                        # just failed, unchanged, would loop forever.
                        last_imported = fingerprint
                        pending, stable_count = None, 0
                        if exit_code:
                            logger.warning(
                                "Import of %s finished with exit code %d — see the "
                                "report above.",
                                path.name,
                                exit_code,
                            )
                else:
                    # First sighting of this version, or it changed again while
                    # settling. Restart the stability count.
                    pending, stable_count = fingerprint, 1

        except FileNotFoundError:
            # The file was replaced between glob and stat. Normal during a save.
            pending, stable_count = None, 0
        except Exception:
            logger.exception("Watcher poll failed; continuing.")

        await asyncio.sleep(interval_seconds)
