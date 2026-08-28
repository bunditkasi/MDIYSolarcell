"""Baseline — schema created by db/init/02_schema.sql

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-27

WHY THIS REVISION IS EMPTY
--------------------------
The Phase 1 schema is created by db/init/02_schema.sql, which the PostgreSQL
container runs once when its data volume is empty. That file is the readable
artefact handed to corporate IT for Phase 2 — a single annotated SQL document
beats reading it back out of Python migration code.

This revision exists so Alembic has a known starting point. Running
``alembic upgrade head`` against a freshly initialised database records this id
in ``alembic_version`` and changes nothing else.

Every schema change from here on gets its own revision. Nothing may be added to
02_schema.sql after the first deployment — an existing volume never re-runs it,
so an edit there would apply to new environments and silently skip existing
ones, and the two would drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentionally empty. See the module docstring.
    pass


def downgrade() -> None:
    # There is nothing below the baseline to downgrade to.
    pass
