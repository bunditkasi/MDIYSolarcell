"""HashiCorp Vault secrets provider (production).

Reads KV v2 secrets at ``<mount>/data/<secrets_ref>``, expecting some of the
keys ``username``, ``password``, ``api_key``.

Nothing is cached. Vault leases are short-lived by design, and holding a
credential in process memory past the moment of use throws away most of the
benefit of using Vault at all. If the round trip per sync ever becomes a
measurable cost, add a TTL cache bounded well below the lease duration — not an
unbounded one.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.secrets.base import (
    Credential,
    SecretNotFoundError,
    SecretsProviderInterface,
)

__all__ = ["VaultSecretsProvider"]


class VaultSecretsProvider(SecretsProviderInterface):
    def __init__(self, settings: Settings) -> None:
        if not settings.vault_addr or not settings.vault_token:
            raise ValueError(
                "SECRETS_PROVIDER=vault requires VAULT_ADDR and VAULT_TOKEN to be set."
            )
        self._settings = settings
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import hvac  # imported lazily so dev installs need no Vault client

            self._client = hvac.Client(
                url=self._settings.vault_addr,
                token=self._settings.vault_token,
            )
        return self._client

    async def get_credential(self, secrets_ref: str) -> Credential:
        client = self._get_client()
        try:
            response = client.secrets.kv.v2.read_secret_version(
                path=secrets_ref,
                mount_point=self._settings.vault_mount,
                raise_on_deleted_version=True,
            )
        except Exception as exc:  # hvac raises a wide family of errors
            # Deliberately does not include the response body: Vault errors can
            # echo request content back.
            raise SecretNotFoundError(secrets_ref) from exc

        data = response.get("data", {}).get("data", {}) or {}
        if not data:
            raise SecretNotFoundError(secrets_ref)

        known = {"username", "password", "api_key"}
        return Credential(
            username=data.get("username"),
            password=data.get("password"),
            api_key=data.get("api_key"),
            extra=tuple((k, str(v)) for k, v in data.items() if k not in known),
        )

    async def healthy(self) -> bool:
        try:
            return bool(self._get_client().is_authenticated())
        except Exception:
            return False
