"""HTTP response models.

Kept separate from the domain dataclasses on purpose. The domain describes the
business; these describe the wire. Letting the frontend bind directly to domain
objects would mean any internal rename becomes a breaking API change.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import AlertSeverity, PRStatus, Store, StoreWithStatus

T = TypeVar("T")

__all__ = [
    "DeviceOut",
    "MapResponse",
    "PagedResponse",
    "StoreOut",
    "StoreStatusOut",
    "ThresholdsOut",
]


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_id: UUID
    store_code: str
    retail_store_code: str | None = None
    store_name: str
    region: str | None = None
    province: str | None = None
    address: str | None = None
    installed_kwp: Decimal | None = None
    lat: Decimal | None = None
    lng: Decimal | None = None
    rollout_phase: int | None = None
    monitoring_source: str | None = None
    commissioned_at: date | None = None
    capex_net: Decimal | None = None
    is_active: bool

    @classmethod
    def from_domain(cls, store: Store) -> StoreOut:
        return cls(
            store_id=store.store_id,
            store_code=store.store_code,
            retail_store_code=store.retail_store_code,
            store_name=store.store_name,
            region=store.region,
            province=store.province,
            address=store.address,
            installed_kwp=store.installed_kwp,
            lat=store.lat,
            lng=store.lng,
            rollout_phase=store.rollout_phase,
            monitoring_source=store.monitoring_source,
            commissioned_at=store.commissioned_at,
            capex_net=store.capex_net,
            is_active=store.is_active,
        )


class StoreStatusOut(BaseModel):
    """One map pin."""

    store_id: UUID
    store_code: str
    store_name: str
    region: str | None = None
    province: str | None = None
    lat: Decimal
    lng: Decimal
    installed_kwp: Decimal | None = None
    #: True when capacity or position is still missing — rendered as a
    #: "New" badge rather than shown as a zero.
    is_incomplete: bool = False
    pr_status: PRStatus
    performance_ratio: Decimal | None = None
    active_power_kw: Decimal | None = None
    daily_yield_kwh: Decimal | None = None
    last_seen_at: datetime | None = None
    is_online: bool
    has_string_anomaly: bool
    open_alert_count: int
    max_alert_severity: AlertSeverity | None = None
    #: Today's kWh per installed kWp.
    specific_yield_kwh_per_kwp: Decimal | None = None
    #: The above as a percentage of the fleet median for the same day. When
    #: ``performance_ratio`` is null this is what determined ``pr_status``, so
    #: the UI can always show the evidence behind a pin's colour.
    yield_vs_peers_pct: Decimal | None = None
    has_ever_reported: bool = True

    @classmethod
    def from_domain(cls, item: StoreWithStatus) -> StoreStatusOut:
        store = item.store
        # Callers must filter out locationless stores first; asserting here
        # turns a silent None into an obvious failure during development.
        assert store.lat is not None and store.lng is not None
        return cls(
            store_id=store.store_id,
            store_code=store.store_code,
            store_name=store.store_name,
            region=store.region,
            province=store.province,
            lat=store.lat,
            lng=store.lng,
            installed_kwp=store.installed_kwp,
            is_incomplete=store.is_incomplete,
            pr_status=item.pr_status,
            performance_ratio=item.performance_ratio,
            active_power_kw=item.active_power_kw,
            daily_yield_kwh=item.daily_yield_kwh,
            last_seen_at=item.last_seen_at,
            is_online=item.is_online,
            has_string_anomaly=item.has_string_anomaly,
            open_alert_count=item.open_alert_count,
            max_alert_severity=item.max_alert_severity,
            specific_yield_kwh_per_kwp=item.specific_yield_kwh_per_kwp,
            yield_vs_peers_pct=item.yield_vs_peers_pct,
            has_ever_reported=item.has_ever_reported,
        )


class FleetRowOut(BaseModel):
    """One row of the fleet table.

    Separate from ``StoreStatusOut`` rather than an extension of it because the
    two answer different questions. A map pin must have a position, and
    StoreStatusOut asserts one; a fleet LIST has no such requirement, and
    dropping the 13 branches whose coordinates are still unknown would be
    reporting a data gap as if those branches did not exist.
    """

    store_id: UUID
    #: The vendor cloud this branch reports through.
    source: str | None = None
    pr_status: PRStatus
    store_code: str
    store_name: str
    province: str | None = None
    #: Grid connection date — the vendor's, or the workbook's where it has one.
    commissioned_at: date | None = None
    installed_kwp: Decimal | None = None
    battery_capacity_kwh: Decimal | None = None
    active_power_kw: Decimal | None = None
    daily_yield_kwh: Decimal | None = None
    monthly_yield_kwh: Decimal | None = None
    lifetime_yield_kwh: Decimal | None = None
    last_seen_at: datetime | None = None
    is_online: bool
    is_incomplete: bool = False
    has_location: bool = True
    #: False when no vendor account has ever delivered a reading. Such a branch
    #: is waiting on access or commissioning, not failing.
    has_ever_reported: bool = True
    open_alert_count: int = 0

    @classmethod
    def from_domain(cls, item: StoreWithStatus) -> FleetRowOut:
        store = item.store
        return cls(
            store_id=store.store_id,
            source=item.vendor_key,
            pr_status=item.pr_status,
            store_code=store.store_code,
            store_name=store.store_name,
            province=store.province,
            commissioned_at=store.commissioned_at,
            installed_kwp=store.installed_kwp,
            battery_capacity_kwh=store.battery_capacity_kwh,
            active_power_kw=item.active_power_kw,
            daily_yield_kwh=item.daily_yield_kwh,
            monthly_yield_kwh=item.monthly_yield_kwh,
            lifetime_yield_kwh=item.lifetime_yield_kwh,
            last_seen_at=item.last_seen_at,
            is_online=item.is_online,
            is_incomplete=store.is_incomplete,
            has_location=store.has_location,
            has_ever_reported=item.has_ever_reported,
            open_alert_count=item.open_alert_count,
        )


class FleetResponse(BaseModel):
    rows: list[FleetRowOut]
    thresholds: ThresholdsOut


class ThresholdsOut(BaseModel):
    """Classification thresholds, served to the frontend.

    The map reads these instead of hard-coding 75, so backend and frontend
    cannot disagree about what counts as a green pin.
    """

    pr_green_threshold: Decimal
    string_variance_threshold_pct: Decimal
    device_offline_after_minutes: int
    #: Specific-yield threshold as a percentage of the fleet median, used when
    #: no irradiance baseline exists and PR% cannot be computed.
    yield_green_threshold_pct: Decimal


class MapResponse(BaseModel):
    stores: list[StoreStatusOut]
    thresholds: ThresholdsOut
    #: Stores matching the filter that have no coordinates and so cannot be
    #: drawn. Surfaced rather than silently dropped — 35 of the current 153
    #: sites are in this state, and hiding that would look like data loss.
    stores_without_location: int = Field(
        default=0,
        description="Matching stores omitted because lat/lng are unknown.",
    )


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: UUID
    store_id: UUID
    brand: str
    model: str | None = None
    serial_number: str
    device_type: str
    #: STRING or PANEL — tells the UI whether per-string I/V exists for this
    #: device, so it does not render empty voltage/current charts for
    #: microinverters that never report them.
    measurement_basis: str
    vendor_key: str | None = None
    capacity_kw: Decimal | None = None
    mppt_count: int | None = None
    is_active: bool


class PagedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool
