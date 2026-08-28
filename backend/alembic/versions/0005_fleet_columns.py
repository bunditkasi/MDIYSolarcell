"""Battery capacity and month-to-date yield

Revision ID: 0005_fleet_cols
Revises: 0004_vendor_links
Create Date: 2026-08-28

WHY
---
The fleet table has to report battery storage and month-to-date generation
alongside the figures already held. Both arrive on every sweep and were being
discarded.

``telemetry_raw.monthly_yield_kwh`` stores the VENDOR's month-to-date total
rather than a figure derived from our own rows. Deriving it looks tempting —
the data is right there — but our history begins when ingestion was switched
on, so a derived total under-reports every branch until a full month has passed,
and would silently disagree with the vendor's own portal in the meantime.

``stores.battery_capacity_kwh`` is an asset property, so it belongs on the store
rather than on a reading. It is nullable and stays null for Huawei, whose
station list publishes no such field: a branch that did not report its storage
must not be shown a confident zero.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_fleet_cols"
down_revision: str | None = "0004_vendor_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("battery_capacity_kwh", sa.Numeric(10, 2), nullable=True),
    )
    # Same precision as total_yield_kwh: a month total is the same order of
    # magnitude as a lifetime one for a site commissioned this year.
    op.add_column(
        "telemetry_raw",
        sa.Column("monthly_yield_kwh", sa.Numeric(14, 3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telemetry_raw", "monthly_yield_kwh")
    op.drop_column("stores", "battery_capacity_kwh")
