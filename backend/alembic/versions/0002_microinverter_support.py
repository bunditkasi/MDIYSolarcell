"""Distinguish string inverters from microinverters

Revision ID: 0002_microinverter
Revises: 0001_baseline
Create Date: 2026-08-27

WHY
---
The fleet runs two fundamentally different hardware topologies, and the
per-panel fault detection works differently on each:

  STRING inverter (Huawei FusionSolar)
      Several panels wired in series into one MPPT. The FusionSolar northbound
      API reports per-string voltage and current, so the specification's
      Intra-String Peer Comparison (compare I/V across strings on the same
      MPPT) works exactly as written.

  MICROINVERTER (Atmoce Cloud)
      One inverter per panel. There is no MPPT and no string. The Atmoce API
      (v1.2.2, verified across all 63 pages) exposes NO PV voltage and NO PV
      current anywhere — only per-branch POWER via `pvData[].pvPower`. The
      I/V comparison is therefore impossible on this hardware, and the
      equivalent check is a power comparison between panels at the same site.

Recording which basis a device uses is what stops the analytics layer from
silently applying the wrong rule — for example, comparing microinverter panels
"on the same MPPT" when every one of them reports mppt_index 0, or reporting
"no string data" for a device that never had any to give.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_microinverter"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # MICROINVERTER joins the allowed device types.
    op.drop_constraint("ck_devices_device_type", "devices", type_="check")
    op.create_check_constraint(
        "ck_devices_device_type",
        "devices",
        "device_type IN ('INVERTER', 'MICROINVERTER', 'METER', 'LOGGER', 'WEATHER_STATION')",
    )

    op.add_column(
        "devices",
        sa.Column("measurement_basis", sa.String(16), nullable=False, server_default="STRING"),
    )
    op.create_check_constraint(
        "ck_devices_measurement_basis",
        "devices",
        "measurement_basis IN ('STRING', 'PANEL')",
    )

    # Which vendor cloud this device's data comes from. Needed because the
    # analytics layer picks a comparison rule per vendor topology.
    op.add_column("devices", sa.Column("vendor_key", sa.String(64), nullable=True))
    op.create_index("idx_devices_measurement_basis", "devices", ["measurement_basis"])

    # Microinverters report per-panel power but no voltage or current, so a
    # telemetry_string row for them legitimately has pv_voltage / pv_current
    # NULL while pv_power_kw is present. Both were already nullable; this
    # constraint states the rule that at least ONE measurement must be present,
    # so a wholly empty row cannot be written.
    op.create_check_constraint(
        "ck_telemetry_string_has_measurement",
        "telemetry_string",
        "pv_voltage IS NOT NULL OR pv_current IS NOT NULL OR pv_power_kw IS NOT NULL",
    )

    op.execute(
        """
        COMMENT ON COLUMN devices.measurement_basis IS
        'STRING: per-MPPT string I/V available (Huawei). '
        'PANEL: per-panel power only, no I/V (Atmoce microinverters).'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN telemetry_string.mppt_index IS
        'Real MPPT index for string inverters. Always 0 for microinverters, '
        'which have no MPPT — string_index then carries the panel number.'
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_telemetry_string_has_measurement", "telemetry_string", type_="check")
    op.drop_index("idx_devices_measurement_basis", table_name="devices")
    op.drop_constraint("ck_devices_measurement_basis", "devices", type_="check")
    op.drop_column("devices", "vendor_key")
    op.drop_column("devices", "measurement_basis")

    op.drop_constraint("ck_devices_device_type", "devices", type_="check")
    op.create_check_constraint(
        "ck_devices_device_type",
        "devices",
        "device_type IN ('INVERTER', 'METER', 'LOGGER', 'WEATHER_STATION')",
    )
