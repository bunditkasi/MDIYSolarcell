"""FastAPI application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import health
from app.api.v1.router import api_router
from app.core.config import AuthMode, Settings, get_settings
from app.domain.exceptions import (
    DomainError,
    DuplicateStoreCodeError,
    RepositoryError,
    StoreNotFoundError,
)
from app.infrastructure.db.session import dispose_engine

logger = logging.getLogger(__name__)


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings)

    if settings.auth_mode is AuthMode.MOCK:
        # Loud on purpose. Mock auth authenticates nobody; if this line ever
        # appears in a production log, the deployment is wide open.
        logger.warning(
            "AUTH_MODE=mock — every request is treated as '%s' with full access. "
            "This is Phase 1 behaviour and must not reach production.",
            settings.mock_user_name,
        )

    if not settings.weather_enabled:
        logger.warning(
            "SOLCAST_API_KEY is not set — Performance Ratio cannot be computed. "
            "Map pins will show as UNKNOWN rather than a colour."
        )

    yield

    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="MR.DIY Thailand — Solar PV Monitoring",
        description=(
            "Phase 1 (MVP) API. Data access goes through StoreRepositoryInterface "
            "and authentication through AuthProviderInterface, so Phase 2 can move "
            "to the corporate database and Active Directory without changing "
            "business logic or the frontend."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Translate domain errors into HTTP responses in one place.

    Keeping this central means route handlers stay free of try/except noise, and
    a Phase 2 repository raising the same domain errors gets the same responses
    with no extra work.
    """

    @app.exception_handler(StoreNotFoundError)
    async def _store_not_found(request: Request, exc: StoreNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
        )

    @app.exception_handler(DuplicateStoreCodeError)
    async def _duplicate_store(
        request: Request, exc: DuplicateStoreCodeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )

    @app.exception_handler(RepositoryError)
    async def _repository_error(request: Request, exc: RepositoryError) -> JSONResponse:
        # Log the cause, return a generic message: driver errors can carry
        # connection strings and query fragments.
        logger.exception("Repository failure on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "A storage error occurred. Please retry."},
        )

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
        )


app = create_app()
