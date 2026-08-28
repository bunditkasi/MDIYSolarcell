"""Link a vendor site to a branch by hand.

    python -m app.ingestion.link --vendor huawei --site NE=81818192 \
        --store PMGN --note "Account DIYNAN@; site name carries no branch code"

    python -m app.ingestion.link --list

For vendor sites whose name carries no 4-letter branch code, so automatic
matching cannot resolve them. Every link records who made it and why, because a
mapping nobody can justify is one nobody can safely change later.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, select

from app.infrastructure.db.orm import StoreORM, VendorSiteLinkORM
from app.infrastructure.db.session import dispose_engine, session_scope


async def show_links() -> int:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(VendorSiteLinkORM, StoreORM)
                .join(StoreORM, StoreORM.store_id == VendorSiteLinkORM.store_id)
                .order_by(VendorSiteLinkORM.vendor_key, StoreORM.store_code)
            )
        ).all()

    if not rows:
        print("No manual links. Every vendor site is resolved by its name.")
        return 0

    print(f"{len(rows)} manual link(s):\n")
    for link, store in rows:
        print(f"  {link.vendor_key:<8} {link.vendor_site_id:<16} -> {store.store_code}")
        print(f"           {store.store_name}")
        if link.note:
            print(f"           note: {link.note}")
        print(f"           added {link.created_at:%Y-%m-%d} by {link.created_by or 'unknown'}")
        print()
    return 0


async def create_link(vendor: str, site_id: str, store_code: str, note: str, by: str) -> int:
    async with session_scope() as session:
        store = await session.scalar(
            select(StoreORM).where(StoreORM.store_code == store_code.upper())
        )
        if store is None:
            print(f"No branch with code {store_code!r}.")
            return 1

        # Replace rather than duplicate: the unique constraint would reject a
        # second row, and re-pointing a link is a legitimate correction.
        await session.execute(
            delete(VendorSiteLinkORM).where(
                VendorSiteLinkORM.vendor_key == vendor,
                VendorSiteLinkORM.vendor_site_id == site_id,
            )
        )
        session.add(
            VendorSiteLinkORM(
                vendor_key=vendor,
                vendor_site_id=site_id,
                store_id=store.store_id,
                note=note or None,
                created_by=by or None,
            )
        )

    print(f"Linked {vendor} site {site_id} -> {store.store_code} ({store.store_name}).")
    print("The next ingestion run will attribute that site's data to this branch.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(prog="app.ingestion.link", description=__doc__)
    parser.add_argument("--list", action="store_true", help="show existing links")
    parser.add_argument("--vendor")
    parser.add_argument("--site", help="the vendor's own site id")
    parser.add_argument("--store", help="our 4-letter branch code")
    parser.add_argument("--note", default="", help="why this link is correct")
    parser.add_argument("--by", default="", help="who decided it")
    args = parser.parse_args()

    try:
        if args.list:
            return await show_links()
        if not (args.vendor and args.site and args.store):
            parser.error("--vendor, --site and --store are all required")
        return await create_link(args.vendor, args.site, args.store, args.note, args.by)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
