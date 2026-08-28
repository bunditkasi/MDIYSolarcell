"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.config import AuthMode
from app.core.deps import SettingsDep, StoreRepositoryDep

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: str
    app_env: str
    auth_mode: AuthMode
    database: str
    weather_enabled: bool


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Is the process up? Deliberately touches no dependency, so a database
    blip does not cause the orchestrator to kill an otherwise healthy container."""
    return {"status": "ok"}


@router.get("/ready", response_model=HealthOut, summary="Readiness probe")
async def ready(
    repository: StoreRepositoryDep,
    settings: SettingsDep,
    response: Response,
) -> HealthOut:
    """Can the process actually serve traffic? Checks the database."""
    db_ok = await repository.ping()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthOut(
        status="ok" if db_ok else "degraded",
        app_env=settings.app_env,
        auth_mode=settings.auth_mode,
        database="ok" if db_ok else "unreachable",
        weather_enabled=settings.weather_enabled,
    )
