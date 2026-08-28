"""Guards the Phase 2 handover contract.

The whole architecture rests on one property: ``app.domain`` knows nothing about
how data is stored. That property is easy to state, easy to agree with, and very
easy to break by accident — one convenient import of a SQLAlchemy type into a
domain model and the seam is gone, with nothing failing until Phase 2 begins.

These tests make that failure immediate.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "app" / "domain"

#: Import roots the domain layer must never reach for.
FORBIDDEN_ROOTS = {
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "alembic",
    "fastapi",
    "starlette",
    "pydantic",
    "redis",
    "arq",
    "httpx",
    "hvac",
    "playwright",
}


def _domain_modules() -> list[Path]:
    return sorted(p for p in DOMAIN_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", _domain_modules(), ids=lambda p: p.name)
def test_domain_module_has_no_infrastructure_imports(path: Path) -> None:
    """No domain module may import a storage, transport or framework package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue

        for name in names:
            if name and name.split(".")[0] in FORBIDDEN_ROOTS:
                offenders.append(f"{path.name}:{node.lineno} imports {name}")

    assert not offenders, (
        "app.domain must stay free of infrastructure imports so Phase 2 can "
        "swap the database.\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "module",
    [
        "app.domain.models",
        "app.domain.filters",
        "app.domain.status",
        "app.domain.exceptions",
        "app.domain.repositories",
    ],
)
def test_domain_module_imports_cleanly(module: str) -> None:
    """Each domain module must import on its own."""
    assert importlib.import_module(module) is not None


def test_domain_imports_without_any_infrastructure_installed() -> None:
    """The domain layer must import in a bare interpreter with no database.

    Run as a subprocess so nothing another test already imported can mask a
    missing dependency. This mirrors the acceptance check in the plan:

        docker compose exec backend python -c "import app.domain.repositories"
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.domain.repositories, app.domain.models, app.domain.status",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, (
        "Importing app.domain failed in a clean interpreter:\n" + result.stderr
    )


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------


def test_env_credentials_strip_placeholder_wrappers(monkeypatch) -> None:
    """A credential pasted as <value> or "value" must still work.

    This exact mistake — angle brackets copied along with the placeholder from
    the setup instructions — produced a vendor authentication failure whose
    message pointed at the credential being wrong rather than at two stray
    characters, and cost an afternoon to find.
    """
    import asyncio

    from app.core.secrets.env import EnvSecretsProvider

    monkeypatch.setenv("SECRET__DEMO__APIKEY", "<abc123>")
    monkeypatch.setenv("SECRET__DEMO__PASSWORD", '"s3cret"')
    monkeypatch.setenv("SECRET__DEMO__USERNAME", "  spaced  ")

    credential = asyncio.run(EnvSecretsProvider().get_credential("demo"))

    assert credential.api_key == "abc123"
    assert credential.password == "s3cret"
    assert credential.username == "spaced"


def test_env_credentials_keep_inner_punctuation(monkeypatch) -> None:
    """Only WRAPPING characters are stripped. A secret containing a quote or an
    angle bracket in the middle is still a valid secret."""
    import asyncio

    from app.core.secrets.env import EnvSecretsProvider

    monkeypatch.setenv("SECRET__DEMO2__PASSWORD", 'a"b<c>d')
    credential = asyncio.run(EnvSecretsProvider().get_credential("demo2"))

    assert credential.password == 'a"b<c>d'
