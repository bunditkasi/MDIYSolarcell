"""Vendor data -> database.

The adapters fetch and normalise; this module is what actually persists. It is
the last piece between a working credential and live data on the map.

TWO THINGS IT DOES THAT ARE EASY TO GET WRONG
---------------------------------------------
1. MAPPING. Vendor site ids mean nothing to us; ``store_code`` does. In this
   tenancy the vendor's site NAME is the 4-letter branch code, so the two can be
   matched automatically — which is the difference between wiring 111 branches
   by hand and running one command. Anything that does not match is REPORTED,
   never guessed at.

2. IDEMPOTENCY. Re-polling an overlapping window is normal after a failed sync,
   so every write upserts on its natural key. Without that, one retry doubles a
   day's yield and the error is invisible until somebody reconciles a report.

3. COMMIT PER SITE. A fleet sweep is ~100 sites and several minutes of vendor
   calls. Wrapping all of it in one transaction was the first design and it was
   wrong twice over: nothing became durable until the last site landed, so a
   failure at site 97 threw away 96 sites' work, and the run held write locks
   for minutes. Each site now commits on its own — partial progress survives,
   and the upserts above make resuming safe.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DeviceType, MeasurementBasis
from app.infrastructure.db.orm import (
    DeviceORM,
    StoreORM,
    TelemetryRawORM,
    TelemetryStringORM,
    VendorSiteLinkORM,
)
from app.ingestion.base import (
    DeviceReading,
    InverterDataSourceInterface,
    PanelReading,
    SiteReading,
    VendorSite,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SiteMapping",
    "SyncReport",
    "extract_store_code",
    "map_vendor_sites",
    "sync_site_readings",
]


@dataclass
class SyncReport:
    vendor_key: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sites_matched: int = 0
    sites_unmatched: list[str] = field(default_factory=list)
    devices_created: int = 0
    #: Branches registered from vendor data because the roster lacked them.
    stores_created: list[str] = field(default_factory=list)
    raw_rows: int = 0
    #: Of raw_rows, how many are station totals rather than per-inverter
    #: readings. These carry no per-string detail, so fault detection cannot
    #: run on them.
    site_level_rows: int = 0
    #: Branches whose blank fields were filled from vendor data this sweep —
    #: battery capacity and grid-tied date, which no workbook column supplies.
    stores_enriched: int = 0
    string_rows: int = 0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Ingestion sync — {self.vendor_key}",
            f"  branches matched   : {self.sites_matched}",
            f"  devices registered : {self.devices_created}",
            f"  inverter readings  : {self.raw_rows}",
            (
                f"    of which station totals : {self.site_level_rows}"
                if self.site_level_rows
                else "    all per-inverter"
            ),
            f"  panel readings     : {self.string_rows}",
        ]
        if self.site_level_rows and not self.string_rows:
            lines.append(
                "  NOTE: station totals carry no per-string data, so panel and"
            )
            lines.append(
                "  string fault detection cannot run for those branches."
            )
        if self.stores_enriched:
            lines.append(
                f"  branches enriched  : {self.stores_enriched} "
                f"(battery / grid-tied date from the vendor)"
            )
        if self.stores_created:
            lines.append(
                f"  branches CREATED from vendor data ({len(self.stores_created)}): "
                + ", ".join(self.stores_created)
            )
            lines.append(
                "    Capacity and name come from the vendor. Coordinates, CapEx and"
            )
            lines.append(
                "    tariff are blank until BaseInfo/Solar Report.xlsx supplies them,"
            )
            lines.append("    so these will not appear on the map yet.")
        if self.sites_unmatched:
            shown = ", ".join(self.sites_unmatched[:10])
            extra = len(self.sites_unmatched) - 10
            more = f" (+{extra} more)" if extra > 0 else ""
            lines.append(f"  UNMATCHED vendor sites ({len(self.sites_unmatched)}): {shown}{more}")
            lines.append("    These exist at the vendor but not in our roster — check the")
            lines.append("    branch code, or add the branch to BaseInfo/Solar Report.xlsx.")
        if self.errors:
            lines.append(f"  errors ({len(self.errors)}):")
            lines.extend(f"    {e}" for e in self.errors[:10])
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SiteMapping:
    vendor_site_id: str
    store_id: UUID
    store_code: str


def extract_store_code(vendor_site_name: str, known_codes: set[str]) -> str | None:
    """Pull the 4-letter branch code out of a vendor site name.

    The two vendors name sites very differently:

        Atmoce : "PMPP"
        Huawei : "12  PLPR  Mr.DIY Nern payom."

    Huawei prefixes a rollout-phase number and appends a description, so an
    exact-string match finds nothing — which is exactly what happened on the
    first live run: 50 sites, 0 matched. The code is matched wherever it appears
    rather than only at the start.

    Only codes we already know are accepted. Any 4-character token would
    otherwise match things like "2026" or "MRDI" and silently attach a vendor
    site to the wrong branch, which is far worse than leaving it unmatched.
    """
    name = vendor_site_name.strip().upper()
    if name in known_codes:
        return name

    for separator in ("-", "_", ".", ",", "/", "(", ")"):
        name = name.replace(separator, " ")

    for token in name.split():
        if len(token) == 4 and token.isalnum() and token in known_codes:
            return token
    return None


async def map_vendor_sites(
    session: AsyncSession,
    sites: list[VendorSite],
    *,
    report: SyncReport,
    source_vendor_key: str,
    create_missing: bool = False,
) -> list[SiteMapping]:
    """Match vendor sites onto our branches by 4-letter code.

    Matching is on the vendor's site NAME, not its id: the id is an opaque
    integer, while the name in this tenancy is the branch code. Both the name
    and, as a fallback, any 4-letter token inside it are tried, because operators
    sometimes append a description to the code.

    Order of resolution: an explicit row in ``vendor_site_links`` first, then
    the branch code in the site name. A manual link always wins — it exists
    because a name could not be resolved, so a rule must not override it.

    ``create_missing`` registers branches the vendor knows about but the roster
    does not. It is off by default — a branch is a business fact, and inventing
    one from a vendor string is how a store nobody has verified ends up in a
    board report. Turned on, it fills in only what the vendor actually states
    (code, name, capacity) and leaves everything else NULL: no coordinates, no
    CapEx, no tariff. Those stay blank until the workbook supplies them, so the
    branch appears in listings and on the dashboard while being visibly
    incomplete rather than quietly wrong.
    """
    rows = (await session.execute(select(StoreORM.store_id, StoreORM.store_code))).all()
    by_code = {code.upper(): store_id for store_id, code in rows}
    by_store_id = {store_id: code.upper() for store_id, code in rows}

    # Manual links win over name matching. They exist precisely for the sites a
    # name cannot resolve, so a rule must never override a human decision.
    link_rows = (
        await session.execute(
            select(VendorSiteLinkORM.vendor_site_id, VendorSiteLinkORM.store_id).where(
                VendorSiteLinkORM.vendor_key == source_vendor_key
            )
        )
    ).all()
    manual = {vendor_site_id: store_id for vendor_site_id, store_id in link_rows}

    known = set(by_code)
    mappings: list[SiteMapping] = []

    for site in sites:
        linked = manual.get(site.vendor_site_id)
        if linked is not None:
            mappings.append(
                SiteMapping(
                    vendor_site_id=site.vendor_site_id,
                    store_id=linked,
                    store_code=by_store_id.get(linked, "?"),
                )
            )
            continue

        code = extract_store_code(site.name, known)

        if code is None and create_missing:
            code = _code_from_name(site.name)
            if code and code not in known:
                store_id = await _create_store_from_vendor(session, site, code)
                if store_id is not None:
                    by_code[code] = store_id
                    known.add(code)
                    report.stores_created.append(code)

        if code is None or code not in by_code:
            report.sites_unmatched.append(site.name)
            continue

        mappings.append(
            SiteMapping(
                vendor_site_id=site.vendor_site_id,
                store_id=by_code[code],
                store_code=code,
            )
        )

    report.sites_matched = len(mappings)
    return mappings


async def _ensure_device(
    session: AsyncSession,
    *,
    store_id: UUID,
    serial_number: str,
    source: InverterDataSourceInterface,
    report: SyncReport,
) -> UUID | None:
    """Find the device by serial, registering it if the vendor reports a new one.

    Devices ARE created automatically, unlike branches: a serial number is the
    vendor's own identifier for hardware it is already reporting on, so there is
    nothing to guess. A branch, by contrast, is a business fact.
    """
    existing = await session.scalar(
        select(DeviceORM.device_id).where(DeviceORM.serial_number == serial_number)
    )
    if existing is not None:
        return existing

    device = DeviceORM(
        store_id=store_id,
        brand=source.vendor_key,
        serial_number=serial_number,
        device_type=(
            DeviceType.MICROINVERTER.value
            if source.measurement_basis is MeasurementBasis.PANEL
            else DeviceType.INVERTER.value
        ),
        measurement_basis=source.measurement_basis.value,
        vendor_key=source.vendor_key,
    )
    session.add(device)
    await session.flush()
    report.devices_created += 1
    return device.device_id


SITE_AGGREGATE_PREFIX = "SITE-"


async def _ensure_site_device(
    session: AsyncSession,
    *,
    store_id: UUID,
    store_code: str,
    source: InverterDataSourceInterface,
    report: SyncReport,
) -> UUID:
    """A stand-in device representing the station meter for one branch.

    Some vendor accounts expose station totals but not the inverters behind
    them — Huawei's northbound permissions do exactly this. Telemetry is keyed
    by device, so the station reading needs something to hang from.

    Modelled as a LOGGER rather than an INVERTER because it is not one: it
    reports no per-string data and must never be counted as generating hardware.
    """
    serial = f"{SITE_AGGREGATE_PREFIX}{store_code}"
    existing = await session.scalar(
        select(DeviceORM.device_id).where(DeviceORM.serial_number == serial)
    )
    if existing is not None:
        return existing

    device = DeviceORM(
        store_id=store_id,
        brand=source.vendor_key,
        model="Station aggregate",
        serial_number=serial,
        device_type=DeviceType.LOGGER.value,
        measurement_basis=MeasurementBasis.STRING.value,
        vendor_key=source.vendor_key,
    )
    session.add(device)
    await session.flush()
    report.devices_created += 1
    return device.device_id


async def _write_raw(
    session: AsyncSession, device_id: UUID, reading: DeviceReading
) -> None:
    """Upsert one inverter reading on (device_id, time)."""
    stmt = insert(TelemetryRawORM).values(
        time=reading.measured_at,
        device_id=device_id,
        active_power_kw=reading.active_power_kw,
        daily_yield_kwh=reading.daily_yield_kwh,
        monthly_yield_kwh=reading.monthly_yield_kwh,
        total_yield_kwh=reading.total_yield_kwh,
        grid_voltage=reading.grid_voltage,
        grid_current=reading.grid_current,
        status_code=reading.status_code,
        payload=reading.raw or None,
    )
    # Re-polling the same window is routine after a failure; without this a
    # retry would double the day's figures.
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TelemetryRawORM.device_id, TelemetryRawORM.time],
            set_={
                "active_power_kw": stmt.excluded.active_power_kw,
                "daily_yield_kwh": stmt.excluded.daily_yield_kwh,
                "monthly_yield_kwh": stmt.excluded.monthly_yield_kwh,
                "total_yield_kwh": stmt.excluded.total_yield_kwh,
                "grid_voltage": stmt.excluded.grid_voltage,
                "grid_current": stmt.excluded.grid_current,
                "status_code": stmt.excluded.status_code,
            },
        )
    )


async def _write_panel(
    session: AsyncSession, device_id: UUID, reading: PanelReading
) -> None:
    stmt = insert(TelemetryStringORM).values(
        time=reading.measured_at,
        device_id=device_id,
        mppt_index=reading.mppt_index,
        string_index=reading.string_index,
        pv_voltage=reading.pv_voltage,
        pv_current=reading.pv_current,
        pv_power_kw=reading.pv_power_kw,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                TelemetryStringORM.device_id,
                TelemetryStringORM.mppt_index,
                TelemetryStringORM.string_index,
                TelemetryStringORM.time,
            ],
            set_={
                "pv_voltage": stmt.excluded.pv_voltage,
                "pv_current": stmt.excluded.pv_current,
                "pv_power_kw": stmt.excluded.pv_power_kw,
            },
        )
    )


async def sync_site_readings(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    source: InverterDataSourceInterface,
    *,
    include_panels: bool = False,
    include_devices: bool = True,
    max_sites: int | None = None,
    create_missing_stores: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> SyncReport:
    """Pull from the vendor and persist. Read the vendor, write us.

    ``include_panels`` is off by default because per-panel detail costs one call
    PER SITE against a monthly budget, while the site sweep covers 100 sites in
    one. The scheduler runs panels on a much slower cadence.

    ``include_devices`` governs the same trade for inverter-level readings, and
    is what makes a frequent sweep affordable at all. With it on, one sweep of
    107 branches costs 107 calls; four times an hour through daylight that is
    roughly 148,000 calls a month against a budget of 10,000. The scheduler
    leaves it OFF for the 15-minute poll — which then costs two calls — and on
    for the daily detail run. CLI callers keep it on, because a one-shot pull is
    usually asking for exactly that detail.

    Takes a session FACTORY rather than a session: each site commits in its own
    transaction, so a long sweep makes durable progress instead of staking all
    of it on the final call.
    """
    report = SyncReport(vendor_key=source.vendor_key)

    await source.authenticate()
    sites = await source.list_sites()

    # Branch registrations commit first, so the rows telemetry references
    # already exist and are durable before any reading is written.
    async with session_factory() as session:
        mapped = await map_vendor_sites(
            session,
            sites,
            report=report,
            source_vendor_key=source.vendor_key,
            create_missing=create_missing_stores,
        )
        mappings = [
            SiteMapping(m.vendor_site_id, m.store_id, m.store_code) for m in mapped
        ]

    if not mappings:
        report.errors.append(
            "No vendor site matched a branch code. Nothing was written."
        )
        return report

    # Backfill the store fields only the vendor reports. Done once per sweep
    # against the already-mapped sites, so it costs no extra API call.
    by_site = {s.vendor_site_id: s for s in sites}
    async with session_factory() as session:
        for mapping in mappings:
            site = by_site.get(mapping.vendor_site_id)
            if site is not None and await _fill_store_gaps(
                session, mapping.store_id, site
            ):
                report.stores_enriched += 1

    if max_sites:
        mappings = mappings[:max_sites]

    by_vendor_id = {m.vendor_site_id: m for m in mappings}

    # Site-level readings are fetched in batches at the vendor's stated limit —
    # this is what keeps a 111-site fleet inside a 10,000-call monthly budget.
    batch_size = max(1, source.max_sites_per_call)
    ids = list(by_vendor_id)
    site_readings: list[SiteReading] = []
    for start in range(0, len(ids), batch_size):
        chunk = ids[start : start + batch_size]
        try:
            site_readings.extend(await source.fetch_site_readings(chunk))
        except Exception as exc:  # noqa: BLE001 — one bad batch must not lose the rest
            report.errors.append(f"site batch failed: {exc}")

    by_site_reading = {r.vendor_site_id: r for r in site_readings}
    logger.info("%s: %d site readings fetched", source.vendor_key, len(site_readings))

    # Device-level detail is per-site, so it is the expensive half: fetched,
    # written and committed one site at a time.
    total = len(mappings)
    for index, mapping in enumerate(mappings, start=1):
        if progress:
            progress(index, total, mapping.store_code)

        devices: list[DeviceReading] = []
        try:
            if include_devices:
                devices = await source.fetch_device_readings(mapping.vendor_site_id)
        except Exception as exc:  # noqa: BLE001
            # Not fatal: the station reading below may still be available, and
            # for accounts without device permissions it is the only data there
            # is. Losing the whole branch over it would be the wrong trade.
            report.errors.append(f"{mapping.store_code}: device readings failed: {exc}")
            devices = []

        panels: list[PanelReading] = []
        if include_panels and source.supports_panel_data:
            try:
                panels = await source.fetch_panel_readings(mapping.vendor_site_id)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(
                    f"{mapping.store_code}: panel readings failed: {exc}"
                )

        site_reading = by_site_reading.get(mapping.vendor_site_id)

        try:
            async with session_factory() as session:
                # The station total is written whenever the vendor supplies
                # one, including alongside inverter rows. It is not double
                # counting: the fleet query treats a station meter as
                # AUTHORITATIVE for the site total where one exists and only
                # sums inverters in its absence, because the meter already
                # measures what they produce.
                #
                # This is what lets the frequent sweep skip the per-site device
                # endpoint and still keep the map current.
                if site_reading is not None:
                    device_id = await _ensure_site_device(
                        session,
                        store_id=mapping.store_id,
                        store_code=mapping.store_code,
                        source=source,
                        report=report,
                    )
                    await _write_raw(
                        session,
                        device_id,
                        DeviceReading(
                            serial_number=f"{SITE_AGGREGATE_PREFIX}{mapping.store_code}",
                            measured_at=site_reading.measured_at,
                            active_power_kw=site_reading.active_power_kw,
                            daily_yield_kwh=site_reading.daily_yield_kwh,
                            monthly_yield_kwh=site_reading.monthly_yield_kwh,
                            total_yield_kwh=site_reading.total_yield_kwh,
                            raw={"source": "station", "status": site_reading.status},
                        ),
                    )
                    report.raw_rows += 1
                    report.site_level_rows += 1

                for reading in devices:
                    device_id = await _ensure_device(
                        session,
                        store_id=mapping.store_id,
                        serial_number=reading.serial_number,
                        source=source,
                        report=report,
                    )
                    if device_id is None:
                        continue
                    await _write_raw(session, device_id, reading)
                    report.raw_rows += 1

                for panel in panels:
                    device_id = await _ensure_device(
                        session,
                        store_id=mapping.store_id,
                        serial_number=panel.serial_number,
                        source=source,
                        report=report,
                    )
                    if device_id is None:
                        continue
                    await _write_panel(session, device_id, panel)
                    report.string_rows += 1
        except Exception as exc:  # noqa: BLE001 — one site must not end the sweep
            report.errors.append(f"{mapping.store_code}: write failed: {exc}")

    logger.info(report.render())
    return report


def _code_from_name(vendor_site_name: str) -> str | None:
    """Best-effort branch code for a site the roster has never seen.

    Used only when creating a branch, where there is no known-code list to check
    against, so the rules are stricter to compensate: the token must be exactly
    four characters, contain a letter, and not be a bare year or phase number.
    """
    name = vendor_site_name.strip().upper()
    for separator in ("-", "_", ".", ",", "/", "(", ")"):
        name = name.replace(separator, " ")

    for token in name.split():
        if len(token) != 4 or not token.isalnum():
            continue
        if token.isdigit():
            continue  # "2026", "1234" — a year or an id, not a branch
        if not any(c.isalpha() for c in token):
            continue
        return token
    return None


async def _fill_store_gaps(
    session: AsyncSession, store_id: UUID, site: VendorSite
) -> bool:
    """Fill store fields the vendor knows and our roster does not.

    Only ever fills a NULL. The workbook is the system of record for anything
    an operator maintains by hand, so a value already recorded there wins even
    when the vendor disagrees — otherwise a correction someone made in the
    workbook would be silently reverted on the next sweep.

    Battery capacity and grid-tied date are the two fields no workbook column
    supplies, so for most branches the vendor is the only source there is.
    Capacity is filled on the same NULL-only terms: for a branch the roster has
    never seen, the vendor is the only source of it, and a NULL capacity makes
    every kWh/kWp figure for that branch uncomputable.
    """
    store = await session.get(StoreORM, store_id)
    if store is None:
        return False

    changed = False
    if (
        store.installed_kwp is None
        and site.capacity_kwp is not None
        and site.capacity_kwp > 0
    ):
        store.installed_kwp = site.capacity_kwp
        changed = True
    if store.battery_capacity_kwh is None and site.battery_capacity_kwh is not None:
        store.battery_capacity_kwh = site.battery_capacity_kwh
        changed = True
    if store.commissioned_at is None and site.grid_tied_on is not None:
        store.commissioned_at = site.grid_tied_on
        changed = True
    return changed


async def _create_store_from_vendor(
    session: AsyncSession, site: VendorSite, code: str
) -> UUID | None:
    """Register a branch from vendor data, filling only what the vendor states.

    Everything the vendor does not know is left NULL rather than defaulted.
    A guessed coordinate would put a pin somewhere wrong on the map; a guessed
    capacity would corrupt every kWh/kWp comparison it takes part in. Both are
    nullable precisely so a branch can exist while incomplete.
    """
    capacity = site.capacity_kwp
    if capacity is not None and capacity <= 0:
        # Zero or negative is a bad reading, not "unknown". Discard it and let
        # the column stay NULL rather than storing a value that fails the CHECK.
        capacity = None

    if capacity is None:
        logger.info(
            "Registering branch %s with no capacity — the vendor has not "
            "reported one. It will show as incomplete until the roster does.",
            code,
        )

    store = StoreORM(
        store_code=code,
        store_name=site.name.strip() or code,
        installed_kwp=capacity,
        lat=site.latitude,
        lng=site.longitude,
        battery_capacity_kwh=site.battery_capacity_kwh,
        commissioned_at=site.grid_tied_on,
        is_active=True,
    )
    session.add(store)
    await session.flush()
    logger.info(
        "Registered branch %s from vendor data (%s)",
        code,
        f"{capacity} kWp" if capacity is not None else "capacity unknown",
    )
    return store.store_id
