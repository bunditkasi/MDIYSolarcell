"""Vendor ingestion interface — the fourth seam.

MR.DIY's fleet reports through several vendor clouds that agree on nothing: not
authentication, not units, not pagination, not how a "device" is identified, and
not whether per-panel data exists at all. This module is the contract that hides
all of it. Everything downstream — the worker, the analytics, the repository —
sees one normalised shape.

RULES FOR AN ADAPTER
--------------------
1. Return the DTOs below. Never leak vendor JSON, an httpx.Response, or a
   vendor-specific status string upward.
2. NORMALISE UNITS AT THE BOUNDARY. This is the single most common source of
   silent corruption in this kind of integration: two vendors report "power"
   and mean different things by it. Convert once, here, and state the source
   unit in a comment. (Huawei's `active_power` is documented as kW and actually
   returns W — see huawei.py.)
3. Fetch credentials through ``SecretsProviderInterface`` at the moment of use.
   Never accept a password as a constructor argument, never log one, never put
   one in an exception message or a URL.
4. Respect the vendor's quota. Declare ``max_sites_per_call`` honestly — the
   scheduler batches against it, and getting it wrong is how a month's quota
   disappears in an afternoon.
5. Raise ``IngestionError`` and its subclasses. The worker's retry and
   back-off policy is written against those, not against httpx.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.domain.models import MeasurementBasis

__all__ = [
    "AuthenticationError",
    "DeviceReading",
    "IngestionError",
    "InverterDataSourceInterface",
    "PanelReading",
    "QuotaExceededError",
    "SiteReading",
    "TransientVendorError",
    "VendorAlarm",
    "VendorDevice",
    "VendorSite",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class IngestionError(Exception):
    """Base class for every ingestion failure."""


class AuthenticationError(IngestionError):
    """Credentials were rejected, or a session could not be established.

    Not retryable with the same credentials — retrying a bad password is how
    accounts get locked out.
    """


class QuotaExceededError(IngestionError):
    """The vendor refused the call on quota grounds (HTTP 429).

    Must NOT be retried inside the current run. The remaining monthly budget is
    a shared resource; burning it on retries costs visibility for the rest of
    the month.
    """


class TransientVendorError(IngestionError):
    """A timeout, a 5xx, or a malformed response. Safe to retry with back-off."""


# --------------------------------------------------------------------------- #
# Normalised data transfer objects
#
# Units are fixed here and are not negotiable: kW for power, kWh for energy,
# volts, amps, and timezone-aware UTC datetimes.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VendorSite:
    """A plant/station as the vendor knows it."""

    vendor_site_id: str
    name: str
    capacity_kwp: Decimal | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    #: Usable battery storage. None where the vendor does not report it —
    #: Huawei's station list has no such field, so those branches stay blank
    #: rather than being shown a zero they did not report.
    battery_capacity_kwh: Decimal | None = None
    #: Date the site was connected to the grid. The nearest thing any vendor
    #: publishes to a commissioning date, and more reliable than the workbook
    #: for branches the roster has not caught up with.
    grid_tied_on: date | None = None


@dataclass(frozen=True, slots=True)
class VendorDevice:
    vendor_site_id: str
    #: Serial number. The join key against ``devices.serial_number``.
    serial_number: str
    name: str | None = None
    model: str | None = None
    capacity_kw: Decimal | None = None
    #: None where the vendor does not report it — microinverters have no MPPT.
    mppt_count: int | None = None


@dataclass(frozen=True, slots=True)
class SiteReading:
    """Site-level snapshot. The cheap, bulk-fetchable call that feeds the map."""

    vendor_site_id: str
    measured_at: datetime
    active_power_kw: Decimal | None = None
    daily_yield_kwh: Decimal | None = None
    #: Month to date, as the VENDOR counts it. Taken rather than derived: our
    #: own history starts when ingestion was switched on, so summing it would
    #: under-report every branch until a full month has passed.
    monthly_yield_kwh: Decimal | None = None
    total_yield_kwh: Decimal | None = None
    #: Normalised to ONLINE / OFFLINE / FAULT / UNKNOWN. Vendor vocabularies
    #: differ wildly ("onGrid", "Normal", "standby", "waitLight"...) and the
    #: mapping is the adapter's job, not the caller's.
    status: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DeviceReading:
    """Inverter-level snapshot. Maps onto ``telemetry_raw``."""

    serial_number: str
    measured_at: datetime
    active_power_kw: Decimal | None = None
    daily_yield_kwh: Decimal | None = None
    #: Month to date. Only station-level sources report it; individual
    #: inverters do not, and leave it None.
    monthly_yield_kwh: Decimal | None = None
    total_yield_kwh: Decimal | None = None
    grid_voltage: Decimal | None = None
    grid_current: Decimal | None = None
    status_code: int | None = None
    #: Untouched vendor response, stored for debugging field-mapping bugs.
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PanelReading:
    """One string or one panel. Maps onto ``telemetry_string``.

    For microinverters ``mppt_index`` is 0 and ``string_index`` is the panel
    number, with voltage and current None — those vendors do not report them.
    """

    serial_number: str
    measured_at: datetime
    mppt_index: int
    string_index: int
    pv_voltage: Decimal | None = None
    pv_current: Decimal | None = None
    pv_power_kw: Decimal | None = None


@dataclass(frozen=True, slots=True)
class VendorAlarm:
    vendor_site_id: str
    serial_number: str | None
    raised_at: datetime
    #: Vendor's own severity, normalised to CRITICAL / MAJOR / MINOR.
    severity: str
    message: str
    vendor_code: str | None = None


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #


class InverterDataSourceInterface(ABC):
    """One vendor cloud account."""

    #: Stable identifier written to ``devices.vendor_key``.
    vendor_key: str

    #: Whether this vendor's hardware yields per-string I/V or per-panel power.
    #: The analytics layer branches on it.
    measurement_basis: MeasurementBasis

    #: How many sites one bulk site-reading call accepts. The scheduler uses
    #: this to batch, which is what keeps a 127-site fleet inside a
    #: 10,000-call monthly budget.
    max_sites_per_call: int = 1

    #: Whether per-panel detail is available at all.
    supports_panel_data: bool = True

    @abstractmethod
    async def authenticate(self) -> None:
        """Establish a session, reusing a cached token where the vendor allows.

        Implementations must NOT re-authenticate per call. On Atmoce the session
        token lasts 30 days and generating one is itself a metered request.
        """

    @abstractmethod
    async def list_sites(self) -> list[VendorSite]:
        """Every site visible to this account."""

    @abstractmethod
    async def list_devices(self, vendor_site_id: str) -> list[VendorDevice]:
        """Devices at one site."""

    @abstractmethod
    async def fetch_site_readings(self, vendor_site_ids: list[str]) -> list[SiteReading]:
        """Latest site-level data, batched.

        Callers pass at most ``max_sites_per_call`` ids. This is the call that
        runs every poll cycle, so it must be the cheapest one available.
        """

    @abstractmethod
    async def fetch_device_readings(self, vendor_site_id: str) -> list[DeviceReading]:
        """Latest inverter-level data for one site."""

    @abstractmethod
    async def fetch_panel_readings(self, vendor_site_id: str) -> list[PanelReading]:
        """Latest per-string or per-panel data for one site.

        Returns an empty list when ``supports_panel_data`` is False. Callers
        must treat empty as "not available", never as "everything is zero".
        """

    @abstractmethod
    async def fetch_alarms(self, vendor_site_id: str) -> list[VendorAlarm]:
        """Active vendor-side alarms for one site."""

    async def close(self) -> None:
        """Release the HTTP client. Overridden where one is held."""
        return None

    async def __aenter__(self) -> InverterDataSourceInterface:
        await self.authenticate()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
