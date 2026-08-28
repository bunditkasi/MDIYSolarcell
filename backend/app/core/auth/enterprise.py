"""Phase 2 authentication: corporate SSO / Active Directory.

INTENTIONALLY UNIMPLEMENTED — this is the corporate IT team's slot.

The class is real and selectable so that the seam can be proven before the
implementation exists: set AUTH_MODE=enterprise_sso and the application still
starts, then answers 501 with a clear message instead of 500 or a crash. That
distinguishes "not built yet" from "broken", which matters during handover.

To implement:
  1. Fetch and cache the JWKS from ``settings.sso_jwks_url``.
  2. Verify the bearer token's signature, issuer, audience and expiry.
  3. Map AD group claims onto roles and ``allowed_store_codes``.
  4. Delete ``NotImplementedError`` below and remove MockAuthProvider from the
     production configuration.
"""

from __future__ import annotations

from app.core.auth.base import AuthenticatedUser, AuthProviderInterface
from app.core.config import Settings

__all__ = ["EnterpriseSSOProvider", "SSONotConfiguredError"]


class SSONotConfiguredError(RuntimeError):
    """Raised when enterprise SSO is selected but not yet implemented."""


class EnterpriseSSOProvider(AuthProviderInterface):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def authenticate(self, token: str | None) -> AuthenticatedUser:
        raise SSONotConfiguredError(
            "AUTH_MODE=enterprise_sso is selected, but EnterpriseSSOProvider has "
            "not been implemented yet. See app/core/auth/enterprise.py for the "
            "steps. Set AUTH_MODE=mock to continue in Phase 1 mode."
        )

    async def healthy(self) -> bool:
        return False
