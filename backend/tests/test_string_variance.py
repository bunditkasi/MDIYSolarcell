"""Intra-String Peer Comparison rules."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.analytics.string_variance import (
    StringReading,
    analyse_device_strings,
    analyse_mppt_group,
    analyse_site_panels,
)
from app.domain.models import MeasurementBasis

DEVICE = uuid4()
THRESHOLD = Decimal("10")


def reading(mppt: int, string: int, current: str) -> StringReading:
    return StringReading(
        device_id=DEVICE,
        mppt_index=mppt,
        string_index=string,
        pv_current=Decimal(current),
    )


def test_healthy_group_reports_nothing() -> None:
    group = [reading(0, i, "8.50") for i in range(4)]
    assert analyse_mppt_group(group, threshold_pct=THRESHOLD) == []


def test_small_spread_stays_under_threshold() -> None:
    group = [reading(0, 0, "8.50"), reading(0, 1, "8.40"), reading(0, 2, "8.60")]
    assert analyse_mppt_group(group, threshold_pct=THRESHOLD) == []


def test_failing_string_is_detected() -> None:
    group = [
        reading(0, 0, "8.50"),
        reading(0, 1, "8.50"),
        reading(0, 2, "8.50"),
        reading(0, 3, "6.00"),  # ~29% below the median
    ]
    anomalies = analyse_mppt_group(group, threshold_pct=THRESHOLD)

    assert len(anomalies) == 1
    assert anomalies[0].string_index == 3
    assert anomalies[0].is_underperforming
    assert anomalies[0].deviation_pct < Decimal("-10")


def test_median_reference_is_not_dragged_down_by_the_fault() -> None:
    """The whole point of using the median rather than the mean.

    With a mean reference, one dead string lowers the baseline: the dead string
    looks less broken and its healthy peers drift toward the threshold. Here the
    reference must stay at the healthy value, so exactly one string is flagged.
    """
    group = [
        reading(0, 0, "9.00"),
        reading(0, 1, "9.00"),
        reading(0, 2, "9.00"),
        reading(0, 3, "0.60"),  # near-total failure, still above min_current
    ]
    anomalies = analyse_mppt_group(group, threshold_pct=THRESHOLD)

    assert len(anomalies) == 1
    assert anomalies[0].expected == Decimal("9.00")


def test_strings_on_different_mppts_are_never_compared() -> None:
    """Separate MPPTs track independently, so a difference between them is
    normal. Comparing across them is the classic false-alarm generator."""
    readings = [
        # MPPT 0 running high, MPPT 1 running low — each internally consistent.
        reading(0, 0, "9.00"),
        reading(0, 1, "9.00"),
        reading(0, 2, "9.00"),
        reading(1, 0, "4.00"),
        reading(1, 1, "4.00"),
        reading(1, 2, "4.00"),
    ]
    assert analyse_device_strings(readings, threshold_pct=THRESHOLD) == []


def test_night_time_readings_are_ignored() -> None:
    """Percentage deviation between near-zero currents is meaningless and would
    otherwise fire an alert on every string, every night."""
    group = [
        reading(0, 0, "0.05"),
        reading(0, 1, "0.01"),
        reading(0, 2, "0.20"),
    ]
    assert analyse_mppt_group(group, threshold_pct=THRESHOLD) == []


def test_two_strings_are_not_enough_to_form_a_peer_group() -> None:
    """With n=2 a single fault makes BOTH strings look 50% off, so there is no
    way to tell which one is broken."""
    group = [reading(0, 0, "9.00"), reading(0, 1, "4.00")]
    assert analyse_mppt_group(group, threshold_pct=THRESHOLD) == []


def test_missing_current_values_are_skipped_not_treated_as_zero() -> None:
    group = [
        StringReading(DEVICE, 0, 0, pv_current=None),
        reading(0, 1, "8.50"),
        reading(0, 2, "8.50"),
        reading(0, 3, "8.50"),
    ]
    anomalies = analyse_mppt_group(group, threshold_pct=THRESHOLD)
    assert anomalies == []


def test_threshold_is_configurable() -> None:
    group = [
        reading(0, 0, "10.00"),
        reading(0, 1, "10.00"),
        reading(0, 2, "10.00"),
        reading(0, 3, "8.80"),  # exactly 12% below
    ]
    assert analyse_mppt_group(group, threshold_pct=Decimal("15")) == []
    assert len(analyse_mppt_group(group, threshold_pct=Decimal("10"))) == 1


def test_overperforming_string_is_reported_too() -> None:
    """A string reading far ABOVE its peers usually means a sensor or mapping
    fault, which is worth surfacing even though it is not a yield loss."""
    group = [
        reading(0, 0, "8.00"),
        reading(0, 1, "8.00"),
        reading(0, 2, "8.00"),
        reading(0, 3, "12.00"),
    ]
    anomalies = analyse_mppt_group(group, threshold_pct=THRESHOLD)
    assert len(anomalies) == 1
    assert not anomalies[0].is_underperforming


def test_anomalies_are_ordered_worst_first() -> None:
    group = [
        reading(0, 0, "10.00"),
        reading(0, 1, "10.00"),
        reading(0, 2, "10.00"),
        reading(0, 3, "8.00"),  # -20%
        reading(0, 4, "5.00"),  # -50%
    ]
    anomalies = analyse_mppt_group(group, threshold_pct=THRESHOLD)
    assert [a.string_index for a in anomalies] == [4, 3]


# ---------------------------------------------------------------------------
# Microinverter (panel) basis — Atmoce reports power only, never current.
# ---------------------------------------------------------------------------


def panel(index: int, power_kw: str) -> StringReading:
    """A microinverter panel reading: power present, current absent."""
    return StringReading(
        device_id=DEVICE,
        mppt_index=0,
        string_index=index,
        pv_current=None,
        pv_power_kw=Decimal(power_kw),
    )


def test_healthy_panels_report_nothing() -> None:
    panels = [panel(i, "0.480") for i in range(6)]
    assert analyse_site_panels(panels, threshold_pct=THRESHOLD) == []


def test_shaded_panel_is_detected_on_power() -> None:
    panels = [panel(i, "0.480") for i in range(5)] + [panel(5, "0.300")]
    anomalies = analyse_site_panels(panels, threshold_pct=THRESHOLD)

    assert len(anomalies) == 1
    assert anomalies[0].string_index == 5
    assert anomalies[0].basis is MeasurementBasis.PANEL
    assert anomalies[0].is_underperforming


def test_panel_anomaly_reports_kilowatts_not_amps() -> None:
    """The unit must follow the basis, or an operator reads 0.30 as amps."""
    panels = [panel(i, "0.480") for i in range(5)] + [panel(5, "0.300")]
    anomaly = analyse_site_panels(panels, threshold_pct=THRESHOLD)[0]

    assert anomaly.unit == "kW"
    assert "kW" in anomaly.describe()
    assert "Panel 5" in anomaly.describe()


def test_night_time_panels_are_ignored() -> None:
    panels = [panel(i, "0.005") for i in range(6)]
    assert analyse_site_panels(panels, threshold_pct=THRESHOLD) == []


def test_current_based_analysis_finds_nothing_in_microinverter_data() -> None:
    """The guard that matters: running the STRING rule over microinverter
    readings must return nothing rather than crash or invent a result, because
    those readings carry no current at all."""
    panels = [panel(i, "0.480") for i in range(5)] + [panel(5, "0.100")]
    assert analyse_mppt_group(panels, threshold_pct=THRESHOLD) == []
    assert len(analyse_site_panels(panels, threshold_pct=THRESHOLD)) == 1


def test_string_anomaly_reports_amps() -> None:
    group = [reading(0, i, "8.50") for i in range(3)] + [reading(0, 3, "6.00")]
    anomaly = analyse_mppt_group(group, threshold_pct=THRESHOLD)[0]

    assert anomaly.basis is MeasurementBasis.STRING
    assert anomaly.unit == "A"
    assert "MPPT 0 string 3" in anomaly.describe()
