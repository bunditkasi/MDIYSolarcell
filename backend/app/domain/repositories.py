"""Repository interfaces — the Phase 2 handover seam.

WHY THIS EXISTS
---------------
Phase 1 runs on a local Dockerised PostgreSQL + TimescaleDB. Phase 2 moves onto
the corporate enterprise SQL server, and corporate IT does that work. The whole
point of this module is that the move should touch exactly one thing: a new
class implementing these interfaces, selected in ``app.core.deps``. No API
route, no service, and nothing in the frontend should need editing.

RULES FOR ANY IMPLEMENTATION
----------------------------
1. Accept and return domain objects only. Never an ORM row, ``Session``,
   ``Row``, cursor, or connection.
2. No SQL in a signature. Callers describe intent with ``StoreFilter`` and
   ``PageRequest``; how that becomes a query is the implementation's business.
3. Raise domain exceptions (``app.domain.exceptions``), never driver exceptions.
4. Engine-specific features — TimescaleDB ``time_bucket``, ``DISTINCT ON``,
   partial indexes — are allowed, but only INSIDE the implementation.
5. Ordering must be deterministic. Two calls with the same arguments and
   unchanged data must return the same rows in the same order, or pagination
   silently duplicates and skips records.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.filters import Page, PageRequest, StoreFilter
from app.domain.models import (
    AlertWithStore,
    Device,
    EnergyBucket,
    PanelSnapshot,
    Store,
    StoreWithStatus,
)

__all__ = [
    "AlertRepositoryInterface",
    "StoreRepositoryInterface",
    "TelemetryRepositoryInterface",
]


class StoreRepositoryInterface(ABC):
    """Read and write access to stores and their devices."""

    # -- Reads ---------------------------------------------------------------

    @abstractmethod
    async def list_stores(
        self,
        store_filter: StoreFilter,
        page: PageRequest,
    ) -> Page[Store]:
        """Return one page of stores matching ``store_filter``.

        Filtering, sorting and pagination must all be executed by the storage
        engine. Fetching every row and slicing in Python would work at pilot
        scale and fall over at 200+ stores with several years of history.
        """

    @abstractmethod
    async def get_store(self, store_id: UUID) -> Store | None:
        """Return the store, or ``None`` if no such id exists.

        Returns ``None`` rather than raising: "does this exist?" is a normal
        question, not an exceptional one. Callers that require the store should
        raise ``StoreNotFoundError`` themselves.
        """

    @abstractmethod
    async def get_store_by_code(self, store_code: str) -> Store | None:
        """Look up by the human-facing branch code (e.g. ``MRD-0142``)."""

    @abstractmethod
    async def list_stores_with_status(
        self,
        store_filter: StoreFilter,
    ) -> list[StoreWithStatus]:
        """Return every matching store with its live status, for the map.

        Deliberately unpaginated: the GIS view plots the whole fleet at once,
        and a partially loaded map is worse than a slow one. Callers restrict
        the volume with ``StoreFilter.bbox`` instead.

        Implementations must compute ``pr_status`` by calling
        ``app.domain.status.classify_pr_status`` rather than reimplementing the
        thresholds, so every backend colours pins identically.
        """

    @abstractmethod
    async def list_devices_for_store(self, store_id: UUID) -> list[Device]:
        """Return the store's devices, ordered deterministically."""

    @abstractmethod
    async def count_stores(self, store_filter: StoreFilter) -> int:
        """Count matching stores without materialising them."""

    # -- Writes --------------------------------------------------------------

    @abstractmethod
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
        """Create a store and return it as persisted.

        Raises:
            DuplicateStoreCodeError: ``store_code`` is already taken.
        """

    @abstractmethod
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
        """Partially update a store. ``None`` means "leave unchanged".

        Raises:
            StoreNotFoundError: no store with that id.
        """

    @abstractmethod
    async def deactivate_store(self, store_id: UUID) -> Store:
        """Soft-delete: set ``is_active`` false, keeping telemetry history.

        There is no hard delete on this interface by design. Telemetry is the
        evidence base for ESG and financial reporting on a >1,000M THB asset
        programme; a decommissioned branch's history must survive the branch.

        Raises:
            StoreNotFoundError: no store with that id.
        """

    # -- Health --------------------------------------------------------------

    @abstractmethod
    async def ping(self) -> bool:
        """Cheap round trip, for the readiness probe."""


class TelemetryRepositoryInterface(ABC):
    """Time-series writes and reads.

    Split from ``StoreRepositoryInterface`` because the two have genuinely
    different shapes: metadata is transactional CRUD, telemetry is high-volume
    append plus analytical aggregation. In Phase 2 they may not even land on the
    same server — telemetry could stay on TimescaleDB while metadata moves to
    the corporate database.
    """

    @abstractmethod
    async def record_raw_readings(self, readings: list[dict[str, object]]) -> int:
        """Append inverter readings idempotently; return rows written.

        Re-polling an overlapping window is normal after a failed sync, so this
        must upsert on ``(device_id, time)`` rather than duplicate.
        """

    @abstractmethod
    async def record_string_readings(self, readings: list[dict[str, object]]) -> int:
        """Append per-string readings idempotently on
        ``(device_id, mppt_index, string_index, time)``."""

    @abstractmethod
    async def get_latest_reading(self, device_id: UUID) -> dict[str, object] | None:
        """Most recent raw reading for one device, or ``None``."""

    @abstractmethod
    async def get_energy_history(
        self,
        store_id: UUID,
        *,
        granularity: str,
        start: date,
        end: date,
    ) -> list[EnergyBucket]:
        """Energy produced per day, month or year for one branch.

        ``granularity`` is "day", "month" or "year".

        Aggregation belongs in the storage engine, not in Python: a year of
        15-minute readings is ~35,000 rows per device, and pulling them across
        the wire to sum them would make the Reports page unusable.
        """

    @abstractmethod
    async def get_panel_snapshot(
        self,
        store_id: UUID,
        *,
        on_date: date,
    ) -> list[PanelSnapshot]:
        """Per-panel / per-string energy for one branch on one day.

        Feeds the array view. Returns an empty list when the branch has no
        per-panel data — which is a real state, not an error: a site whose
        inverter reports nothing below the device level simply has none.
        """

    @abstractmethod
    async def get_string_readings_at(
        self,
        device_id: UUID,
        at_time: datetime,
        tolerance_minutes: int = 5,
    ) -> list[dict[str, object]]:
        """String readings near ``at_time``, for Intra-String Peer Comparison.

        Comparison is only valid between strings sampled at the same moment
        under the same irradiance, hence the tolerance window rather than an
        exact timestamp match — vendors rarely align their sample clocks.
        """


class AlertRepositoryInterface(ABC):
    """Alert reads.

    Separate from the store repository because alerts have their own lifecycle
    — raised by analytics, acknowledged by a person, resolved when the fault
    clears — and in Phase 2 they may not even live in the same system as the
    store master data.
    """

    @abstractmethod
    async def list_alerts(
        self,
        *,
        statuses: tuple[str, ...] = (),
        severities: tuple[str, ...] = (),
        alert_types: tuple[str, ...] = (),
        store_id: UUID | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Page[AlertWithStore]:
        """Alerts matching the filters, newest first.

        Returns the store alongside each alert: an alert without its branch name
        is unactionable, and making the caller join them back together invites
        an N+1 query per row.
        """

    @abstractmethod
    async def count_open_by_severity(self) -> dict[str, int]:
        """Open alert counts keyed by severity, for the navigation badge."""
