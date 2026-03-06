"""Tests for battery state-of-charge estimation."""

import pytest

from opendisplay.battery import voltage_to_percent
from opendisplay.models.enums import CapacityEstimator


@pytest.mark.parametrize("chemistry,voltage_mv,expected", [
    # LI_ION — exact breakpoints
    (CapacityEstimator.LI_ION, 4200, 100),
    (CapacityEstimator.LI_ION, 3790, 50),
    (CapacityEstimator.LI_ION, 3550, 20),
    (CapacityEstimator.LI_ION, 3000, 0),
    # LI_ION — clamping
    (CapacityEstimator.LI_ION, 4500, 100),
    (CapacityEstimator.LI_ION, 2000, 0),
    # LIFEPO4 — exact breakpoints
    (CapacityEstimator.LIFEPO4, 3650, 100),
    (CapacityEstimator.LIFEPO4, 3270, 50),
    (CapacityEstimator.LIFEPO4, 2500, 0),
    # LIFEPO4 — clamping
    (CapacityEstimator.LIFEPO4, 4000, 100),
    (CapacityEstimator.LIFEPO4, 2000, 0),
    # LITHIUM_PRIMARY — endpoints and midpoint (linear placeholder)
    (CapacityEstimator.LITHIUM_PRIMARY, 3600, 100),
    (CapacityEstimator.LITHIUM_PRIMARY, 3200, 50),
    (CapacityEstimator.LITHIUM_PRIMARY, 2800, 0),
    # LITHIUM_PRIMARY — clamping
    (CapacityEstimator.LITHIUM_PRIMARY, 3700, 100),
    (CapacityEstimator.LITHIUM_PRIMARY, 2000, 0),
    # SUPERCAP — endpoints and midpoint (linear placeholder)
    (CapacityEstimator.SUPERCAP, 5000, 100),
    (CapacityEstimator.SUPERCAP, 3500, 50),
    (CapacityEstimator.SUPERCAP, 2000, 0),
    # SUPERCAP — clamping
    (CapacityEstimator.SUPERCAP, 6000, 100),
    (CapacityEstimator.SUPERCAP, 1000, 0),
])
def test_exact_values(chemistry: CapacityEstimator, voltage_mv: int, expected: int) -> None:
    """Exact breakpoints and boundary clamping return expected SOC."""
    assert voltage_to_percent(voltage_mv, chemistry) == expected


@pytest.mark.parametrize("chemistry,voltage_mv,low,high", [
    # LI_ION — midpoint between 3790 mV (50%) and 3750 mV (45%)
    (CapacityEstimator.LI_ION, 3770, 46, 48),
    # LIFEPO4 — midpoint between 3400 mV (90%) and 3350 mV (80%)
    (CapacityEstimator.LIFEPO4, 3375, 84, 86),
])
def test_interpolation(
    chemistry: CapacityEstimator, voltage_mv: int, low: int, high: int
) -> None:
    """Voltages between breakpoints are linearly interpolated."""
    result = voltage_to_percent(voltage_mv, chemistry)
    assert result is not None
    assert low <= result <= high


def test_lifepo4_flat_plateau() -> None:
    """3320 mV (70%) and 3290 mV (60%) are only 30 mV apart — documents the
    characteristic flat plateau where voltage-based gauging is unreliable."""
    assert voltage_to_percent(3320, CapacityEstimator.LIFEPO4) == 70
    assert voltage_to_percent(3290, CapacityEstimator.LIFEPO4) == 60


@pytest.mark.parametrize("chemistry,voltage_mv", [
    # raw int — CapacityEstimator.LI_ION == 1
    (1, 4200),
])
def test_accepts_raw_int_chemistry(chemistry: int, voltage_mv: int) -> None:
    """Raw integer chemistry values are accepted when they map to a known enum."""
    assert voltage_to_percent(voltage_mv, chemistry) == 100


@pytest.mark.parametrize("chemistry", [99, 0, 255])
def test_unknown_chemistry_returns_none(chemistry: int) -> None:
    """Unknown chemistry values return None."""
    assert voltage_to_percent(3800, chemistry) is None
