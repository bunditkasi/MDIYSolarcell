"""Fleet dashboard aggregation.

Sits between the API and the repository. It knows nothing about SQL — it asks
``StoreRepositoryInterface`` for fleet status and reduces it.

Aggregation happens in Python rather than SQL on purpose. The fleet is in the
hundreds (153 sites today, growing by roughly 200 a year), so this is a pass
over a small list, and keeping it here means the calculation does not have to be
rewritten for the Phase 2 database. If the fleet ever reaches a scale where this
matters, add an aggregate method to the interface — at which point every backend
implements it deliberately, rather than one backend having silently grown a
dialect-specific query.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import Settings
from app.domain.filters import StoreFilter
from app.domain.models import AlertSeverity, PRStatus, StoreWithStatus
from app.domain.repositories import StoreRepositoryInterface
from app.engines.carbon import co2_avoided_kg

__all__ = ["DashboardService", "DashboardSummary"]


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    # Fleet composition
    total_stores: int
    active_stores: int
    stores_without_location: int
    total_installed_kwp: Decimal
    #: Branches missing a capacity or a position. They appear in listings but
    #: are absent from capacity-weighted figures.
    incomplete_stores: int

    # Live operation
    stores_online: int
    stores_offline: int
    total_active_power_kw: Decimal
    total_daily_yield_kwh: Decimal

    # Status distribution — drives the map legend counts
    status_counts: dict[PRStatus, int]

    # Performance
    fleet_performance_ratio: Decimal | None
    stores_with_pr: int

    # Alerts
    open_alert_count: int
    alert_counts_by_severity: dict[AlertSeverity, int]
    stores_with_string_anomaly: int

    # ESG
    co2_avoided_today_kg: Decimal | None
    emission_factor: Decimal
    emission_factor_year: int


class DashboardService:
    def __init__(
        self,
        repository: StoreRepositoryInterface,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._settings = settings

    async def summary(self, store_filter: StoreFilter | None = None) -> DashboardSummary:
        fleet = await self._repository.list_stores_with_status(
            store_filter or StoreFilter(is_active=True)
        )
        return self._reduce(fleet)

    def _reduce(self, fleet: list[StoreWithStatus]) -> DashboardSummary:
        status_counts: Counter[PRStatus] = Counter()
        severity_counts: Counter[AlertSeverity] = Counter()

        total_kwp = Decimal("0")
        total_power = Decimal("0")
        total_yield = Decimal("0")
        online = offline = 0
        open_alerts = 0
        anomalies = 0
        no_location = 0
        incomplete = 0

        # Fleet PR is capacity-weighted, not a plain mean of site PRs. A 20 kWp
        # branch and the 195 kWp distribution centre do not deserve equal votes
        # in a single headline number.
        pr_weighted_sum = Decimal("0")
        pr_weight = Decimal("0")
        stores_with_pr = 0

        for item in fleet:
            store = item.store

            status_counts[item.pr_status] += 1
            # A branch with no recorded capacity contributes nothing here.
            # Counting it as zero would be the same number but a different
            # claim — that it is 0 kWp rather than unknown.
            if store.installed_kwp is not None:
                total_kwp += store.installed_kwp

            if not store.has_location:
                no_location += 1
            if store.is_incomplete:
                incomplete += 1

            if item.is_online:
                online += 1
            else:
                offline += 1

            if item.active_power_kw is not None:
                total_power += item.active_power_kw
            if item.daily_yield_kwh is not None:
                total_yield += item.daily_yield_kwh

            if (
                item.performance_ratio is not None
                and store.installed_kwp is not None
                and store.installed_kwp > 0
            ):
                pr_weighted_sum += item.performance_ratio * store.installed_kwp
                pr_weight += store.installed_kwp
                stores_with_pr += 1

            open_alerts += item.open_alert_count
            if item.max_alert_severity is not None:
                severity_counts[item.max_alert_severity] += 1
            if item.has_string_anomaly:
                anomalies += 1

        fleet_pr = (
            (pr_weighted_sum / pr_weight).quantize(Decimal("0.01"))
            if pr_weight > 0
            else None
        )

        return DashboardSummary(
            total_stores=len(fleet),
            active_stores=sum(1 for item in fleet if item.store.is_active),
            stores_without_location=no_location,
            total_installed_kwp=total_kwp.quantize(Decimal("0.01")),
            incomplete_stores=incomplete,
            stores_online=online,
            stores_offline=offline,
            total_active_power_kw=total_power.quantize(Decimal("0.001")),
            total_daily_yield_kwh=total_yield.quantize(Decimal("0.001")),
            status_counts={status: status_counts.get(status, 0) for status in PRStatus},
            fleet_performance_ratio=fleet_pr,
            stores_with_pr=stores_with_pr,
            open_alert_count=open_alerts,
            alert_counts_by_severity={
                severity: severity_counts.get(severity, 0) for severity in AlertSeverity
            },
            stores_with_string_anomaly=anomalies,
            co2_avoided_today_kg=co2_avoided_kg(
                energy_kwh=total_yield,
                emission_factor=self._settings.tgo_grid_emission_factor,
            ),
            emission_factor=self._settings.tgo_grid_emission_factor,
            emission_factor_year=self._settings.tgo_ef_effective_year,
        )
