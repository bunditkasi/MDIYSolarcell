"""Dependency wiring — THE Phase 2 handover file.

Corporate IT should be able to move this system onto the enterprise database and
Active Directory by editing this one module (plus adding the new implementation
classes). Nothing in ``app/api``, ``app/services`` or the frontend selects an
implementation; they only ever ask for an interface.

If a future change makes it necessary to edit an API route in order to swap a
backend, that change has broken the architecture — fix the wiring here instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.base import AuthenticatedUser, AuthProviderInterface
from app.core.auth.enterprise import EnterpriseSSOProvider, SSONotConfiguredError
from app.core.auth.mock import MockAuthProvider
from app.core.config import AuthMode, SecretsProviderKind, Settings, get_settings
from app.core.secrets.base import SecretsProviderInterface
from app.core.secrets.env import EnvSecretsProvider
from app.core.secrets.vault import VaultSecretsProvider
from app.domain.repositories import AlertRepositoryInterface, StoreRepositoryInterface
from app.infrastructure.db.session import get_session
from app.infrastructure.repositories.local_postgres import LocalPostgresRepository
from app.infrastructure.repositories.local_postgres_alerts import (
    LocalPostgresAlertRepository,
)
from app.infrastructure.repositories.local_postgres_telemetry import (
    LocalPostgresTelemetryRepository,
)
from app.ingestion.base import InverterDataSourceInterface
from app.ingestion.oem.atmoce import AtmoceAdapter
from app.ingestion.oem.huawei import HuaweiFusionSolarAdapter

__all__ = [
    "AlertRepositoryDep",
    "TelemetryRepositoryDep",
    "CurrentUser",
    "build_data_source",
    "SettingsDep",
    "StoreRepositoryDep",
    "get_auth_provider",
    "get_current_user",
    "get_secrets_provider",
    "get_store_repository",
]

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


async def get_store_repository(
    session: SessionDep,
    settings: SettingsDep,
) -> AsyncIterator[StoreRepositoryInterface]:
    """Provide the store repository.

    PHASE 2: replace the constructed class below with the enterprise
    implementation, e.g.

        if settings.database_url.startswith("mssql+aioodbc"):
            yield SqlServerStoreRepository(session, settings)
        else:
            yield LocalPostgresRepository(session, settings)

    The declared return type stays ``StoreRepositoryInterface``, so no caller
    notices.
    """
    yield LocalPostgresRepository(session, settings)


StoreRepositoryDep = Annotated[StoreRepositoryInterface, Depends(get_store_repository)]


async def get_alert_repository(
    session: SessionDep,
) -> AsyncIterator[AlertRepositoryInterface]:
    """PHASE 2: swap for the enterprise implementation here, as with stores."""
    yield LocalPostgresAlertRepository(session)


AlertRepositoryDep = Annotated[AlertRepositoryInterface, Depends(get_alert_repository)]


async def get_telemetry_repository(
    session: SessionDep,
    settings: SettingsDep,
) -> AsyncIterator[LocalPostgresTelemetryRepository]:
    """PHASE 2: telemetry may stay on TimescaleDB even after metadata moves to
    the corporate database — the two are swapped independently."""
    yield LocalPostgresTelemetryRepository(session, settings)


TelemetryRepositoryDep = Annotated[
    LocalPostgresTelemetryRepository, Depends(get_telemetry_repository)
]


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_auth_provider() -> AuthProviderInterface:
    """Select the auth provider from AUTH_MODE.

    Cached: providers are stateless apart from key caches, and rebuilding one
    per request would re-fetch the JWKS every time in Phase 2.
    """
    settings = get_settings()
    if settings.auth_mode is AuthMode.ENTERPRISE_SSO:
        return EnterpriseSSOProvider(settings)
    return MockAuthProvider(settings)


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedUser:
    """Resolve the caller.

    Every route depends on this from Phase 1 onward, even though mock mode lets
    everything through — so enabling real authentication in Phase 2 is a
    configuration change, not a code change spread across every endpoint.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    provider = get_auth_provider()
    try:
        return await provider.authenticate(token)
    except SSONotConfiguredError as exc:
        # 501, not 500: the deployment asked for a mode that has not been built
        # yet. This is what proves the seam works end to end.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_secrets_provider() -> SecretsProviderInterface:
    settings = get_settings()

    if settings.secrets_provider is SecretsProviderKind.VAULT:
        return VaultSecretsProvider(settings)

    if settings.is_production:
        # Refuse rather than warn. Environment variables are readable by
        # anything that can inspect the container, and a silent downgrade here
        # would be exactly the kind of thing nobody notices until it matters.
        raise RuntimeError(
            "SECRETS_PROVIDER=env is not permitted when APP_ENV=production. "
            "Configure Vault (SECRETS_PROVIDER=vault, VAULT_ADDR, VAULT_TOKEN)."
        )

    return EnvSecretsProvider()


# --------------------------------------------------------------------------- #
# Vendor ingestion
# --------------------------------------------------------------------------- #


def build_data_source(
    *,
    vendor_key: str,
    base_url: str,
    secrets_ref: str,
) -> InverterDataSourceInterface:
    """Construct the adapter for one vendor account.

    Adding a vendor means adding a class and one line here — no scheduler, no
    analytics and no API code changes, because everything downstream is written
    against ``InverterDataSourceInterface``.

    ``secrets_ref`` is a LOOKUP KEY from ``data_adapters.secrets_ref``, never a
    credential. The adapter resolves it through the secrets provider at the
    moment of use.
    """
    secrets = get_secrets_provider()
    key = vendor_key.strip().lower()

    if key == AtmoceAdapter.vendor_key:
        return AtmoceAdapter(base_url=base_url, secrets_ref=secrets_ref, secrets=secrets)

    if key == HuaweiFusionSolarAdapter.vendor_key:
        return HuaweiFusionSolarAdapter(
            base_url=base_url, secrets_ref=secrets_ref, secrets=secrets
        )

    raise ValueError(
        f"No adapter registered for vendor_key={vendor_key!r}. "
        f"Known vendors: {AtmoceAdapter.vendor_key}, {HuaweiFusionSolarAdapter.vendor_key}. "
        f"Sites with no API (FusionSolar accounts without northbound access) use "
        f"adapter_type=SCRAPER instead."
    )
