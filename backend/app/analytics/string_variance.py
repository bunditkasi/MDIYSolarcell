"""Peer comparison for per-panel fault detection.

Specification section 3: compare across strings on the same MPPT and alert when
variance exceeds 10% — detecting a failing string without adding hardware.

TWO HARDWARE TOPOLOGIES, TWO COMPARISONS
----------------------------------------
The fleet runs both, and the rule is not the same for each.

STRING inverters (Huawei FusionSolar) — compare CURRENT within one MPPT.
    An MPPT tracks its own maximum power point, so two strings on different
    MPPTs can legitimately sit at different operating currents under identical
    irradiance; comparing across MPPTs generates constant false alarms. Strings
    on the SAME MPPT are electrically forced to share a voltage, so their
    currents must track closely. Current, not power, because shared voltage
    makes power variance merely current variance restated — and current is what
    an engineer will confirm with a clamp meter.

MICROINVERTERS (Atmoce Cloud) — compare POWER across panels at one site.
    Each panel has its own inverter, so there is no MPPT and no string. Verified
    against the Atmoce-Cloud API Reference v1.2.2 in full: it exposes no PV
    voltage and no PV current anywhere, only per-branch power via
    ``pvData[].pvPower``. Current comparison is therefore impossible on this
    hardware. Panels at one site share irradiance, which is what makes their
    power outputs comparable — so the peer group is the site, not the MPPT.

Everything below is a pure function. No database, no HTTP, no settings lookup:
the fault rule stays testable without infrastructure, and a Phase 2 backend
reuses it unchanged.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from app.domain.models import MeasurementBasis

__all__ = [
    "MIN_MEANINGFUL_CURRENT_A",
    "MIN_MEANINGFUL_POWER_KW",
    "MIN_PEERS_FOR_COMPARISON",
    "StringAnomaly",
    "StringReading",
    "analyse_device_strings",
    "analyse_mppt_group",
    "analyse_site_panels",
]

#: Below this current a string is effectively idle — night, heavy cloud, or an
#: inverter that has stopped tracking. Percentage deviations between near-zero
#: readings are numerically enormous and physically meaningless, so the rule is
#: suspended rather than allowed to fire all night, every night.
MIN_MEANINGFUL_CURRENT_A = Decimal("0.5")

#: The same idea for microinverters. A typical panel is 400-600 W, so 0.03 kW
#: is comfortably into "not really generating" territory.
MIN_MEANINGFUL_POWER_KW = Decimal("0.03")

#: Two peers cannot establish a norm: with n=2 the median is just their mean, so
#: a single failure makes BOTH look 50% off and there is no way to tell which
#: one broke. Three is the minimum where a majority can define "normal".
MIN_PEERS_FOR_COMPARISON = 3

_UNITS = {MeasurementBasis.STRING: "A", MeasurementBasis.PANEL: "kW"}


@dataclass(frozen=True, slots=True)
class StringReading:
    """One string, or — for microinverters — one panel.

    ``pv_current`` is None for microinverters, which report no current at all;
    ``pv_power_kw`` carries the signal instead. For microinverters
    ``mppt_index`` is always 0 and ``string_index`` is the panel number.
    """

    device_id: UUID
    mppt_index: int
    string_index: int
    pv_current: Decimal | None
    pv_voltage: Decimal | None = None
    pv_power_kw: Decimal | None = None

    def value_for(self, basis: MeasurementBasis) -> Decimal | None:
        """The quantity this basis compares on."""
        if basis is MeasurementBasis.STRING:
            return self.pv_current
        return self.pv_power_kw


@dataclass(frozen=True, slots=True)
class StringAnomaly:
    #: Which quantity was compared, so a reader of the alert knows whether
    #: "8.40" means amps or kilowatts.
    basis: MeasurementBasis
    device_id: UUID
    mppt_index: int
    string_index: int
    measured: Decimal
    #: Peer reference — the median of the group.
    expected: Decimal
    #: Signed deviation from the reference, in percent. Negative means
    #: underperforming, which is the case that matters operationally.
    deviation_pct: Decimal
    peer_count: int

    @property
    def is_underperforming(self) -> bool:
        return self.deviation_pct < 0

    @property
    def unit(self) -> str:
        return _UNITS[self.basis]

    def describe(self) -> str:
        direction = "below" if self.is_underperforming else "above"
        unit = self.unit
        if self.basis is MeasurementBasis.PANEL:
            subject = f"Panel {self.string_index}"
            peers = "peer panels at this site"
        else:
            subject = f"MPPT {self.mppt_index} string {self.string_index}"
            peers = "peer strings on the same MPPT"
        return (
            f"{subject}: {self.measured}{unit} is {abs(self.deviation_pct)}% "
            f"{direction} the {self.expected}{unit} median of "
            f"{self.peer_count} {peers}"
        )


def _compare_group(
    readings: list[StringReading],
    *,
    basis: MeasurementBasis,
    threshold_pct: Decimal,
    floor: Decimal,
) -> list[StringAnomaly]:
    """Flag outliers within one peer group.

    The reference is the MEDIAN, never the mean. With a mean, one dead unit
    drags the reference down: the dead unit looks less broken and its healthy
    peers drift toward the threshold — the failure corrupts the very baseline
    used to detect it. The median is unmoved by a minority of outliers, which is
    precisely the situation being detected.
    """
    live = [
        (r, value)
        for r in readings
        if (value := r.value_for(basis)) is not None and value >= floor
    ]

    if len(live) < MIN_PEERS_FOR_COMPARISON:
        return []

    median = Decimal(str(statistics.median([value for _, value in live])))
    if median <= 0:
        return []

    anomalies: list[StringAnomaly] = []
    for reading, value in live:
        deviation = ((value - median) / median * Decimal("100")).quantize(Decimal("0.01"))
        if abs(deviation) >= threshold_pct:
            anomalies.append(
                StringAnomaly(
                    basis=basis,
                    device_id=reading.device_id,
                    mppt_index=reading.mppt_index,
                    string_index=reading.string_index,
                    measured=value,
                    expected=median.quantize(Decimal("0.01")),
                    deviation_pct=deviation,
                    peer_count=len(live),
                )
            )

    return sorted(anomalies, key=lambda a: a.deviation_pct)


def analyse_mppt_group(
    readings: list[StringReading],
    *,
    threshold_pct: Decimal,
    min_current: Decimal = MIN_MEANINGFUL_CURRENT_A,
) -> list[StringAnomaly]:
    """Compare strings within a single MPPT, on current. String inverters only."""
    return _compare_group(
        readings,
        basis=MeasurementBasis.STRING,
        threshold_pct=threshold_pct,
        floor=min_current,
    )


def analyse_device_strings(
    readings: list[StringReading],
    *,
    threshold_pct: Decimal,
    min_current: Decimal = MIN_MEANINGFUL_CURRENT_A,
) -> list[StringAnomaly]:
    """Run the current comparison across every MPPT on one string inverter.

    Readings are grouped by ``mppt_index`` first. Skipping that grouping — and
    comparing every string on the device against every other — is what makes
    naive implementations of this check unusable in the field.
    """
    groups: dict[int, list[StringReading]] = {}
    for reading in readings:
        groups.setdefault(reading.mppt_index, []).append(reading)

    anomalies: list[StringAnomaly] = []
    for mppt_index in sorted(groups):
        anomalies.extend(
            analyse_mppt_group(
                groups[mppt_index],
                threshold_pct=threshold_pct,
                min_current=min_current,
            )
        )
    return anomalies


def analyse_site_panels(
    readings: list[StringReading],
    *,
    threshold_pct: Decimal,
    min_power_kw: Decimal = MIN_MEANINGFUL_POWER_KW,
) -> list[StringAnomaly]:
    """Compare microinverter panels across a whole site, on power.

    The peer group is every panel at the site, spanning microinverters, because
    they share one roof and therefore one irradiance. Restricting the comparison
    to panels on a single microinverter would usually leave only one or two
    peers — below the minimum needed to establish a norm at all.

    Caveat worth knowing before acting on a result: panels on differently
    oriented roof planes do NOT share irradiance and will deviate for entirely
    healthy reasons. Where a site has multiple orientations, pass one plane's
    panels at a time rather than the whole site.
    """
    return _compare_group(
        readings,
        basis=MeasurementBasis.PANEL,
        threshold_pct=threshold_pct,
        floor=min_power_kw,
    )
