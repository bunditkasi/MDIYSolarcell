"""Map pin classification.

Pure logic, no database — this is the payoff of keeping the rule in the domain
layer. The frontend mirrors these cases in
``frontend/src/lib/pr-status.ts``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.models import PRStatus
from app.domain.status import classify_pr_status

GREEN_AT = Decimal("75")


def classify(**overrides: object) -> PRStatus:
    kwargs: dict[str, object] = {
        "performance_ratio": Decimal("80"),
        "is_online": True,
        "has_string_anomaly": False,
        "has_critical_alert": False,
        "green_threshold": GREEN_AT,
    }
    kwargs.update(overrides)
    return classify_pr_status(**kwargs)  # type: ignore[arg-type]


def test_healthy_site_is_green() -> None:
    assert classify(performance_ratio=Decimal("82.4")) is PRStatus.GREEN


def test_threshold_is_inclusive() -> None:
    """Spec says "PR >= 75%", so exactly 75 must be green, not yellow."""
    assert classify(performance_ratio=GREEN_AT) is PRStatus.GREEN
    assert classify(performance_ratio=Decimal("74.99")) is PRStatus.YELLOW


def test_underperforming_site_is_yellow() -> None:
    assert classify(performance_ratio=Decimal("61.2")) is PRStatus.YELLOW


def test_string_anomaly_is_yellow_even_at_good_pr() -> None:
    """A failed string can hide behind a healthy fleet-level PR."""
    assert classify(performance_ratio=Decimal("95"), has_string_anomaly=True) is PRStatus.YELLOW


def test_offline_site_is_red() -> None:
    assert classify(is_online=False) is PRStatus.RED


def test_offline_beats_a_good_stale_pr() -> None:
    """Yesterday's good number must not paint a dead site green today."""
    assert classify(performance_ratio=Decimal("99"), is_online=False) is PRStatus.RED


def test_critical_alert_is_red() -> None:
    assert classify(performance_ratio=Decimal("99"), has_critical_alert=True) is PRStatus.RED


def test_missing_pr_is_unknown_not_red() -> None:
    """No irradiance baseline means unknown, not broken.

    Reporting UNKNOWN as RED would send technicians to healthy sites; reporting
    it as GREEN would hide real faults.
    """
    assert classify(performance_ratio=None) is PRStatus.UNKNOWN


def test_missing_pr_still_red_when_offline() -> None:
    assert classify(performance_ratio=None, is_online=False) is PRStatus.RED


@pytest.mark.parametrize("threshold", [Decimal("70"), Decimal("75"), Decimal("80")])
def test_threshold_is_configurable(threshold: Decimal) -> None:
    """Thresholds come from settings, never hard-coded."""
    assert classify(performance_ratio=threshold, green_threshold=threshold) is PRStatus.GREEN
    assert (
        classify(performance_ratio=threshold - Decimal("0.01"), green_threshold=threshold)
        is PRStatus.YELLOW
    )
