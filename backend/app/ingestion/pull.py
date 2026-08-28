"""One-shot ingestion run.

    python -m app.ingestion.pull atmoce
    python -m app.ingestion.pull atmoce --panels
    python -m app.ingestion.pull huawei --secrets-ref huawei_korn \n        --max-sites 5

Fetches from the vendor and writes to the database. Use --max-sites on a first
run: it proves the whole path end to end for a handful of branches without
spending the monthly API budget on a full sweep.

Each site commits separately, so an interrupted run keeps what it already
wrote and re-running resumes safely — every write is an upsert.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.core.deps import build_data_source
from app.infrastructure.db.session import dispose_engine, session_scope
from app.ingestion.base import AuthenticationError, IngestionError
from app.ingestion.check import DEFAULTS
from app.ingestion.sync import sync_site_readings


async def main() -> int:
    parser = argparse.ArgumentParser(prog="app.ingestion.pull", description=__doc__)
    parser.add_argument("vendor", choices=sorted(DEFAULTS))
    parser.add_argument("--base-url")
    parser.add_argument("--secrets-ref")
    parser.add_argument(
        "--panels",
        action="store_true",
        help="also pull per-panel detail (one API call PER SITE — expensive)",
    )
    parser.add_argument("--max-sites", type=int, help="cap the run, for a rehearsal")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help=(
            "register branches the vendor knows but the roster does not. "
            "Name and capacity come from the vendor; coordinates, CapEx and "
            "tariff stay blank until the workbook supplies them."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    source = build_data_source(
        vendor_key=args.vendor,
        base_url=args.base_url or DEFAULTS[args.vendor],
        secrets_ref=args.secrets_ref or f"{args.vendor}_main",
    )

    def show(index: int, total: int, code: str) -> None:
        print(f"  [{index:>3}/{total}] {code}", flush=True)

    try:
        report = await sync_site_readings(
            session_scope,
            source,
            include_panels=args.panels,
            max_sites=args.max_sites,
            create_missing_stores=args.create_missing,
            progress=show,
        )

        print()
        print(report.render())
        print(f"\nAPI calls used: {getattr(source, 'call_count', 0)}")
        return 1 if report.errors and report.raw_rows == 0 else 0

    except AuthenticationError as exc:
        print(f"Authentication failed: {exc}")
        return 1
    except IngestionError as exc:
        print(f"Ingestion failed: {exc}")
        return 1
    finally:
        await source.close()
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
