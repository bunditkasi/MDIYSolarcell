"""Allow branches with no capacity recorded yet

Revision ID: 0003_unsized
Revises: 0002_microinverter
Create Date: 2026-08-28

WHY
---
A vendor cloud can report a branch before anyone has recorded how big it is —
Atmoce does exactly this for at least one MR.DIY site. The choice was between
dropping that branch (it disappears from the fleet entirely) and inventing a
capacity (every kWh/kWp figure derived from it becomes wrong, silently).

Neither is acceptable, so capacity becomes nullable. The branch appears in
listings and on the map, is badged as incomplete in the UI, and is excluded
from any capacity-weighted figure until a real number arrives.

The positive-value CHECK is kept for the case where a number IS present.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_unsized"
down_revision: str | None = "0002_microinverter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "stores", "installed_kwp", existing_type=sa.Numeric(10, 2), nullable=True
    )
    op.drop_constraint("ck_stores_kwp_positive", "stores", type_="check")
    op.create_check_constraint(
        "ck_stores_kwp_positive",
        "stores",
        "installed_kwp IS NULL OR installed_kwp > 0",
    )
    op.execute(
        """
        COMMENT ON COLUMN stores.installed_kwp IS
        'NULL means the capacity is not yet known — usually a branch a vendor '
        'cloud reported before the roster caught up. Never default it to zero: '
        'that would corrupt every kWh/kWp comparison it takes part in.'
        """
    )


def downgrade() -> None:
    # Rows with no capacity cannot survive a NOT NULL column, and there is no
    # honest value to give them, so they are removed on the way back down.
    op.execute("DELETE FROM stores WHERE installed_kwp IS NULL")
    op.drop_constraint("ck_stores_kwp_positive", "stores", type_="check")
    op.create_check_constraint(
        "ck_stores_kwp_positive", "stores", "installed_kwp > 0"
    )
    op.alter_column(
        "stores", "installed_kwp", existing_type=sa.Numeric(10, 2), nullable=False
    )
