"""PostgreSQL implementation of the alert repository."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import RepositoryError
from app.domain.filters import Page
from app.domain.models import (
    Alert,
    AlertSeverity,
    AlertStatus,
    AlertType,
    AlertWithStore,
)
from app.domain.repositories import AlertRepositoryInterface
from app.infrastructure.db.orm import AlertORM, StoreORM

__all__ = ["LocalPostgresAlertRepository"]

#: Worst first. Used to order by urgency rather than alphabetically — "CRITICAL"
#: sorting after "MAJOR" is exactly the wrong order for a dispatch queue.
_SEVERITY_ORDER = {"CRITICAL": 1, "MAJOR": 2, "MINOR": 3}


class LocalPostgresAlertRepository(AlertRepositoryInterface):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        try:
            base = self._apply_filters(
                select(AlertORM, StoreORM).join(StoreORM, AlertORM.store_id == StoreORM.store_id),
                statuses=statuses,
                severities=severities,
                alert_types=alert_types,
                store_id=store_id,
                search=search,
            )

            total = await self._session.scalar(
                select(func.count()).select_from(base.subquery())
            )

            # Order by urgency, not alphabetically: "CRITICAL" sorts after
            # "MAJOR" as text, which is precisely the wrong order for a queue
            # somebody works down from the top.
            severity_rank = case(_SEVERITY_ORDER, value=AlertORM.severity, else_=99)

            rows = (
                await self._session.execute(
                    base.order_by(
                        # Worst severity first, then newest. A dispatcher works
                        # top-down, so the ordering IS the triage.
                        severity_rank.asc(),
                        AlertORM.created_at.desc(),
                        AlertORM.alert_id.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()

            return Page(
                items=[_to_domain(alert, store) for alert, store in rows],
                total=int(total or 0),
                limit=limit,
                offset=offset,
            )
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to list alerts", cause=exc) from exc

    async def count_open_by_severity(self) -> dict[str, int]:
        try:
            rows = (
                await self._session.execute(
                    select(AlertORM.severity, func.count())
                    .where(AlertORM.status != AlertStatus.RESOLVED.value)
                    .group_by(AlertORM.severity)
                )
            ).all()
            counts = {severity.value: 0 for severity in AlertSeverity}
            for severity, count in rows:
                counts[str(severity)] = int(count)
            return counts
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to count alerts", cause=exc) from exc

    def _apply_filters(
        self,
        stmt: Select[Any],
        *,
        statuses: tuple[str, ...],
        severities: tuple[str, ...],
        alert_types: tuple[str, ...],
        store_id: UUID | None,
        search: str | None,
    ) -> Select[Any]:
        if statuses:
            stmt = stmt.where(AlertORM.status.in_(statuses))
        if severities:
            stmt = stmt.where(AlertORM.severity.in_(severities))
        if alert_types:
            stmt = stmt.where(AlertORM.alert_type.in_(alert_types))
        if store_id is not None:
            stmt = stmt.where(AlertORM.store_id == store_id)

        if search:
            needle = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{needle}%"
            stmt = stmt.where(
                or_(
                    StoreORM.store_code.ilike(pattern, escape="\\"),
                    StoreORM.store_name.ilike(pattern, escape="\\"),
                    AlertORM.message.ilike(pattern, escape="\\"),
                )
            )

        return stmt


def _to_domain(row: AlertORM, store: StoreORM) -> AlertWithStore:
    return AlertWithStore(
        alert=Alert(
            alert_id=row.alert_id,
            store_id=row.store_id,
            device_id=row.device_id,
            alert_type=AlertType(row.alert_type),
            severity=AlertSeverity(row.severity),
            message=row.message,
            status=AlertStatus(row.status),
            details=dict(row.details or {}),
            created_at=row.created_at,
            acknowledged_at=row.acknowledged_at,
            resolved_at=row.resolved_at,
        ),
        store_code=store.store_code,
        store_name=store.store_name,
        province=store.province,
    )
