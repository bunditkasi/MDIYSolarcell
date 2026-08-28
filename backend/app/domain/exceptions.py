"""Domain-level errors.

Repository implementations translate storage-specific failures into these before
they escape. Business logic and the API layer therefore never catch
``IntegrityError``, ``asyncpg.UniqueViolationError`` or an ODBC error code —
which is exactly what lets Phase 2 replace the storage engine without touching
any caller.
"""

from __future__ import annotations

from uuid import UUID

__all__ = [
    "ConcurrencyError",
    "DomainError",
    "DuplicateStoreCodeError",
    "RepositoryError",
    "StoreNotFoundError",
]


class DomainError(Exception):
    """Base class for every error this application raises deliberately."""


class RepositoryError(DomainError):
    """A storage failure that callers cannot act on (connection lost, timeout).

    Wraps the original exception so the cause survives in logs without leaking
    a driver-specific type into the call site.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class StoreNotFoundError(DomainError):
    def __init__(self, *, store_id: UUID | None = None, store_code: str | None = None) -> None:
        identifier = store_id if store_id is not None else store_code
        super().__init__(f"Store not found: {identifier}")
        self.store_id = store_id
        self.store_code = store_code


class DuplicateStoreCodeError(DomainError):
    def __init__(self, store_code: str) -> None:
        super().__init__(f"Store code already exists: {store_code}")
        self.store_code = store_code


class ConcurrencyError(DomainError):
    """The row changed between read and write."""
