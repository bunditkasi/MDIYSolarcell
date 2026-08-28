"""Aggregates the v1 routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import alerts, dashboard, site, stores

api_router = APIRouter()
# `site` first: its /stores/{store_id}/energy and /array must be matched before
# the catch-all /stores/{store_id} in `stores`.
api_router.include_router(site.router)
api_router.include_router(stores.router)
api_router.include_router(alerts.router)
api_router.include_router(dashboard.router)
