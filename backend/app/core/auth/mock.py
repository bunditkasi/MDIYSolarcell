"""Phase 1 authentication: no real checking.

DO NOT DEPLOY THIS OUTSIDE THE PILOT. It authenticates nobody and grants access
to every store. It exists so Phase 1 can be built and demonstrated before
corporate SSO is available, and ``app.main`` logs a warning at startup whenever
it is active so nobody mistakes it for a working login.
"""

from __future__ import annotations

from app.core.auth.base import AuthenticatedUser, AuthProviderInterface
from app.core.config import Settings

__all__ = ["MockAuthProvider"]


class MockAuthProvider(AuthProviderInterface):
    def __init__(self, settings: Settings) -> None:
        self._user = AuthenticatedUser(
            user_id=settings.mock_user_id,
            username=settings.mock_user_name,
            email=settings.mock_user_email,
            roles=tuple(settings.mock_user_role_list),
            allowed_store_codes=(),  # unrestricted
        )

    async def authenticate(self, token: str | None) -> AuthenticatedUser:
        # The token is ignored on purpose — including when absent.
        return self._user

    async def healthy(self) -> bool:
        return True
