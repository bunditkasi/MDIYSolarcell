"""PostgreSQL + TimescaleDB implementation of the store repository.

This is the Phase 1 half of the handover seam. Everything PostgreSQL-specific in
the read path lives here and nowhere else: ``DISTINCT ON``, ``ILIKE``, JSONB,
and the TimescaleDB chunk-exclusion patterns. Phase 2 adds a sibling module
implementing the same interface against the corporate SQL server, and
``app.core.deps`` picks between them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.exceptions import (
    DuplicateStoreCodeError,
    RepositoryError,
    StoreNotFoundError,
)
from app.domain.filters import Page, PageRequest, StoreFilter, StoreSortField
from app.domain.models import (
    AlertSeverity,
    Device,
    DeviceType,
    MeasurementBasis,
    Store,
    StoreWithStatus,
)
from app.domain.repositories import StoreRepositoryInterface
from app.domain.status import classify_pr_status
from app.infrastructure.db.orm import DeviceORM, StoreORM

__all__ = ["LocalPostgresRepository"]


#: Maps the sortable-field enum to real columns. Because the enum is closed,
#: nothing a client sends can reach ORDER BY as raw text.
_SORT_COLUMNS = {
    StoreSortField.STORE_CODE: StoreORM.store_code,
    StoreSortField.STORE_NAME: StoreORM.store_name,
    StoreSortField.REGION: StoreORM.region,
    StoreSortField.INSTALLED_KWP: StoreORM.installed_kwp,
    StoreSortField.CREATED_AT: StoreORM.created_at,
}


class LocalPostgresRepository(StoreRepositoryInterface):
    """Store repository backed by local PostgreSQL + TimescaleDB.

    The session is injected rather than created here: transaction boundaries
    belong to the caller (the request, or the worker's task), and injection is
    what makes this class testable against a throwaway transaction.
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    async def list_stores(
        self,
        store_filter: StoreFilter,
        page: PageRequest,
    ) -> Page[Store]:
        try:
            base = self._apply_filter(select(StoreORM), store_filter)

            total = await self._session.scalar(
                select(func.count()).select_from(base.subquery())
            )

            sort_column = _SORT_COLUMNS[page.sort_by]
            order = sort_column.desc() if page.descending else sort_column.asc()

            stmt = (
                base
                # store_id as a tiebreaker keeps ordering total. Without it,
                # rows sharing a sort value can swap between pages and the
                # client silently sees duplicates and gaps.
                .order_by(order, StoreORM.store_id.asc())
                .limit(page.limit)
                .offset(page.offset)
            )
            rows = (await self._session.scalars(stmt)).all()

            return Page(
                items=[_to_store(row) for row in rows],
                total=int(total or 0),
                limit=page.limit,
                offset=page.offset,
            )
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to list stores", cause=exc) from exc

    async def get_store(self, store_id: UUID) -> Store | None:
        try:
            row = await self._session.get(StoreORM, store_id)
            return _to_store(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to load store {store_id}", cause=exc) from exc

    async def get_store_by_code(self, store_code: str) -> Store | None:
        try:
            row = await self._session.scalar(
                select(StoreORM).where(StoreORM.store_code == store_code)
            )
            return _to_store(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to load store {store_code}", cause=exc) from exc

    async def count_stores(self, store_filter: StoreFilter) -> int:
        try:
            base = self._apply_filter(select(StoreORM.store_id), store_filter)
            total = await self._session.scalar(
                select(func.count()).select_from(base.subquery())
            )
            return int(total or 0)
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to count stores", cause=exc) from exc

    async def list_devices_for_store(self, store_id: UUID) -> list[Device]:
        try:
            rows = (
                await self._session.scalars(
                    select(DeviceORM)
                    .where(DeviceORM.store_id == store_id)
                    .order_by(DeviceORM.serial_number.asc())
                )
            ).all()
            return [_to_device(row) for row in rows]
        except SQLAlchemyError as exc:
            raise RepositoryError(
                f"Failed to list devices for store {store_id}", cause=exc
            ) from exc

    async def list_stores_with_status(
        self,
        store_filter: StoreFilter,
    ) -> list[StoreWithStatus]:
        """Fleet status for the GIS map, in one round trip.

        Hand-written SQL rather than ORM constructs: this is four correlated
        aggregations over a hypertable, and expressing it through the ORM would
        obscure the chunk-exclusion behaviour that makes it fast.

        Every telemetry CTE is bounded by a time window. That bound is what
        lets TimescaleDB skip every chunk but the newest — without it this query
        degrades from milliseconds to a full-history scan as data accumulates.
        """
        offline_minutes = self._settings.effective_offline_after_minutes
        green_threshold = self._settings.pr_green_threshold
        yield_threshold = self._settings.yield_green_threshold_pct
        min_peers = self._settings.yield_min_peers

        where_sql, params = _build_status_filter(store_filter)
        params["offline_minutes"] = offline_minutes

        # Staleness is judged per vendor, because the vendors are polled at
        # different rates. Huawei's northbound API throttles hard enough that it
        # runs on a two-hour cadence; measuring it against Atmoce's 15-minute
        # threshold would mark all 51 of its branches permanently offline.
        #
        # The CASE is generated from settings rather than written into the SQL,
        # so vendor policy stays in configuration.
        staleness_sql, threshold_params = _vendor_staleness_case(
            self._settings.offline_thresholds_by_vendor
        )
        params.update(threshold_params)
        # One day of telemetry is enough for "latest reading" and today's yield,
        # and keeps the scan inside the most recent chunk.
        params["lookback_hours"] = 24

        sql = text(
            f"""
            WITH latest_reading AS (
                -- DISTINCT ON is the PostgreSQL idiom for "newest row per group".
                SELECT DISTINCT ON (t.device_id)
                       t.device_id,
                       t."time"            AS reading_time,
                       t.active_power_kw,
                       t.daily_yield_kwh,
                       t.monthly_yield_kwh,
                       t.total_yield_kwh
                FROM telemetry_raw t
                WHERE t."time" >= now() - make_interval(hours => :lookback_hours)
                ORDER BY t.device_id, t."time" DESC
            ),
            store_telemetry AS (
                SELECT d.store_id,
                       -- A station meter measures the whole site, including
                       -- everything the inverters behind it produce. Where one
                       -- reports, it IS the site total; summing it together
                       -- with the inverter rows would count every branch twice.
                       -- Inverters are summed only in its absence.
                       COALESCE(
                           MAX(lr.active_power_kw)
                               FILTER (WHERE d.device_type = 'LOGGER'),
                           SUM(lr.active_power_kw)
                               FILTER (WHERE d.device_type <> 'LOGGER')
                       ) AS active_power_kw,
                       COALESCE(
                           MAX(lr.daily_yield_kwh)
                               FILTER (WHERE d.device_type = 'LOGGER'),
                           SUM(lr.daily_yield_kwh)
                               FILTER (WHERE d.device_type <> 'LOGGER')
                       ) AS daily_yield_kwh,
                       -- Month and lifetime totals come only from the station
                       -- meter. Summing inverter rows for these would be wrong
                       -- in a different way from the daily figure: a device
                       -- replaced mid-life resets its own lifetime counter, so
                       -- the sum silently drops.
                       MAX(lr.monthly_yield_kwh)
                           FILTER (WHERE d.device_type = 'LOGGER')
                           AS monthly_yield_kwh,
                       MAX(lr.total_yield_kwh)
                           FILTER (WHERE d.device_type = 'LOGGER')
                           AS lifetime_yield_kwh,
                       -- The vendor behind this branch's data. MIN over a set
                       -- that is single-valued in practice; a branch served by
                       -- two vendors would be a mapping fault, not a state to
                       -- render.
                       MIN(d.vendor_key)       AS vendor_key,
                       MAX(lr.reading_time)    AS last_seen_at,
                       -- Online if ANY device reported inside the window its
                       -- OWN vendor is polled at. Aggregating the timestamp
                       -- first and comparing once would force a single
                       -- threshold onto a mixed-vendor site.
                       BOOL_OR(
                           lr.reading_time
                           >= now() - make_interval(mins => {staleness_sql})
                       ) AS is_online
                FROM devices d
                JOIN latest_reading lr ON lr.device_id = d.device_id
                WHERE d.is_active
                GROUP BY d.store_id
            ),
            store_yield AS (
                -- Specific yield: today's kWh per installed kWp. Needs no
                -- irradiance data, so unlike PR% it is available fleet-wide.
                SELECT st2.store_id,
                       (st2.daily_yield_kwh / s2.installed_kwp)::numeric
                           AS specific_yield
                FROM store_telemetry st2
                JOIN stores s2 ON s2.store_id = st2.store_id
                WHERE s2.is_active
                  AND s2.installed_kwp IS NOT NULL
                  AND s2.installed_kwp > 0
                  AND st2.daily_yield_kwh IS NOT NULL
            ),
            fleet_yield AS (
                -- The reference every branch is judged against. Deliberately
                -- computed over the WHOLE fleet, ignoring the caller's filter:
                -- if the median moved when someone filtered to one region, the
                -- same branch would change colour depending on how it was
                -- looked at.
                --
                -- Median, not mean, and non-producing sites excluded. A handful
                -- of dead branches would otherwise drag the reference down far
                -- enough to make the rest of the fleet look healthy, which is
                -- exactly backwards.
                SELECT PERCENTILE_CONT(0.5)
                           WITHIN GROUP (ORDER BY specific_yield)::numeric
                           AS median_yield,
                       COUNT(*) AS peer_count
                FROM store_yield
                WHERE specific_yield > 0
            ),
            store_irradiance AS (
                -- MVP approximation of the Virtual Pyranometer baseline: mean
                -- plane-of-array irradiance today, converted from W/m2 to an
                -- equivalent number of peak-sun hours. The analytics engine
                -- replaces this with a properly integrated figure; until then
                -- it is good enough to rank sites and bad enough to warrant
                -- this comment.
                SELECT w.store_id,
                       AVG(w.poa_irradiance) FILTER (WHERE w.poa_irradiance > 0) AS avg_poa,
                       COUNT(*) FILTER (WHERE w.poa_irradiance > 0)              AS sample_count
                FROM weather_data w
                WHERE w."time" >= date_trunc('day', now())
                GROUP BY w.store_id
            ),
            store_devices AS (
                -- A device row is only ever created when a vendor first
                -- delivers a reading for that branch, so its existence is the
                -- record of whether this site has EVER reported — a question
                -- the 24-hour telemetry window above cannot answer.
                SELECT d.store_id, COUNT(*) AS device_count
                FROM devices d
                WHERE d.is_active
                GROUP BY d.store_id
            ),
            store_alerts AS (
                SELECT a.store_id,
                       COUNT(*) AS open_count,
                       BOOL_OR(a.alert_type = 'STRING_VARIANCE') AS has_string_anomaly,
                       BOOL_OR(a.severity = 'CRITICAL') AS has_critical,
                       MIN(CASE a.severity
                               WHEN 'CRITICAL' THEN 1
                               WHEN 'MAJOR'    THEN 2
                               ELSE 3
                           END) AS worst_rank
                FROM alerts a
                WHERE a.status <> 'RESOLVED'
                GROUP BY a.store_id
            )
            SELECT s.*,
                   st.active_power_kw,
                   st.daily_yield_kwh,
                   st.last_seen_at,
                   si.avg_poa,
                   si.sample_count,
                   COALESCE(sa.open_count, 0)              AS open_alert_count,
                   COALESCE(sa.has_string_anomaly, FALSE)  AS has_string_anomaly,
                   COALESCE(sa.has_critical, FALSE)        AS has_critical,
                   sa.worst_rank,
                   st.monthly_yield_kwh,
                   st.lifetime_yield_kwh,
                   st.vendor_key,
                   COALESCE(sd.device_count, 0)            AS device_count,
                   sy.specific_yield,
                   fy.median_yield,
                   fy.peer_count,
                   COALESCE(st.is_online, FALSE)           AS is_online
            FROM stores s
            LEFT JOIN store_telemetry  st ON st.store_id = s.store_id
            LEFT JOIN store_irradiance si ON si.store_id = s.store_id
            LEFT JOIN store_alerts     sa ON sa.store_id = s.store_id
            LEFT JOIN store_devices    sd ON sd.store_id = s.store_id
            LEFT JOIN store_yield      sy ON sy.store_id = s.store_id
            CROSS JOIN fleet_yield fy
            WHERE {where_sql}
            ORDER BY s.store_code
            """
        )

        try:
            result = await self._session.execute(sql, params)
            rows = result.mappings().all()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to load fleet status", cause=exc) from exc

        out: list[StoreWithStatus] = []
        for row in rows:
            performance_ratio = _performance_ratio(
                daily_yield_kwh=row["daily_yield_kwh"],
                installed_kwp=row["installed_kwp"],
                avg_poa=row["avg_poa"],
                sample_count=row["sample_count"],
            )
            specific_yield = _as_decimal(row["specific_yield"])
            yield_vs_peers = _yield_vs_peers(
                specific_yield=specific_yield,
                median_yield=_as_decimal(row["median_yield"]),
                peer_count=row["peer_count"],
                min_peers=min_peers,
            )
            has_ever_reported = int(row["device_count"]) > 0
            status = classify_pr_status(
                performance_ratio=performance_ratio,
                is_online=bool(row["is_online"]),
                has_string_anomaly=bool(row["has_string_anomaly"]),
                has_critical_alert=bool(row["has_critical"]),
                green_threshold=green_threshold,
                yield_vs_peers_pct=yield_vs_peers,
                yield_green_threshold=yield_threshold,
                has_ever_reported=has_ever_reported,
            )
            out.append(
                StoreWithStatus(
                    store=_to_store_from_mapping(row),
                    pr_status=status,
                    performance_ratio=performance_ratio,
                    active_power_kw=row["active_power_kw"],
                    daily_yield_kwh=row["daily_yield_kwh"],
                    last_seen_at=row["last_seen_at"],
                    is_online=bool(row["is_online"]),
                    has_string_anomaly=bool(row["has_string_anomaly"]),
                    open_alert_count=int(row["open_alert_count"]),
                    max_alert_severity=_severity_from_rank(row["worst_rank"]),
                    specific_yield_kwh_per_kwp=(
                        None
                        if specific_yield is None
                        else specific_yield.quantize(Decimal("0.001"))
                    ),
                    yield_vs_peers_pct=yield_vs_peers,
                    vendor_key=row["vendor_key"],
                    monthly_yield_kwh=row["monthly_yield_kwh"],
                    lifetime_yield_kwh=row["lifetime_yield_kwh"],
                    has_ever_reported=has_ever_reported,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    async def create_store(
        self,
        *,
        store_code: str,
        store_name: str,
        installed_kwp: Decimal,
        lat: Decimal,
        lng: Decimal,
        region: str | None = None,
        province: str | None = None,
        tariff_id: UUID | None = None,
        commissioned_at: date | None = None,
    ) -> Store:
        row = StoreORM(
            store_code=store_code,
            store_name=store_name,
            installed_kwp=installed_kwp,
            lat=lat,
            lng=lng,
            region=region,
            province=province,
            tariff_id=tariff_id,
            commissioned_at=commissioned_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            # Let the caller see a domain error, not a driver one — otherwise
            # every call site would need to know PostgreSQL's constraint names.
            if "uq_stores_store_code" in str(exc.orig):
                raise DuplicateStoreCodeError(store_code) from exc
            raise RepositoryError("Failed to create store", cause=exc) from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to create store", cause=exc) from exc

        await self._session.refresh(row)
        return _to_store(row)

    async def update_store(
        self,
        store_id: UUID,
        *,
        store_name: str | None = None,
        region: str | None = None,
        province: str | None = None,
        installed_kwp: Decimal | None = None,
        lat: Decimal | None = None,
        lng: Decimal | None = None,
        tariff_id: UUID | None = None,
        commissioned_at: date | None = None,
    ) -> Store:
        row = await self._session.get(StoreORM, store_id)
        if row is None:
            raise StoreNotFoundError(store_id=store_id)

        # None means "leave alone", so assign only what was supplied. Clearing a
        # field needs an explicit method rather than an overloaded None.
        updates: dict[str, Any] = {
            "store_name": store_name,
            "region": region,
            "province": province,
            "installed_kwp": installed_kwp,
            "lat": lat,
            "lng": lng,
            "tariff_id": tariff_id,
            "commissioned_at": commissioned_at,
        }
        for field, value in updates.items():
            if value is not None:
                setattr(row, field, value)

        try:
            await self._session.flush()
            await self._session.refresh(row)
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to update store {store_id}", cause=exc) from exc

        return _to_store(row)

    async def deactivate_store(self, store_id: UUID) -> Store:
        row = await self._session.get(StoreORM, store_id)
        if row is None:
            raise StoreNotFoundError(store_id=store_id)

        row.is_active = False
        try:
            await self._session.flush()
            await self._session.refresh(row)
        except SQLAlchemyError as exc:
            raise RepositoryError(
                f"Failed to deactivate store {store_id}", cause=exc
            ) from exc

        return _to_store(row)

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #

    async def ping(self) -> bool:
        try:
            await self._session.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _apply_filter(self, stmt: Select[Any], store_filter: StoreFilter) -> Select[Any]:
        """Translate a StoreFilter into WHERE clauses.

        All of it runs in the database. Filtering in Python would work today at
        153 stores and fail quietly as the fleet grows by ~200 a year.
        """
        if store_filter.is_active is not None:
            stmt = stmt.where(StoreORM.is_active.is_(store_filter.is_active))

        if store_filter.regions:
            stmt = stmt.where(StoreORM.region.in_(store_filter.regions))

        if store_filter.search:
            # Escape LIKE wildcards so a user searching for "50%" does not match
            # everything.
            needle = (
                store_filter.search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{needle}%"
            stmt = stmt.where(
                or_(
                    StoreORM.store_code.ilike(pattern, escape="\\"),
                    StoreORM.store_name.ilike(pattern, escape="\\"),
                    StoreORM.retail_store_code.ilike(pattern, escape="\\"),
                )
            )

        if store_filter.bbox is not None:
            bbox = store_filter.bbox
            stmt = stmt.where(
                StoreORM.lat.is_not(None),
                StoreORM.lng.is_not(None),
                StoreORM.lat.between(bbox.min_lat, bbox.max_lat),
                StoreORM.lng.between(bbox.min_lng, bbox.max_lng),
            )

        return stmt


# --------------------------------------------------------------------------- #
# Row -> domain conversion
# --------------------------------------------------------------------------- #


def _to_store(row: StoreORM) -> Store:
    return Store(
        store_id=row.store_id,
        store_code=row.store_code,
        store_name=row.store_name,
        retail_store_code=row.retail_store_code,
        region=row.region,
        province=row.province,
        address=row.address,
        installed_kwp=row.installed_kwp,
        lat=row.lat,
        lng=row.lng,
        tariff_id=row.tariff_id,
        rollout_phase=row.rollout_phase,
        monitoring_source=row.monitoring_source,
        commissioned_at=row.commissioned_at,
        battery_capacity_kwh=row.battery_capacity_kwh,
        capex_before_vat=row.capex_before_vat,
        capex_vat=row.capex_vat,
        capex_net=row.capex_net,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_store_from_mapping(row: Any) -> Store:
    """Same conversion for the raw-SQL path, which yields mappings not ORM rows."""
    return Store(
        store_id=row["store_id"],
        store_code=row["store_code"],
        store_name=row["store_name"],
        retail_store_code=row["retail_store_code"],
        region=row["region"],
        province=row["province"],
        address=row["address"],
        installed_kwp=row["installed_kwp"],
        lat=row["lat"],
        lng=row["lng"],
        tariff_id=row["tariff_id"],
        rollout_phase=row["rollout_phase"],
        monitoring_source=row["monitoring_source"],
        commissioned_at=row["commissioned_at"],
        battery_capacity_kwh=row["battery_capacity_kwh"],
        capex_before_vat=row["capex_before_vat"],
        capex_vat=row["capex_vat"],
        capex_net=row["capex_net"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_device(row: DeviceORM) -> Device:
    return Device(
        device_id=row.device_id,
        store_id=row.store_id,
        brand=row.brand,
        model=row.model,
        serial_number=row.serial_number,
        device_type=DeviceType(row.device_type),
        measurement_basis=MeasurementBasis(row.measurement_basis),
        vendor_key=row.vendor_key,
        capacity_kw=row.capacity_kw,
        mppt_count=row.mppt_count,
        installed_at=row.installed_at,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _severity_from_rank(rank: int | None) -> AlertSeverity | None:
    if rank is None:
        return None
    return {1: AlertSeverity.CRITICAL, 2: AlertSeverity.MAJOR}.get(
        int(rank), AlertSeverity.MINOR
    )


def _vendor_staleness_case(
    thresholds: dict[str, int],
) -> tuple[str, dict[str, Any]]:
    """Build a SQL CASE mapping a device's vendor to its staleness threshold.

    Vendor names come from settings, never from a request, but they are still
    bound as parameters rather than interpolated: the SQL string is assembled
    from fixed text only.
    """
    params: dict[str, Any] = {}
    if not thresholds:
        return ":offline_minutes", params

    # Every branch is cast explicitly. Without it PostgreSQL resolves the
    # untyped bind parameters in a CASE to text, and make_interval(mins => text)
    # does not exist — the query fails outright rather than misbehaving, but
    # only once a vendor override is actually configured.
    #
    # CAST(...) rather than the shorter ::int: SQLAlchemy'''s text() parser cannot
    # tell ":name::type" from the start of a second parameter, and drops the
    # bind silently.
    branches: list[str] = []
    for index, (vendor, minutes) in enumerate(sorted(thresholds.items())):
        params[f"vendor_key_{index}"] = vendor.lower()
        params[f"vendor_mins_{index}"] = minutes
        branches.append(
            f"WHEN lower(COALESCE(d.vendor_key, '')) = :vendor_key_{index} "
            f"THEN CAST(:vendor_mins_{index} AS INTEGER)"
        )
    return (
        "CASE " + " ".join(branches) + " ELSE CAST(:offline_minutes AS INTEGER) END",
        params,
    )


def _as_decimal(value: Any) -> Decimal | None:
    """Normalise a numeric column to Decimal.

    PERCENTILE_CONT returns double precision while NUMERIC division returns
    Decimal, and mixing the two raises TypeError on the first comparison.
    """
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _yield_vs_peers(
    *,
    specific_yield: Decimal | None,
    median_yield: Decimal | None,
    peer_count: int | None,
    min_peers: int,
) -> Decimal | None:
    """Today's specific yield as a percentage of the fleet median.

    Returns None rather than a number whenever the comparison would be
    meaningless:

    * no capacity or no reading for this branch — nothing to compare;
    * fewer than ``min_peers`` branches reporting — the median would be an
      accident of which sites happened to answer;
    * a median at or below zero — before first light every branch reads zero,
      and dividing by that would repaint the whole fleet at once.

    A branch producing nothing while its peers produce is NOT excluded here:
    zero against a healthy median is the signal, not an absence of one.
    """
    if specific_yield is None or median_yield is None:
        return None
    if not peer_count or int(peer_count) < min_peers:
        return None
    if median_yield <= 0:
        return None
    return (specific_yield / median_yield * Decimal("100")).quantize(Decimal("0.1"))


def _performance_ratio(
    *,
    daily_yield_kwh: Decimal | None,
    installed_kwp: Decimal | None,
    avg_poa: float | None,
    sample_count: int | None,
) -> Decimal | None:
    """PR% = actual yield / theoretical yield at the measured irradiance.

    Returns None whenever any input is missing. That is deliberate: a site with
    no irradiance data has an *unknown* performance ratio, and substituting zero
    would paint a healthy store red.
    """
    if daily_yield_kwh is None or installed_kwp is None:
        return None
    if avg_poa is None or not sample_count:
        return None
    if installed_kwp <= 0 or avg_poa <= 0:
        return None

    # Peak-sun-hour equivalent: mean W/m2 over the samples, scaled by how much
    # of the day they cover, against the 1000 W/m2 STC reference.
    peak_sun_hours = Decimal(str(avg_poa)) / Decimal("1000") * Decimal(sample_count)
    if peak_sun_hours <= 0:
        return None

    theoretical_kwh = installed_kwp * peak_sun_hours
    if theoretical_kwh <= 0:
        return None

    return (daily_yield_kwh / theoretical_kwh * Decimal("100")).quantize(Decimal("0.01"))


def _build_status_filter(store_filter: StoreFilter) -> tuple[str, dict[str, Any]]:
    """Build the WHERE fragment for the raw-SQL fleet query.

    Values are always bound parameters, never interpolated — only fixed clause
    text is assembled here, so no caller input reaches the SQL string.
    """
    clauses: list[str] = ["TRUE"]
    params: dict[str, Any] = {}

    if store_filter.is_active is not None:
        clauses.append("s.is_active = :is_active")
        params["is_active"] = store_filter.is_active

    if store_filter.regions:
        clauses.append("s.region = ANY(:regions)")
        params["regions"] = list(store_filter.regions)

    if store_filter.search:
        clauses.append(
            "(s.store_code ILIKE :search OR s.store_name ILIKE :search"
            " OR s.retail_store_code ILIKE :search)"
        )
        needle = (
            store_filter.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        params["search"] = f"%{needle}%"

    if store_filter.bbox is not None:
        clauses.append(
            "s.lat BETWEEN :min_lat AND :max_lat AND s.lng BETWEEN :min_lng AND :max_lng"
        )
        params.update(
            min_lat=store_filter.bbox.min_lat,
            max_lat=store_filter.bbox.max_lat,
            min_lng=store_filter.bbox.min_lng,
            max_lng=store_filter.bbox.max_lng,
        )

    return " AND ".join(clauses), params
