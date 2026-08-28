"""Specific yield as the fallback that decides a pin's colour.

Without a Solcast key PR% cannot be computed for any branch, and every pin in
the fleet reads UNKNOWN — a map that cannot tell a healthy site from a dead one.
The fallback measures each branch's kWh/kWp against the FLEET MEDIAN for the
same day, which needs no irradiance data and self-calibrates for time of day and
weather.

These tests pin the boundaries of that fallback, especially the cases where it
must decline to answer. A wrong "healthy" here is worse than no answer at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.domain.models import PRStatus
from app.domain.status import classify_pr_status
from app.infrastructure.repositories.local_postgres import (
    _vendor_staleness_case,
    _yield_vs_peers,
)

GREEN_AT = Decimal("75")
YIELD_AT = Decimal("80")


def classify(**overrides: object) -> PRStatus:
    kwargs: dict[str, object] = {
        "performance_ratio": None,
        "is_online": True,
        "has_string_anomaly": False,
        "has_critical_alert": False,
        "green_threshold": GREEN_AT,
        "yield_vs_peers_pct": None,
        "yield_green_threshold": YIELD_AT,
    }
    kwargs.update(overrides)
    return classify_pr_status(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_yield_decides_when_pr_is_unavailable() -> None:
    assert classify(yield_vs_peers_pct=Decimal("96.0")) is PRStatus.GREEN
    assert classify(yield_vs_peers_pct=Decimal("41.0")) is PRStatus.YELLOW


def test_threshold_is_inclusive() -> None:
    assert classify(yield_vs_peers_pct=YIELD_AT) is PRStatus.GREEN


def test_pr_still_wins_when_both_are_available() -> None:
    """PR measures against actual irradiance; the peer median only approximates it.

    A branch under a passing cloud can sit far below its peers while performing
    perfectly for the light it is receiving, and PR is the measure that knows
    the difference.
    """
    assert (
        classify(
            performance_ratio=Decimal("88"),
            yield_vs_peers_pct=Decimal("20"),
        )
        is PRStatus.GREEN
    )


def test_unknown_survives_when_neither_measure_exists() -> None:
    assert classify(yield_vs_peers_pct=None) is PRStatus.UNKNOWN


def test_offline_outranks_a_good_yield() -> None:
    """An offline site reports nothing fresh, so its yield figure is yesterday's."""
    assert (
        classify(is_online=False, yield_vs_peers_pct=Decimal("140")) is PRStatus.RED
    )


def test_string_anomaly_outranks_a_good_yield() -> None:
    """A site can hit its fleet-median output with one string dead.

    Site-level yield cannot see that; the string comparison can, so it wins.
    """
    assert (
        classify(has_string_anomaly=True, yield_vs_peers_pct=Decimal("99"))
        is PRStatus.YELLOW
    )


# --------------------------------------------------------------------------
# The ratio itself
# --------------------------------------------------------------------------


def ratio(**overrides: object) -> Decimal | None:
    kwargs: dict[str, object] = {
        "specific_yield": Decimal("4.0"),
        "median_yield": Decimal("5.0"),
        "peer_count": 40,
        "min_peers": 5,
    }
    kwargs.update(overrides)
    return _yield_vs_peers(**kwargs)  # type: ignore[arg-type]


def test_ratio_is_a_percentage_of_the_median() -> None:
    assert ratio() == Decimal("80.0")


def test_zero_output_against_producing_peers_is_a_signal_not_a_gap() -> None:
    """The branch that produced nothing today is exactly what this must catch."""
    assert ratio(specific_yield=Decimal("0")) == Decimal("0.0")
    assert classify(yield_vs_peers_pct=Decimal("0.0")) is PRStatus.YELLOW


def test_too_few_peers_declines_to_answer() -> None:
    """With four branches reporting, the median is an accident of which four."""
    assert ratio(peer_count=4) is None


@pytest.mark.parametrize("median", [Decimal("0"), Decimal("-1")])
def test_non_positive_median_declines_to_answer(median: Decimal) -> None:
    """Before first light every branch reads zero.

    Dividing by that median would recolour the entire fleet at dawn on nothing
    but rounding noise.
    """
    assert ratio(median_yield=median) is None


def test_missing_capacity_declines_to_answer() -> None:
    """A branch with no recorded kWp — PNGN — has no yield to compare."""
    assert ratio(specific_yield=None) is None


# --------------------------------------------------------------------------
# Per-vendor staleness
# --------------------------------------------------------------------------


def test_huawei_offline_threshold_follows_its_own_cadence() -> None:
    """The two numbers cannot be chosen independently.

    Huawei is polled every two hours, so a Huawei branch can never look fresher
    than two hours old. Judging it by Atmoce's 15-minute threshold would mark
    every Huawei branch offline permanently.
    """
    settings = Settings(
        ingestion_poll_interval_min=15,
        ingestion_poll_interval_min_huawei=120,
        device_offline_after_minutes=None,
    )
    assert settings.offline_after_minutes_for("atmoce") == 37
    assert settings.offline_after_minutes_for("huawei") == 300
    assert settings.offline_after_minutes_for("huawei") > 120


def test_explicit_override_applies_to_every_vendor() -> None:
    settings = Settings(device_offline_after_minutes=45)
    assert settings.offline_after_minutes_for("atmoce") == 45
    assert settings.offline_after_minutes_for("huawei") == 45
    assert settings.offline_thresholds_by_vendor == {}


def test_staleness_case_binds_its_values_and_casts_them() -> None:
    """Untyped binds inside a CASE resolve to text, and make_interval rejects it.

    The CAST is what keeps the query executable; without it the fleet query
    fails outright the moment a vendor override is configured.
    """
    sql, params = _vendor_staleness_case({"huawei": 300})
    assert ":vendor_key_0" in sql
    assert "CAST(:vendor_mins_0 AS INTEGER)" in sql
    assert "CAST(:offline_minutes AS INTEGER)" in sql
    assert params == {"vendor_key_0": "huawei", "vendor_mins_0": 300}


def test_no_overrides_uses_the_single_threshold() -> None:
    sql, params = _vendor_staleness_case({})
    assert sql == ":offline_minutes"
    assert params == {}
