"""Per-branch analytics: energy history and the panel array.

These back the branch page's Reports and Array tabs.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, SettingsDep, StoreRepositoryDep, TelemetryRepositoryDep

router = APIRouter(prefix="/stores/{store_id}", tags=["site"])

Granularity = Literal["day", "month", "year"]


class EnergyBucketOut(BaseModel):
    period: date
    produced_kwh: Decimal | None = None
    device_count: int
    sample_count: int


class EnergyHistoryOut(BaseModel):
    granularity: Granularity
    start: date
    end: date
    buckets: list[EnergyBucketOut]
    total_produced_kwh: Decimal | None = None


class PanelOut(BaseModel):
    serial_number: str
    mppt_index: int
    string_index: int
    #: Human label. "B-4" for a microinverter panel, "MPPT 1 / S2" for a string.
    label: str
    produced_kwh: Decimal | None = None
    avg_power_kw: Decimal | None = None
    deviation_pct: Decimal | None = None
    is_anomalous: bool


class PanelArrayOut(BaseModel):
    on_date: date
    panels: list[PanelOut]
    anomaly_count: int
    variance_threshold_pct: Decimal
    #: True when the branch reports nothing below device level. Distinct from
    #: "all panels read zero" — one is missing data, the other is a fault.
    has_panel_data: bool = Field(
        description="False means this hardware publishes no per-panel data at all."
    )


def _panel_label(mppt_index: int, string_index: int, is_panel_basis: bool) -> str:
    """Grid reference for a panel, or an MPPT/string reference for a string.

    Microinverter panels get spreadsheet-style labels (A-1, B-4) because that is
    how an installer reads a roof layout: row letter, column number.
    """
    if is_panel_basis:
        row = chr(ord("A") + (string_index - 1) // 12) if string_index > 0 else "A"
        col = ((string_index - 1) % 12) + 1 if string_index > 0 else 1
        return f"{row}-{col}"
    return f"MPPT {mppt_index} / S{string_index}"


@router.get("/energy", response_model=EnergyHistoryOut, summary="Energy history")
async def energy_history(
    store_id: UUID,
    repository: StoreRepositoryDep,
    telemetry: TelemetryRepositoryDep,
    user: CurrentUser,
    granularity: Annotated[Granularity, Query()] = "day",
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> EnergyHistoryOut:
    if await repository.get_store(store_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Store {store_id} not found")

    today = date.today()
    end = end or today
    if start is None:
        span = {"day": 30, "month": 365, "year": 365 * 5}[granularity]
        start = end - timedelta(days=span)

    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be on or before end")

    buckets = await telemetry.get_energy_history(
        store_id, granularity=granularity, start=start, end=end
    )

    totals = [b.produced_kwh for b in buckets if b.produced_kwh is not None]
    return EnergyHistoryOut(
        granularity=granularity,
        start=start,
        end=end,
        buckets=[
            EnergyBucketOut(
                period=b.period,
                produced_kwh=b.produced_kwh,
                device_count=b.device_count,
                sample_count=b.sample_count,
            )
            for b in buckets
        ],
        total_produced_kwh=sum(totals, Decimal("0")) if totals else None,
    )


@router.get("/array", response_model=PanelArrayOut, summary="Panel / string array")
async def panel_array(
    store_id: UUID,
    repository: StoreRepositoryDep,
    telemetry: TelemetryRepositoryDep,
    settings: SettingsDep,
    user: CurrentUser,
    on_date: Annotated[date | None, Query()] = None,
) -> PanelArrayOut:
    if await repository.get_store(store_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Store {store_id} not found")

    target = on_date or date.today()
    snapshots = await telemetry.get_panel_snapshot(store_id, on_date=target)

    devices = {d.serial_number: d for d in await repository.list_devices_for_store(store_id)}

    panels = []
    for snap in snapshots:
        device = devices.get(snap.serial_number)
        is_panel_basis = device is not None and device.measurement_basis.value == "PANEL"
        panels.append(
            PanelOut(
                serial_number=snap.serial_number,
                mppt_index=snap.mppt_index,
                string_index=snap.string_index,
                label=_panel_label(snap.mppt_index, snap.string_index, is_panel_basis),
                produced_kwh=snap.produced_kwh,
                avg_power_kw=snap.avg_power_kw,
                deviation_pct=snap.deviation_pct,
                is_anomalous=snap.is_anomalous,
            )
        )

    return PanelArrayOut(
        on_date=target,
        panels=panels,
        anomaly_count=sum(1 for p in panels if p.is_anomalous),
        variance_threshold_pct=settings.string_variance_threshold_pct,
        has_panel_data=bool(panels),
    )
