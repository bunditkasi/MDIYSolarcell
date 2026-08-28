"""TimescaleDB implementation of the telemetry reads behind the site pages.

Everything here is deliberately aggregated in SQL. A year of 15-minute readings
is roughly 35,000 rows per device; a branch with six inverters is 200,000. Those
numbers are fine for TimescaleDB to sum and hopeless to ship across the wire and
add up in Python.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.exceptions import RepositoryError
from app.domain.models import EnergyBucket, PanelSnapshot

__all__ = ["LocalPostgresTelemetryRepository"]

#: Whitelist, not interpolation. The granularity reaches the SQL string itself
#: (date_trunc takes a literal), so it must never come from user input directly.
_GRANULARITY = {"day": "day", "month": "month", "year": "year"}


class LocalPostgresTelemetryRepository:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def get_energy_history(
        self,
        store_id: UUID,
        *,
        granularity: str,
        start: date,
        end: date,
    ) -> list[EnergyBucket]:
        bucket = _GRANULARITY.get(granularity)
        if bucket is None:
            raise ValueError(
                f"granularity must be one of {sorted(_GRANULARITY)}, got {granularity!r}"
            )

        # daily_yield_kwh is a RESETTING counter: it climbs through the day and
        # returns to zero at midnight. The energy for a day is therefore its
        # MAXIMUM, not the sum of its samples — summing would multiply a day's
        # output by the number of readings taken.
        sql = text(
            f"""
            WITH per_device_day AS (
                SELECT d.device_id,
                       date_trunc('day', t."time" AT TIME ZONE :tz)::date AS day,
                       MAX(t.daily_yield_kwh) AS produced_kwh,
                       COUNT(*)               AS sample_count
                  FROM telemetry_raw t
                  JOIN devices d ON d.device_id = t.device_id
                 WHERE d.store_id = :store_id
                   AND t."time" >= (:start)::timestamp AT TIME ZONE :tz
                   AND t."time" <  ((:end)::date + 1)::timestamp AT TIME ZONE :tz
                 GROUP BY d.device_id, day
            )
            SELECT date_trunc('{bucket}', day)::date AS period,
                   SUM(produced_kwh)                 AS produced_kwh,
                   COUNT(DISTINCT device_id)         AS device_count,
                   SUM(sample_count)                 AS sample_count
              FROM per_device_day
             GROUP BY period
             ORDER BY period DESC
            """
        )

        try:
            rows = (
                await self._session.execute(
                    sql,
                    {
                        "store_id": store_id,
                        "start": start,
                        "end": end,
                        "tz": self._settings.ingestion_timezone,
                    },
                )
            ).mappings().all()
        except SQLAlchemyError as exc:
            raise RepositoryError(
                f"Failed to load energy history for store {store_id}", cause=exc
            ) from exc

        return [
            EnergyBucket(
                period=row["period"],
                produced_kwh=_round(row["produced_kwh"]),
                device_count=int(row["device_count"] or 0),
                sample_count=int(row["sample_count"] or 0),
            )
            for row in rows
        ]

    async def get_panel_snapshot(
        self, store_id: UUID, *, on_date: date
    ) -> list[PanelSnapshot]:
        """Per-panel energy for a day, each panel scored against its peers.

        The peer group is chosen by hardware topology, which is why
        ``measurement_basis`` exists:

          PANEL (microinverters) — peers are every panel at the SITE. They share
            one roof and therefore one irradiance, and a single microinverter
            usually drives too few panels to form a group on its own.

          STRING (string inverters) — peers are the strings on the SAME MPPT.
            Separate MPPTs track independently and legitimately differ, so
            comparing across them is the classic false-alarm generator.

        The median is the reference, never the mean: one dead panel drags a mean
        down, flattering the dead panel and pulling its healthy neighbours toward
        the threshold. The failure would corrupt the very baseline used to
        detect it.
        """
        sql = text(
            """
            WITH panel_day AS (
                SELECT ts.device_id,
                       d.serial_number,
                       ts.mppt_index,
                       ts.string_index,
                       -- The peer key encodes the topology rule: microinverter
                       -- panels are compared site-wide, strings only against
                       -- others on the same MPPT.
                       CASE
                           WHEN d.measurement_basis = 'PANEL' THEN 'site'
                           ELSE ts.device_id::text || ':' || ts.mppt_index
                       END AS peer_key,
                       -- The quantity compared depends on the hardware, and it
                       -- must match app/analytics/string_variance.py exactly or
                       -- the array view and the alerts disagree about the same
                       -- fault. STRING inverters share an MPPT voltage, so
                       -- current is the signal; microinverters report no current
                       -- at all, so power is all there is.
                       AVG(
                           CASE
                               WHEN d.measurement_basis = 'STRING' THEN ts.pv_current
                               ELSE ts.pv_power_kw
                           END
                       ) AS compare_value,
                       MAX(d.measurement_basis) AS measurement_basis,
                       AVG(ts.pv_power_kw)      AS avg_power_kw,
                       COUNT(*)                 AS samples
                  FROM telemetry_string ts
                  JOIN devices d ON d.device_id = ts.device_id
                 WHERE d.store_id = :store_id
                   AND ts."time" >= (:on_date)::timestamp AT TIME ZONE :tz
                   AND ts."time" <  ((:on_date)::date + 1)::timestamp AT TIME ZONE :tz
                 GROUP BY ts.device_id, d.serial_number, d.measurement_basis,
                          ts.mppt_index, ts.string_index
            ),
            -- PERCENTILE_CONT is an ordered-set aggregate and PostgreSQL does
            -- not accept it as a window function, so the median is computed per
            -- group here and joined back rather than taken with OVER().
            peer_groups AS (
                SELECT peer_key,
                       -- Cast back to NUMERIC: PERCENTILE_CONT returns double
                       -- precision, and mixing that with the NUMERIC average
                       -- gives a float/Decimal type error on the way out.
                       (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY compare_value))::numeric
                           AS peer_median,
                       COUNT(*) AS peer_count
                  FROM panel_day
                 WHERE compare_value IS NOT NULL
                 GROUP BY peer_key
            )
            SELECT p.device_id, p.serial_number, p.mppt_index, p.string_index,
                   p.avg_power_kw, p.compare_value, p.samples,
                   g.peer_median, COALESCE(g.peer_count, 0) AS peer_count
              FROM panel_day p
              LEFT JOIN peer_groups g ON g.peer_key = p.peer_key
             ORDER BY p.serial_number, p.mppt_index, p.string_index
            """
        )

        try:
            rows = (
                await self._session.execute(
                    sql,
                    {
                        "store_id": store_id,
                        "on_date": on_date,
                        "tz": self._settings.ingestion_timezone,
                    },
                )
            ).mappings().all()
        except SQLAlchemyError as exc:
            raise RepositoryError(
                f"Failed to load panel snapshot for store {store_id}", cause=exc
            ) from exc

        threshold = self._settings.string_variance_threshold_pct
        out: list[PanelSnapshot] = []

        for row in rows:
            # Normalised rather than trusted: numeric types coming back from a
            # driver are not guaranteed to all be Decimal, and mixing Decimal
            # with float raises rather than coercing.
            avg_power = _as_decimal(row["avg_power_kw"])
            # Deviation is measured on compare_value (current or power per the
            # hardware), NOT on avg_power_kw — which is only ever displayed.
            compare = _as_decimal(row["compare_value"])
            median = _as_decimal(row["peer_median"])
            peers = int(row["peer_count"] or 0)

            deviation: Decimal | None = None
            # Three is the minimum peer group that means anything: with two, a
            # single fault makes BOTH look equally wrong and there is no way to
            # say which one broke.
            if compare is not None and median is not None and median > 0 and peers >= 3:
                deviation = ((compare - median) / median * Decimal("100")).quantize(
                    Decimal("0.01")
                )

            # Energy for the day, from mean power over the samples taken. The
            # interval is the vendor's reporting cadence, not a fixed constant.
            interval_hours = Decimal(self._settings.ingestion_poll_interval_min) / Decimal(60)
            produced = (
                (avg_power * Decimal(int(row["samples"])) * interval_hours).quantize(
                    Decimal("0.001")
                )
                if avg_power is not None
                else None
            )

            out.append(
                PanelSnapshot(
                    device_id=row["device_id"],
                    serial_number=row["serial_number"],
                    mppt_index=int(row["mppt_index"]),
                    string_index=int(row["string_index"]),
                    produced_kwh=produced,
                    avg_power_kw=_round(avg_power, "0.001"),
                    deviation_pct=deviation,
                    is_anomalous=deviation is not None and abs(deviation) >= threshold,
                )
            )

        return out


def _as_decimal(value: Any) -> Decimal | None:
    """Coerce a driver numeric to Decimal, or None."""
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round(value: Any, places: str = "0.001") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places))
