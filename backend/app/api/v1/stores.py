"""Store endpoints.

Note what this module does NOT contain: no SQL, no session handling, no
knowledge of PostgreSQL. It talks to ``StoreRepositoryInterface`` only. That is
what lets Phase 2 change the database without touching these routes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.schemas import (
    FleetResponse,
    FleetRowOut,
    DeviceOut,
    MapResponse,
    PagedResponse,
    StoreOut,
    StoreStatusOut,
    ThresholdsOut,
)
from app.core.deps import CurrentUser, SettingsDep, StoreRepositoryDep
from app.domain.filters import (
    MAX_PAGE_SIZE,
    BoundingBox,
    PageRequest,
    StoreFilter,
    StoreSortField,
)
from app.domain.models import PRStatus

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("", response_model=PagedResponse[StoreOut], summary="List stores")
async def list_stores(
    repository: StoreRepositoryDep,
    user: CurrentUser,
    search: Annotated[str | None, Query(max_length=128)] = None,
    region: Annotated[list[str] | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = True,
    sort_by: Annotated[StoreSortField, Query()] = StoreSortField.STORE_CODE,
    descending: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PagedResponse[StoreOut]:
    page = await repository.list_stores(
        StoreFilter(
            search=search,
            regions=tuple(region or ()),
            is_active=is_active,
        ),
        PageRequest(limit=limit, offset=offset, sort_by=sort_by, descending=descending),
    )
    return PagedResponse[StoreOut](
        items=[StoreOut.from_domain(store) for store in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.get(
    "/map-pins",
    response_model=MapResponse,
    summary="Fleet status pins for the GIS map",
)
async def stores_map_pins(
    repository: StoreRepositoryDep,
    settings: SettingsDep,
    user: CurrentUser,
    search: Annotated[str | None, Query(max_length=128)] = None,
    region: Annotated[list[str] | None, Query()] = None,
    pr_status: Annotated[list[PRStatus] | None, Query()] = None,
    min_lat: Annotated[Decimal | None, Query(ge=-90, le=90)] = None,
    max_lat: Annotated[Decimal | None, Query(ge=-90, le=90)] = None,
    min_lng: Annotated[Decimal | None, Query(ge=-180, le=180)] = None,
    max_lng: Annotated[Decimal | None, Query(ge=-180, le=180)] = None,
) -> MapResponse:
    """Every matching store with live status, unpaginated.

    The map draws the whole fleet at once; a half-loaded map is worse than a
    slow one. Callers narrow the result with a bounding box instead of a page.
    """
    bbox_parts = (min_lat, max_lat, min_lng, max_lng)
    if any(part is not None for part in bbox_parts):
        if any(part is None for part in bbox_parts):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A bounding box needs all four of min_lat, max_lat, min_lng, max_lng.",
            )
        try:
            bbox = BoundingBox(
                min_lat=min_lat,  # type: ignore[arg-type]
                min_lng=min_lng,  # type: ignore[arg-type]
                max_lat=max_lat,  # type: ignore[arg-type]
                max_lng=max_lng,  # type: ignore[arg-type]
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
    else:
        bbox = None

    results = await repository.list_stores_with_status(
        StoreFilter(
            search=search,
            regions=tuple(region or ()),
            pr_statuses=tuple(pr_status or ()),
            bbox=bbox,
            is_active=True,
        )
    )

    # PR-status filtering happens here rather than in SQL because the status is
    # derived from several signals at read time, not stored on the row. At fleet
    # scale (hundreds, not millions) this is a cheap list comprehension; if a
    # stored pr_status column is ever added, push this down into the repository.
    if pr_status:
        wanted = set(pr_status)
        results = [item for item in results if item.pr_status in wanted]

    plottable = [item for item in results if item.store.has_location]

    return MapResponse(
        stores=[StoreStatusOut.from_domain(item) for item in plottable],
        thresholds=ThresholdsOut(
            pr_green_threshold=settings.pr_green_threshold,
            string_variance_threshold_pct=settings.string_variance_threshold_pct,
            device_offline_after_minutes=settings.effective_offline_after_minutes,
            yield_green_threshold_pct=settings.yield_green_threshold_pct,
        ),
        stores_without_location=len(results) - len(plottable),
    )


@router.get(
    "/fleet",
    response_model=FleetResponse,
    summary="Fleet table — every branch, including those with no coordinates",
)
async def fleet_table(
    repository: StoreRepositoryDep,
    user: CurrentUser,
    settings: SettingsDep,
    search: str | None = Query(default=None, max_length=128),
    region: list[str] | None = Query(default=None),
    pr_status: list[PRStatus] | None = Query(default=None),
) -> FleetResponse:
    """Rows for the fleet list.

    Unlike the map feed this returns branches with no coordinates too. They are
    real sites producing real energy; leaving them out of a LIST because nobody
    has recorded their position yet would hide 13 of 163 branches and silently
    understate every fleet total taken from this endpoint.
    """
    results = await repository.list_stores_with_status(
        StoreFilter(
            search=search,
            regions=tuple(region or ()),
            pr_statuses=tuple(pr_status or ()),
            is_active=True,
        )
    )

    if pr_status:
        wanted = set(pr_status)
        results = [item for item in results if item.pr_status in wanted]

    return FleetResponse(
        rows=[FleetRowOut.from_domain(item) for item in results],
        thresholds=ThresholdsOut(
            pr_green_threshold=settings.pr_green_threshold,
            string_variance_threshold_pct=settings.string_variance_threshold_pct,
            device_offline_after_minutes=settings.effective_offline_after_minutes,
            yield_green_threshold_pct=settings.yield_green_threshold_pct,
        ),
    )


@router.get("/{store_id}", response_model=StoreOut, summary="Get one store")
async def get_store(
    store_id: UUID,
    repository: StoreRepositoryDep,
    user: CurrentUser,
) -> StoreOut:
    store = await repository.get_store(store_id)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Store {store_id} not found"
        )
    return StoreOut.from_domain(store)


@router.get(
    "/{store_id}/devices",
    response_model=list[DeviceOut],
    summary="List a store's devices",
)
async def list_store_devices(
    store_id: UUID,
    repository: StoreRepositoryDep,
    user: CurrentUser,
) -> list[DeviceOut]:
    if await repository.get_store(store_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Store {store_id} not found"
        )

    devices = await repository.list_devices_for_store(store_id)
    return [
        DeviceOut(
            device_id=device.device_id,
            store_id=device.store_id,
            brand=device.brand,
            model=device.model,
            serial_number=device.serial_number,
            device_type=device.device_type.value,
            measurement_basis=device.measurement_basis.value,
            vendor_key=device.vendor_key,
            capacity_kw=device.capacity_kw,
            mppt_count=device.mppt_count,
            is_active=device.is_active,
        )
        for device in devices
    ]
