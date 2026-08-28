"""Performance-Ratio status classification.

Single source of truth for map pin colour. The frontend mirrors this logic in
``frontend/src/lib/pr-status.ts`` but reads its thresholds from the API rather
than hard-coding them, so the two cannot drift apart.

Pure function, no I/O — thresholds are passed in by the caller from settings.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.models import PRStatus

__all__ = ["classify_pr_status"]


def classify_pr_status(
    *,
    performance_ratio: Decimal | None,
    is_online: bool,
    has_string_anomaly: bool,
    has_critical_alert: bool,
    green_threshold: Decimal,
    yield_vs_peers_pct: Decimal | None = None,
    yield_green_threshold: Decimal | None = None,
    has_ever_reported: bool = True,
) -> PRStatus:
    """Classify a store into a map pin colour.

    Specification section 3:
        GREEN  = PR >= 75%
        YELLOW = PR < 75% or string anomaly
        RED    = offline / critical

    Order matters. RED is evaluated first because an offline site reports no
    fresh telemetry at all: whatever stale PR value remains is meaningless, and
    letting a good yesterday paint the pin green would hide a dead site.

    When PR cannot be computed — no Solcast irradiance baseline — the fallback
    is ``yield_vs_peers_pct``: today's specific yield (kWh/kWp) as a percentage
    of the fleet median for the same day. It needs no irradiance data, only
    telemetry we already hold, and it self-calibrates for time of day and
    weather because every branch in the comparison sits under the same sky.

    UNKNOWN survives for the case where neither is available: no reading at
    all, or no recorded capacity to divide by. Reporting that as RED would
    generate false call-outs; reporting it as GREEN would hide real faults.

    ``has_ever_reported`` separates a branch that WENT DOWN from one that was
    never connected. Both are silent, but only the first is an incident. A
    branch no vendor account has ever delivered data for is waiting on access
    or commissioning, and colouring it RED sends a technician to a working shop
    — while burying any genuine outage among a dozen permanent false alarms.
    """
    if has_critical_alert:
        return PRStatus.RED

    if not has_ever_reported:
        # Never seen. Not an outage, and deliberately not GREEN either: we know
        # nothing about this plant, which is exactly what UNKNOWN means.
        return PRStatus.UNKNOWN

    if not is_online:
        return PRStatus.RED

    if has_string_anomaly:
        return PRStatus.YELLOW

    if performance_ratio is not None:
        return (
            PRStatus.GREEN
            if performance_ratio >= green_threshold
            else PRStatus.YELLOW
        )

    # No irradiance baseline. Fall back to peer-relative specific yield, which
    # is the same principle the string-level fault detection already uses: a
    # unit is judged against comparable units, not against an absolute figure.
    if yield_vs_peers_pct is not None and yield_green_threshold is not None:
        return (
            PRStatus.GREEN
            if yield_vs_peers_pct >= yield_green_threshold
            else PRStatus.YELLOW
        )

    return PRStatus.UNKNOWN
