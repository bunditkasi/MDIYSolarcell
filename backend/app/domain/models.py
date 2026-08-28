"""Domain entities.

PHASE 2 CONTRACT — this module must stay importable with no database present.

Nothing here may import SQLAlchemy, asyncpg, FastAPI, or anything else tied to a
storage engine or transport. These are plain Python values that the corporate IT
team can keep unchanged while swapping PostgreSQL for the enterprise SQL server.

Enforced by a test:  tests/test_domain_purity.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

__all__ = [
    "AdapterType",
    "Alert",
    "AlertSeverity",
    "AlertStatus",
    "AlertWithStore",
    "AlertType",
    "DataAdapter",
    "Device",
    "DeviceType",
    "EnergyBucket",
    "MeasurementBasis",
    "PRStatus",
    "PanelSnapshot",
    "Store",
    "StoreWithStatus",
    "Tariff",
    "Utility",
]


# ---------------------------------------------------------------------------
# Enumerations
#
# Values match the CHECK constraints in db/init/02_schema.sql exactly. They are
# str-backed so they serialise straight to JSON and compare cleanly against the
# strings the database returns.
# ---------------------------------------------------------------------------


class PRStatus(str, Enum):
    """Map pin colour, driven by Performance Ratio.

    Per specification section 3:
        GREEN  — PR >= threshold (75%)
        YELLOW — PR < threshold, or a string anomaly is present
        RED    — device offline / critical alert

    UNKNOWN is not in the spec but is necessary in practice: without a Solcast
    reading there is no irradiance baseline, so PR cannot be computed at all.
    Rendering that case as RED would send technicians to healthy sites.
    """

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


class DeviceType(str, Enum):
    INVERTER = "INVERTER"
    MICROINVERTER = "MICROINVERTER"
    METER = "METER"
    LOGGER = "LOGGER"
    WEATHER_STATION = "WEATHER_STATION"


class MeasurementBasis(str, Enum):
    """What per-panel data the hardware actually reports.

    This is not cosmetic metadata — it selects the fault-detection rule.

    STRING: panels wired in series into an MPPT, with per-string voltage and
        current available (Huawei FusionSolar). The specification's
        Intra-String Peer Comparison applies as written.

    PANEL: one microinverter per panel (Atmoce). The Atmoce Cloud API exposes
        no PV voltage and no PV current at all — only per-branch power — so
        the comparison must be made on power, between panels at the same site.
    """

    STRING = "STRING"
    PANEL = "PANEL"


class AdapterType(str, Enum):
    API = "API"
    SCRAPER = "SCRAPER"


class AlertSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"

    @property
    def rank(self) -> int:
        """Lower is worse. Lets callers take ``min()`` to find the worst alert."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[AlertSeverity, int] = {
    AlertSeverity.CRITICAL: 1,
    AlertSeverity.MAJOR: 2,
    AlertSeverity.MINOR: 3,
}


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AlertType(str, Enum):
    STRING_VARIANCE = "STRING_VARIANCE"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    LOW_PR = "LOW_PR"
    DATA_GAP = "DATA_GAP"
    ADAPTER_FAILURE = "ADAPTER_FAILURE"


class Utility(str, Enum):
    PEA = "PEA"
    MEA = "MEA"


# ---------------------------------------------------------------------------
# Entities
#
# Frozen so a repository cannot hand out an object that callers mutate behind
# its back, and so they are safe to cache.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tariff:
    tariff_id: UUID
    tariff_name: str
    #: Official utility category, e.g. "3.2.2".
    tariff_code: str
    utility: Utility
    on_peak_rate: Decimal
    #: None where the published source gives no off-peak figure. Callers must
    #: handle this rather than substituting zero.
    off_peak_rate: Decimal | None
    demand_charge_rate: Decimal
    currency: str
    effective_from: date
    effective_to: date | None


@dataclass(frozen=True, slots=True)
class Store:
    store_id: UUID
    #: 4-letter site abbreviation (e.g. "PLBK"). The natural business key.
    store_code: str
    store_name: str
    #: Retail branch code (e.g. "B105"). Sparse in the source roster, so it is
    #: an alternate identifier only — never assume it is present.
    retail_store_code: str | None
    region: str | None
    province: str | None
    address: str | None
    #: None when nobody has recorded the capacity yet — a branch a vendor
    #: reported before the roster caught up. Callers must not substitute
    #: zero: it would corrupt every kWh/kWp comparison.
    installed_kwp: Decimal | None
    #: None when the site's position is not yet known. Always None or set as a
    #: pair with ``lng`` — the database enforces this.
    lat: Decimal | None
    lng: Decimal | None
    tariff_id: UUID | None
    rollout_phase: int | None
    monitoring_source: str | None
    commissioned_at: date | None
    #: Usable battery storage. Null where the vendor publishes no such field
    #: (Huawei) — never substitute zero, which reads as "no battery fitted".
    battery_capacity_kwh: Decimal | None
    capex_before_vat: Decimal | None
    capex_vat: Decimal | None
    capex_net: Decimal | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @property
    def has_location(self) -> bool:
        """True when this store can be plotted on the map."""
        return self.lat is not None and self.lng is not None

    @property
    def is_incomplete(self) -> bool:
        """True when key facts are still missing.

        Surfaced in the UI as a "New" badge so an unsized branch reads as
        awaiting data rather than as a branch producing nothing.
        """
        return self.installed_kwp is None or not self.has_location


