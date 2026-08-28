"""SQLAlchemy table mappings.

These mirror db/init/02_schema.sql. They live in the infrastructure layer and
must never be imported by ``app.domain`` or ``app.api`` — repositories convert
between these rows and domain objects, and that conversion is the boundary that
keeps Phase 2 cheap.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "AlertORM",
    "Base",
    "DataAdapterORM",
    "DeviceORM",
    "StoreORM",
    "TariffORM",
    "TelemetryRawORM",
    "TelemetryStringORM",
    "VendorSiteLinkORM",
    "WeatherDataORM",
]


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


class TariffORM(Base):
    __tablename__ = "tariffs"

    tariff_id: Mapped[UUID] = _uuid_pk()
    tariff_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tariff_code: Mapped[str] = mapped_column(String(16), nullable=False)
    utility: Mapped[str] = mapped_column(String(8), nullable=False)
    on_peak_rate: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    off_peak_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    demand_charge_rate: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="THB")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("utility", "tariff_code", "effective_from", name="uq_tariffs_code_from"),
    )


class StoreORM(Base):
    __tablename__ = "stores"

    store_id: Mapped[UUID] = _uuid_pk()
    store_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    retail_store_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    store_name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64))
    province: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)
    #: NULL where the capacity is not yet known. See migration 0003.
    installed_kwp: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # Nullable: 35 of the 153 real sites have no coordinates yet.
    lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    tariff_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tariffs.tariff_id", ondelete="SET NULL")
    )
    rollout_phase: Mapped[int | None] = mapped_column(SmallInteger)
    monitoring_source: Mapped[str | None] = mapped_column(String(64))
    commissioned_at: Mapped[date | None] = mapped_column(Date)
    capex_before_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    capex_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    capex_net: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    battery_capacity_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    devices: Mapped[list[DeviceORM]] = relationship(
        back_populates="store", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        Index("idx_stores_region", "region"),
        Index("idx_stores_is_active", "is_active"),
        Index("idx_stores_lat_lng", "lat", "lng"),
        CheckConstraint("(lat IS NULL) = (lng IS NULL)", name="ck_stores_latlng_paired"),
    )


class DeviceORM(Base):
    __tablename__ = "devices"

    device_id: Mapped[UUID] = _uuid_pk()
    store_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("stores.store_id", ondelete="CASCADE"),
        nullable=False,
    )
    brand: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    serial_number: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    device_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="INVERTER"
    )
    measurement_basis: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="STRING"
    )
    vendor_key: Mapped[str | None] = mapped_column(String(64))
    capacity_kw: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    mppt_count: Mapped[int | None] = mapped_column(SmallInteger)
    installed_at: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    store: Mapped[StoreORM] = relationship(back_populates="devices", lazy="raise")

    __table_args__ = (
        Index("idx_devices_store_id", "store_id"),
        Index("idx_devices_brand", "brand"),
    )


class DataAdapterORM(Base):
    __tablename__ = "data_adapters"

    adapter_id: Mapped[UUID] = _uuid_pk()
    device_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    adapter_type: Mapped[str] = mapped_column(String(16), nullable=False)
    vendor_key: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(Text)
    #: Vault lookup key ONLY — never a credential. See db/init/02_schema.sql.
    secrets_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    sync_interval_min: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15"
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VendorSiteLinkORM(Base):
    """A human-decided mapping from a vendor site to one of our branches.

    Only for sites whose name carries no branch code. See migration 0004 for
    why these are explicit rows rather than a looser matching rule.
    """

    __tablename__ = "vendor_site_links"

    link_id: Mapped[UUID] = _uuid_pk()
    vendor_key: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor_site_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("stores.store_id", ondelete="CASCADE"),
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "vendor_key", "vendor_site_id", name="uq_vendor_site_link"
        ),
    )


class AlertORM(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[UUID] = _uuid_pk()
    store_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("stores.store_id", ondelete="CASCADE"),
        nullable=False,
    )
    device_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("devices.device_id", ondelete="CASCADE")
    )
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="OPEN")
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_alerts_store_created", "store_id", "created_at"),)


# ---------------------------------------------------------------------------
# Hypertables
#
# Mapped for completeness and for the ingestion path's bulk upserts. Analytical
# reads go through hand-written SQL in the repository instead, because
# TimescaleDB features such as time_bucket() and DISTINCT ON have no ORM
# equivalent worth the indirection.
# ---------------------------------------------------------------------------


class TelemetryRawORM(Base):
    __tablename__ = "telemetry_raw"

    time: Mapped[datetime] = mapped_column(
        "time", DateTime(timezone=True), primary_key=True, nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    active_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    daily_yield_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    monthly_yield_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    total_yield_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    grid_voltage: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    grid_current: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    status_code: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TelemetryStringORM(Base):
    __tablename__ = "telemetry_string"

    time: Mapped[datetime] = mapped_column(
        "time", DateTime(timezone=True), primary_key=True, nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    mppt_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    string_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    pv_voltage: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    pv_current: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    pv_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WeatherDataORM(Base):
    __tablename__ = "weather_data"

    time: Mapped[datetime] = mapped_column(
        "time", DateTime(timezone=True), primary_key=True, nullable=False
    )
    store_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("stores.store_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    ghi: Mapped[float | None] = mapped_column(Float)
    poa_irradiance: Mapped[float | None] = mapped_column(Float)
    ambient_temp: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="solcast")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
