"""Query inputs and paged results.

These types are how callers express "which stores, in what order, which page"
WITHOUT writing SQL. That is what makes the repository swappable in Phase 2: a
filter is a plain value object, so an MS SQL / Oracle implementation can honour
it however that engine prefers.

Note in particular ``StoreSortField``. Sort columns arrive from the HTTP layer,
and accepting a raw string would put user input into an ORDER BY clause. An enum
means an unknown value fails at the API boundary instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar

from app.domain.models import PRStatus

T = TypeVar("T")

__all__ = [
    "BoundingBox",
    "Page",
    "PageRequest",
    "StoreFilter",
    "StoreSortField",
]

#: Guard against a client asking for the whole fleet in one response.
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Map viewport, used to fetch only the pins currently on screen."""

    min_lat: Decimal
    min_lng: Decimal
    max_lat: Decimal
    max_lng: Decimal

    def __post_init__(self) -> None:
        if self.min_lat > self.max_lat:
            raise ValueError("min_lat must not exceed max_lat")
        if self.min_lng > self.max_lng:
            raise ValueError("min_lng must not exceed max_lng")


class StoreSortField(str, Enum):
    STORE_CODE = "store_code"
    STORE_NAME = "store_name"
    REGION = "region"
    INSTALLED_KWP = "installed_kwp"
    CREATED_AT = "created_at"


@dataclass(frozen=True, slots=True)
class StoreFilter:
    """Criteria for narrowing a store listing.

    Empty tuples and ``None`` mean "do not filter on this". Defaulting
    ``is_active`` to True keeps decommissioned branches out of every listing
    unless a caller deliberately asks for them.
    """

    search: str | None = None
    regions: tuple[str, ...] = ()
    pr_statuses: tuple[PRStatus, ...] = ()
    bbox: BoundingBox | None = None
    is_active: bool | None = True


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0
    sort_by: StoreSortField = StoreSortField.STORE_CODE
    descending: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}, got {self.limit}")
        if self.offset < 0:
            raise ValueError(f"offset must not be negative, got {self.offset}")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results plus the total row count.

    ``total`` is carried explicitly rather than inferred from ``len(items)``
    because the UI needs it to render pagination, and counting is the expensive
    half of the query — some Phase 2 backends may want to approximate it.
    """

    items: list[T] = field(default_factory=list)
    total: int = 0
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
