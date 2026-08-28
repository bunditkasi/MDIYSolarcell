"""Environment-variable secrets provider — LOCAL DEVELOPMENT ONLY.

Reads credentials from the process environment using the naming pattern:

    SECRET__<SECRETS_REF>__USERNAME
    SECRET__<SECRETS_REF>__PASSWORD
    SECRET__<SECRETS_REF>__APIKEY

where <SECRETS_REF> is data_adapters.secrets_ref upper-cased with non-alphanumeric
characters replaced by underscores.

Not suitable for production: environment variables are visible to anything that
can read /proc, they appear in container inspect output, and they are not
rotatable without a restart. ``app.core.deps`` refuses to select this provider
when APP_ENV is production.
"""

from __future__ import annotations

import os
import re

from app.core.secrets.base import (
    Credential,
    SecretNotFoundError,
    SecretsProviderInterface,
)

__all__ = ["EnvSecretsProvider"]

_UNSAFE = re.compile(r"[^A-Z0-9]+")


def _env_key(secrets_ref: str, field: str) -> str:
    normalised = _UNSAFE.sub("_", secrets_ref.upper()).strip("_")
    return f"SECRET__{normalised}__{field}"


def _clean(value: str | None) -> str | None:
    """Strip the wrappers a pasted credential usually arrives with.

    Documentation writes placeholders as <app_key>, and .env files get pasted
    with the angle brackets still attached — which happened here and cost an
    afternoon: authentication failed with a vendor error that pointed at the
    credential being wrong rather than at two stray characters.

    Quotes get the same treatment. A shell strips them; docker compose's
    env_file does not, so KEY="abc" arrives as the five characters "abc" with
    the quotes included.

    Only wrapping characters are removed. Anything inside is left untouched —
    a secret containing a quote is still a valid secret.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == "<" and cleaned[-1] == ">":
        cleaned = cleaned[1:-1].strip()
    for quote in ('"', "'"):
        if len(cleaned) >= 2 and cleaned[0] == quote and cleaned[-1] == quote:
            cleaned = cleaned[1:-1].strip()
    return cleaned or None


class EnvSecretsProvider(SecretsProviderInterface):
    async def get_credential(self, secrets_ref: str) -> Credential:
        username = _clean(os.environ.get(_env_key(secrets_ref, "USERNAME")))
        password = _clean(os.environ.get(_env_key(secrets_ref, "PASSWORD")))
        api_key = _clean(os.environ.get(_env_key(secrets_ref, "APIKEY")))

        if username is None and password is None and api_key is None:
            raise SecretNotFoundError(secrets_ref)

        return Credential(username=username, password=password, api_key=api_key)

    async def healthy(self) -> bool:
        return True
