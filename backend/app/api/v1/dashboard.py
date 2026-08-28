"""Dashboard endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.v1.schemas import ThresholdsOut
from app.core.deps import CurrentUser, SettingsDep, StoreRepositoryDep
from app.domain.filters import StoreFilter
from app.services.dashboard_service import DashboardService, DashboardSummary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class StatusCountsOut(BaseModel):
    GREEN: int = 0
    YELLOW: int = 0
    RED: int = 0
    UNKNOWN: int = 0


class SeverityCountsOut(BaseModel):
    CRITICAL: int = 0
    MAJOR: int = 0
    MINOR: int = 0


class FleetOut(BaseModel):
    total_stores: int
    active_stores: int
    stores_without_location: int
    incomplete_stores: int
    total_installed_kwp: Decimal


class LiveOut(BaseModel):
    stores_online: int
    stores_offline: int
    total_active_power_kw: Decimal
    total_daily_yield_kwh: Decimal


class PerformanceOut(BaseModel):
    fleet_performance_ratio: Decimal | None = Field(
        default=None,
        description=(
            "Capacity-weighted mean PR across sites that have one. Null when no "
            "site has an irradiance baseline."
        ),
    )
    stores_with_pr: int = Field(
        description="Sites contributing to the figure above — read it alongside "
        "the ratio, since a fleet PR from 3 of 153 sites means little."
    )


class AlertsOut(BaseModel):
    open_alert_count: int
    by_severity: SeverityCountsOut
    stores_with_string_anomaly: int


class EsgOut(BaseModel):
    co2_avoided_today_kg: Decimal | None
    emission_factor: Decimal
    emission_factor_year: int
    standard: str = "TGO Thailand"


class DashboardSummaryOut(BaseModel):
    fleet: FleetOut
    live: LiveOut
    status_counts: StatusCountsOut
    performance: PerformanceOut
    alerts: AlertsOut
    esg: EsgOut
    thresholds: ThresholdsOut


def _to_response(summary: DashboardSummary, thresholds: ThresholdsOut) -> DashboardSummaryOut:
    return DashboardSummaryOut(
        fleet=FleetOut(
            total_stores=summary.total_stores,
            active_stores=summary.active_stores,
            stores_without_location=summary.stores_without_location,
            incomplete_stores=summary.incomplete_stores,
            total_installed_kwp=summary.total_installed_kwp,
        ),
        live=LiveOut(
            stores_online=summary.stores_online,
            stores_offline=summary.stores_offline,
            total_active_power_kw=summary.total_active_power_kw,
            total_daily_yield_kwh=summary.total_daily_yield_kwh,
        ),
        status_counts=StatusCountsOut(
            **{status.value: count for status, count in summary.status_counts.items()}
        ),
        performance=PerformanceOut(
            fleet_performance_ratio=summary.fleet_performance_ratio,
            stores_with_pr=summary.stores_with_pr,
        ),
        alerts=AlertsOut(
            open_alert_count=summary.open_alert_count,
            by_severity=SeverityCountsOut(
                **{
                    severity.value: count
                    for severity, count in summary.alert_counts_by_severity.items()
                }
            ),
            stores_with_string_anomaly=summary.stores_with_string_anomaly,
        ),
        esg=EsgOut(
            co2_avoided_today_kg=summary.co2_avoided_today_kg,
            emission_factor=summary.emission_factor,
            emission_factor_year=summary.emission_factor_year,
        ),
        thresholds=thresholds,
    )


@router.get("/summary", response_model=DashboardSummaryOut, summary="Fleet KPI summary")
async def dashboard_summary(
    repository: StoreRepositoryDep,
    settings: SettingsDep,
    user: CurrentUser,
    region: Annotated[list[str] | None, Query()] = None,
) -> DashboardSummaryOut:
    """Headline numbers for the executive dashboard."""
    service = DashboardService(repository, settings)
    summary = await service.summary(
        StoreFilter(regions=tuple(region or ()), is_active=True)
    )
    return _to_response(
        summary,
        ThresholdsOut(
            pr_green_threshold=settings.pr_green_threshold,
            string_variance_threshold_pct=settings.string_variance_threshold_pct,
            device_offline_after_minutes=settings.effective_offline_after_minutes,
            yield_green_threshold_pct=settings.yield_green_threshold_pct,
        ),
    )
