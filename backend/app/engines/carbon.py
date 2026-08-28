"""Carbon avoidance (Thailand TGO).

Specification section 5: compute carbon avoidance using the TGO grid emission
factor, 0.4999 kgCO2e/kWh.

Every function takes the factor as an argument rather than reading a constant.
TGO revises the factor periodically, and a report re-run for a previous year
must reproduce that year's numbers — which is impossible if the factor is baked
into the calculation. ``Settings.tgo_grid_emission_factor`` carries the current
value and ``tgo_ef_effective_year`` records which year it belongs to.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "KG_PER_TONNE",
    "co2_avoided_kg",
    "co2_avoided_tonnes",
]

KG_PER_TONNE = Decimal("1000")


def co2_avoided_kg(
    *,
    energy_kwh: Decimal | None,
    emission_factor: Decimal,
) -> Decimal | None:
    """Grid CO2e avoided by self-consuming ``energy_kwh`` of solar generation.

    Returns None for unknown input rather than zero: "we did not measure this"
    and "this avoided nothing" are different claims, and an ESG figure that
    quietly reports the second when it means the first is a reporting defect.
    """
    if energy_kwh is None:
        return None
    if energy_kwh < 0:
        raise ValueError("energy_kwh must not be negative")
    return (energy_kwh * emission_factor).quantize(Decimal("0.001"))


def co2_avoided_tonnes(
    *,
    energy_kwh: Decimal | None,
    emission_factor: Decimal,
) -> Decimal | None:
    """Same figure in tonnes, the unit used in annual ESG disclosures."""
    kg = co2_avoided_kg(energy_kwh=energy_kwh, emission_factor=emission_factor)
    if kg is None:
        return None
    return (kg / KG_PER_TONNE).quantize(Decimal("0.001"))
