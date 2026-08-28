"""Vendor connectivity check.

    python -m app.ingestion.check atmoce
    python -m app.ingestion.check huawei --base-url https://kr5.fusionsolar.huawei.com

Answers the only question that matters before wiring ingestion up: does the
account work, and does what it returns line up with our store roster?

It is READ ONLY. It authenticates, lists sites, pulls one small batch of live
readings, and prints what it found. Nothing is written to the database.

Credentials are read through SecretsProviderInterface from the environment and
are never printed, logged, or echoed — not even partially. A failure reports the
CLASS of failure (rejected / throttled / unreachable) so it can be acted on
without anyone having to look at the secret itself.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import build_data_source, get_secrets_provider
from app.core.secrets.base import SecretNotFoundError
from app.infrastructure.db.orm import StoreORM
from app.infrastructure.db.session import dispose_engine, session_scope
from app.ingestion.base import (
    AuthenticationError,
    IngestionError,
    QuotaExceededError,
    TransientVendorError,
)
from app.ingestion.sync import extract_store_code

DEFAULTS = {
    "atmoce": "https://www.atmocecloud.com",
    "huawei": "https://kr5.fusionsolar.huawei.com",
}


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def load_store_codes() -> set[str]:
    async with session_scope() as session:
        rows = (await session.scalars(select(StoreORM.store_code))).all()
        return {code.upper() for code in rows}


async def check(vendor: str, base_url: str, secrets_ref: str, sample: int) -> int:
    settings = get_settings()

    print(f"Vendor        : {vendor}")
    print(f"Endpoint      : {base_url}")
    print(f"Credential ref: {secrets_ref}  (value never read by this report)")
    print(f"Secrets from  : {settings.secrets_provider.value}")

    # -- Is the credential even present? ---------------------------------- #
    try:
        credential = await get_secrets_provider().get_credential(secrets_ref)
    except SecretNotFoundError:
        rule("RESULT: no credential configured")
        print(f"Nothing is stored under '{secrets_ref}'.")
        print("\nAdd it to .env yourself, then re-run. For Atmoce:")
        print(f"  SECRET__{secrets_ref.upper()}__APIKEY=<app_key>")
        print(f"  SECRET__{secrets_ref.upper()}__PASSWORD=<app_secret>")
        print("For Huawei FusionSolar northbound:")
        print(f"  SECRET__{secrets_ref.upper()}__USERNAME=<userName>")
        print(f"  SECRET__{secrets_ref.upper()}__PASSWORD=<systemCode>")
        return 2

    # Reports WHICH fields are set, never what they contain.
    present = [
        name
        for name, value in (
            ("username", credential.username),
            ("password", credential.password),
            ("api_key", credential.api_key),
        )
        if value
    ]
    print(f"Fields present: {', '.join(present) or 'none'}")

    source = build_data_source(vendor_key=vendor, base_url=base_url, secrets_ref=secrets_ref)

    try:
        # -- Authenticate -------------------------------------------------- #
        rule("1. Authentication")
        try:
            await source.authenticate()
            print("OK — session established.")
        except AuthenticationError as exc:
            print(f"REJECTED — {exc}")
            print("\nThe endpoint answered and refused the credential. Check that the")
            print("key/secret pair is current and that API access is enabled for it.")
            return 1
        except TransientVendorError as exc:
            print(f"UNREACHABLE — {exc}")
            print("\nThe request never got a usable answer: DNS, TLS, a proxy, or the")
            print("vendor being down. This is not a credential problem.")
            return 1

        # -- Sites --------------------------------------------------------- #
        rule("2. Site list")
        sites = await source.list_sites()
        print(f"{len(sites)} sites visible to this account.")
        for site in sites[:5]:
            capacity = f"{site.capacity_kwp} kWp" if site.capacity_kwp else "capacity unknown"
            print(f"   {site.vendor_site_id:<12} {site.name[:38]:<40} {capacity}")
        if len(sites) > 5:
            print(f"   … and {len(sites) - 5} more")

        if not sites:
            print("\nThe account authenticated but exposes no sites. Usually this means")
            print("API access is enabled on the account but no plants are shared with it.")
            return 1

        # -- Do the vendor's names match our roster? ----------------------- #
        rule("3. Match against our store roster")
        our_codes = await load_store_codes()
        print(f"{len(our_codes)} branches in our database.")

        matched: list[tuple[str, str]] = []
        unmatched: list[str] = []
        for site in sites:
            # Same matcher the real sync uses. Having a second copy here is how
            # this report once claimed 0 of 50 matched while the sync would have
            # matched all 50.
            code = extract_store_code(site.name, our_codes)
            if code:
                matched.append((site.vendor_site_id, code))
            else:
                unmatched.append(site.name)

        print(f"matched by code : {len(matched)}")
        print(f"no match here   : {len(unmatched)}")
        if unmatched[:8]:
            print(f"  unmatched sample: {', '.join(unmatched[:8])}")
        if matched[:5]:
            print("  matched sample :")
            for vendor_id, code in matched[:5]:
                print(f"     {code}  <-  vendor site {vendor_id}")

        # -- Live readings -------------------------------------------------- #
        rule("4. Live readings")
        batch = [s.vendor_site_id for s in sites[: min(sample, source.max_sites_per_call)]]
        print(f"Requesting {len(batch)} sites in one call "
              f"(vendor allows {source.max_sites_per_call} per call).")

        readings = await source.fetch_site_readings(batch)
        print(f"{len(readings)} readings returned.")

        statuses = Counter(r.status for r in readings)
        print(f"status mix: {dict(statuses)}")

        for reading in readings[:5]:
            print(
                f"   {reading.vendor_site_id:<12} {reading.status:<8} "
                f"power={reading.active_power_kw} kW  "
                f"today={reading.daily_yield_kwh} kWh  at {reading.measured_at:%Y-%m-%d %H:%M}"
            )

        # -- Panel detail ---------------------------------------------------- #
        rule("5. Per-panel detail (one site)")
        if not source.supports_panel_data:
            print("This vendor publishes no per-panel data.")
        else:
            first = batch[0]
            panels = await source.fetch_panel_readings(first)
            print(f"site {first}: {len(panels)} panel/string readings.")
            for panel in panels[:6]:
                print(
                    f"   {panel.serial_number:<18} mppt={panel.mppt_index} "
                    f"string={panel.string_index}  "
                    f"V={panel.pv_voltage} I={panel.pv_current} P={panel.pv_power_kw} kW"
                )
            if not panels:
                print("   none — this site reports nothing below device level.")

        rule("RESULT: connection works")
        print(f"API calls used by this check: {getattr(source, 'call_count', 0)}")
        print("Atmoce's monthly budget is 10,000 per token.")
        return 0

    except QuotaExceededError as exc:
        rule("RESULT: throttled")
        print(f"{exc}")
        print("\nNot a credential problem. Wait, then retry; do not loop.")
        return 1
    except IngestionError as exc:
        rule("RESULT: vendor rejected the request")
        print(f"{exc}")
        return 1
    finally:
        await source.close()
        await dispose_engine()


async def main() -> int:
    parser = argparse.ArgumentParser(prog="app.ingestion.check", description=__doc__)
    parser.add_argument("vendor", choices=sorted(DEFAULTS))
    parser.add_argument("--base-url")
    parser.add_argument(
        "--secrets-ref",
        help="Lookup key in the secrets provider. Defaults to <vendor>_main.",
    )
    parser.add_argument("--sample", type=int, default=5, help="sites to pull readings for")
    args = parser.parse_args()

    return await check(
        vendor=args.vendor,
        base_url=args.base_url or DEFAULTS[args.vendor],
        secrets_ref=args.secrets_ref or f"{args.vendor}_main",
        sample=args.sample,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
