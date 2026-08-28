"""Secrets provider interface — the third Phase 2 seam.

Specification section 1: OEM portal credentials MUST be fetched securely
in-memory and MUST NEVER be hardcoded.

The database stores only ``data_adapters.secrets_ref``, a lookup key. The
credential itself is resolved through this interface at the moment it is needed
and is never written to a table, a log line, or a config file that gets
committed. A database backup must not be a credential leak.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = ["Credential", "SecretNotFoundError", "SecretsProviderInterface"]


class SecretNotFoundError(KeyError):
    """No credential is stored under the requested reference."""

    def __init__(self, secrets_ref: str) -> None:
        super().__init__(secrets_ref)
        self.secrets_ref = secrets_ref


@dataclass(frozen=True, slots=True)
class Credential:
    """One portal or API credential, held in memory only.

    ``__repr__`` and ``__str__`` are overridden so the secret cannot leak
    through a log line, an exception traceback, or a debugger's default
    rendering — the three ways this normally escapes.
    """

    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    extra: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        present = [
            name
            for name, value in (
                ("username", self.username),
                ("password", self.password),
                ("api_key", self.api_key),
            )
            if value
        ]
        return f"Credential(<redacted: {', '.join(present) or 'empty'}>)"

    __str__ = __repr__


class SecretsProviderInterface(ABC):
    @abstractmethod
    async def get_credential(self, secrets_ref: str) -> Credential:
        """Resolve a reference to a credential.

        Raises:
            SecretNotFoundError: nothing is stored under that reference.
        """

    @abstractmethod
    async def healthy(self) -> bool:
        """Whether the secret store is reachable."""
