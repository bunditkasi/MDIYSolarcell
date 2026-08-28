"""Authentication provider interface — the second Phase 2 seam.

Phase 1 runs AUTH_MODE=mock and lets everything through. Phase 2 flips it to
enterprise_sso and corporate Active Directory takes over.

Every endpoint declares ``Depends(get_current_user)`` from day one, even while
mock mode makes it a formality. That is the point: when IT enables real auth,
nothing has to be found and retrofitted route by route — the wiring is already
there and simply starts enforcing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = ["AuthProviderInterface", "AuthenticatedUser"]


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str
    username: str
    email: str | None = None
    roles: tuple[str, ...] = ()
    #: Store codes this user may see. Empty means unrestricted. Phase 2 RBAC
    #: populates this from AD group membership; the field exists now so the
    #: query layer can already be written against it.
    allowed_store_codes: tuple[str, ...] = field(default=())

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


class AuthProviderInterface(ABC):
    @abstractmethod
    async def authenticate(self, token: str | None) -> AuthenticatedUser:
        """Resolve a bearer token to a user.

        Raises:
            PermissionError: the token is missing, malformed or rejected.
        """

    @abstractmethod
    async def healthy(self) -> bool:
        """Whether the provider can currently verify tokens."""
