"""Alert endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.v1.schemas import PagedResponse
from app.core.deps import AlertRepositoryDep, CurrentUser
from app.domain.models import AlertSeverity, AlertStatus, AlertType, AlertWithStore

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertOut(BaseModel):
    alert_id: UUID
    store_id: UUID
    store_code: str
    store_name: str
    province: str | None = None
    device_id: UUID | None = None
    alert_type: AlertType
    severity: AlertSeverity
    status: AlertStatus
    message: str
    details: dict[str, object]
    created_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None

    @classmethod
    def from_domain(cls, item: AlertWithStore) -> AlertOut:
        a = item.alert
        return cls(
            alert_id=a.alert_id,
            store_id=a.store_id,
            store_code=item.store_code,
            store_name=item.store_name,
            province=item.province,
            device_id=a.device_id,
            alert_type=a.alert_type,
            severity=a.severity,
            status=a.status,
            message=a.message,
            details=a.details,
            created_at=a.created_at,
            acknowledged_at=a.acknowledged_at,
            resolved_at=a.resolved_at,
        )


class AlertCountsOut(BaseModel):
    CRITICAL: int = 0
    MAJOR: int = 0
    MINOR: int = 0

    @property
    def total(self) -> int:
        return self.CRITICAL + self.MAJOR + self.MINOR


@router.get("", response_model=PagedResponse[AlertOut], summary="List alerts")
async def list_alerts(
    repository: AlertRepositoryDep,
    user: CurrentUser,
    status: Annotated[list[AlertStatus] | None, Query()] = None,
    severity: Annotated[list[AlertSeverity] | None, Query()] = None,
    alert_type: Annotated[list[AlertType] | None, Query()] = None,
    store_id: Annotated[UUID | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PagedResponse[AlertOut]:
    """Alerts, worst severity first then newest.

    Defaults to unresolved only: a dispatch queue that opens showing every alert
    ever raised is not a queue.
    """
    statuses = (
        tuple(s.value for s in status)
        if status
        else (AlertStatus.OPEN.value, AlertStatus.ACKNOWLEDGED.value)
    )

    page = await repository.list_alerts(
        statuses=statuses,
        severities=tuple(s.value for s in severity or ()),
        alert_types=tuple(t.value for t in alert_type or ()),
        store_id=store_id,
        search=search,
        limit=limit,
        offset=offset,
    )

    return PagedResponse[AlertOut](
        items=[AlertOut.from_domain(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.get("/counts", response_model=AlertCountsOut, summary="Open alerts by severity")
async def alert_counts(
    repository: AlertRepositoryDep,
    user: CurrentUser,
) -> AlertCountsOut:
    return AlertCountsOut(**await repository.count_open_by_severity())
