"""Manual vendor site -> branch links

Revision ID: 0004_vendor_links
Revises: 0003_unsized
Create Date: 2026-08-28

WHY
---
Most vendor sites carry the 4-letter branch code in their name, so they match
automatically. Some do not: the Huawei account for Mueang Nan names its site
"MR.DIYNAN", which contains no code at all.

The tempting fix is to loosen the matcher — accept fuzzy names, or guess from
the province. That is worse than leaving it unmatched: a wrong link silently
attributes one branch's generation to another, and nothing downstream can tell.
Every ESG and financial figure built on it would be wrong with no symptom.

So the ambiguous cases get an explicit, human-decided row here instead. The
matcher stays strict, and the exceptions are visible, auditable, and attributed
to whoever decided them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_vendor_links"
down_revision: str | None = "0003_unsized"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vendor_site_links",
        sa.Column(
            "link_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("vendor_key", sa.String(64), nullable=False),
        sa.Column("vendor_site_id", sa.String(128), nullable=False),
        sa.Column(
            "store_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stores.store_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Why this link exists, in the words of whoever made it. A mapping with
        # no rationale is impossible to audit a year later.
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # One vendor site maps to at most one branch. Without this a second link
        # would quietly double-count that site's generation.
        sa.UniqueConstraint("vendor_key", "vendor_site_id", name="uq_vendor_site_link"),
    )
    op.create_index(
        "idx_vendor_site_links_store", "vendor_site_links", ["store_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_vendor_site_links_store", table_name="vendor_site_links")
    op.drop_table("vendor_site_links")