@dataclass(frozen=True, slots=True)
class Device:
    device_id: UUID
    store_id: UUID
    brand: str
    model: str | None
    serial_number: str
    device_type: DeviceType
    #: Which comparison rule applies to this device. See MeasurementBasis.
    measurement_basis: MeasurementBasis
    #: Vendor cloud this device reports through, e.g. "atmoce", "huawei".
    vendor_key: str | None
    capacity_kw: Decimal | None
    #: None for microinverters, which have no MPPT.
    mppt_count: int | None
    installed_at: date | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DataAdapter:
    adapter_id: UUID
    device_id: UUID
    adapter_type: AdapterType
    vendor_key: str
    endpoint_url: str | None
    #: Lookup key for SecretsProviderInterface — NEVER the credential itself.
    secrets_ref: str
    sync_interval_min: int
    is_enabled: bool
    last_sync_at: datetime | None
    last_sync_status: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: UUID
    store_id: UUID
    device_id: UUID | None
    alert_type: AlertType
    severity: AlertSeverity
    message: str
    status: AlertStatus
    details: dict[str, object]
    created_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class EnergyBucket:
    """Energy produced in one time bucket, for the reports table."""

    #: Start of the bucket: the day, the first of the month, or 1 January.
    period: date
    produced_kwh: Decimal | None
    #: Devices that contributed. A bucket built from 2 of 6 inverters is not
    #: comparable with a complete one, so the count travels with the number.
    device_count: int
    #: Readings behind the bucket. A day with 4 samples is not a full day, and
    #: presenting it next to a complete day without saying so invites a wrong
    #: conclusion about a dip.
    sample_count: int


@dataclass(frozen=True, slots=True)
class PanelSnapshot:
    """One panel or string on one day, with its standing against its peers."""

    device_id: UUID
    serial_number: str
    mppt_index: int
    string_index: int
    produced_kwh: Decimal | None
    #: Mean power across the day, in kW.
    avg_power_kw: Decimal | None
    #: Signed deviation from the peer median, in percent. None when there were
    #: too few peers to establish one.
    deviation_pct: Decimal | None
    #: Whether this panel is outside the configured variance threshold.
    is_anomalous: bool


@dataclass(frozen=True, slots=True)
class AlertWithStore:
    """An alert together with the branch it concerns.

    Paired at the source because an alert on its own cannot be acted on — a
    dispatcher needs the branch name and, ideally, where it is.
    """

    alert: Alert
    store_code: str
    store_name: str
    province: str | None


@dataclass(frozen=True, slots=True)
class StoreWithStatus:
    """A store plus the live signals that colour its map pin.

    This is the read model behind ``GET /stores/map``. It is deliberately a
    single flat object: the map renders 200+ of these at once, so making the
    frontend issue a follow-up request per store would be untenable.
    """

    store: Store
    pr_status: PRStatus
    #: Performance Ratio as a percentage (e.g. Decimal("82.4")), or None when
    #: no irradiance baseline is available.
    performance_ratio: Decimal | None
    active_power_kw: Decimal | None
    daily_yield_kwh: Decimal | None
    last_seen_at: datetime | None
    is_online: bool
    has_string_anomaly: bool
    open_alert_count: int
    max_alert_severity: AlertSeverity | None
    #: Today's yield per installed kWp. Computable from telemetry alone, so it
    #: is available for every branch with a recorded capacity — unlike PR%,
    #: which needs an irradiance baseline this deployment does not yet have.
    #: Which vendor cloud this branch's data comes from, taken from its
    #: devices rather than from stores.monitoring_source — that column is
    #: filled for only a fifth of the roster, while every reporting branch has
    #: devices carrying the vendor that produced them.
    vendor_key: str | None = None
    #: Month to date and lifetime, as the vendor counts them.
    monthly_yield_kwh: Decimal | None = None
    lifetime_yield_kwh: Decimal | None = None
    specific_yield_kwh_per_kwp: Decimal | None = None
    #: The above as a percentage of the fleet median for the same day. This is
    #: what colours the pin when ``performance_ratio`` is None.
    yield_vs_peers_pct: Decimal | None = None
    #: False when no vendor account has ever delivered a reading for this
    #: branch. Distinguishes "waiting to be connected" from "went dark", which
    #: look identical in the telemetry and mean opposite things operationally.
    has_ever_reported: bool = True
