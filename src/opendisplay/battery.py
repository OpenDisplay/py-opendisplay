"""Battery state-of-charge estimation from cell voltage.

All curves assume single-cell voltages measured at rest (no load).
Multi-cell packs are not supported — the voltage_scaling_factor in
PowerOption is implementation-specific and cannot be safely used as a
cell-count multiplier.
"""

from __future__ import annotations

from .models.enums import CapacityEstimator

# ---------------------------------------------------------------------------
# Lookup tables: list of (voltage_mv, soc_percent) sorted high → low.
# Linear interpolation is used between breakpoints.
# For runtime tests, please run a full discharge cycle
# ---------------------------------------------------------------------------

# Li-Ion / LiPo single cell.
_SOC_LI_ION: list[tuple[int, int]] = [
    (4200, 100),
    (4150, 95),
    (4110, 90),
    (4080, 85),
    (4020, 80),
    (3980, 75),
    (3950, 70),
    (3910, 65),
    (3870, 60),
    (3830, 55),
    (3790, 50),
    (3750, 45),
    (3710, 40),
    (3670, 35),
    (3630, 30),
    (3590, 25),
    (3550, 20),
    (3490, 15),
    (3430, 10),
    (3350, 5),
    (3000, 0),
]

# LiFePO4 single cell (3.2 V nominal).
_SOC_LIFEPO4: list[tuple[int, int]] = [
    (3650, 100),
    (3400, 90),
    (3350, 80),
    (3320, 70),
    (3290, 60),
    (3270, 50),
    (3250, 40),
    (3220, 30),
    (3200, 20),
    (3000, 10),
    (2500, 0),
]

# Lithium primary (3V type, e.g. CR2450).
_SOC_LITHIUM_PRIMARY: list[tuple[int, int]] = [
    (3000, 100),
    (2600, 0),
]

# Supercapacitor.
# to be used with 2s capacitor packs with pmic
_SOC_SUPERCAP: list[tuple[int, int]] = [
    (4500, 100),
    (3000, 0),
]

# Seeed reTerminal E-series (E1001/E1002/E1003/E1004) single-cell LiPo, derived
# from Seeed's own ESPHome reference config's calibrate_linear breakpoints (10%
# steps). The odd 5%-step entries are the midpoints of the surrounding pair,
# added for finer resolution.
_SOC_SEEED_LI_ION: list[tuple[int, int]] = [
    (4150, 100),
    (4055, 95),
    (3960, 90),
    (3935, 85),
    (3910, 80),
    (3880, 75),
    (3850, 70),
    (3825, 65),
    (3800, 60),
    (3775, 55),
    (3750, 50),
    (3715, 45),
    (3680, 40),
    (3630, 35),
    (3580, 30),
    (3535, 25),
    (3490, 20),
    (3450, 15),
    (3410, 10),
    (3300, 5),
    (3270, 0),
]


def _interpolate(table: list[tuple[int, int]], voltage_mv: int) -> int:
    """Linearly interpolate SOC from a voltage lookup table.

    The table must be sorted in descending voltage order.
    Returns a clamped value in the range [0, 100].
    """
    if voltage_mv >= table[0][0]:
        return table[0][1]
    if voltage_mv <= table[-1][0]:
        return table[-1][1]

    for i in range(len(table) - 1):
        v_high, soc_high = table[i]
        v_low, soc_low = table[i + 1]
        if v_low <= voltage_mv <= v_high:
            ratio = (voltage_mv - v_low) / (v_high - v_low)
            return round(soc_low + ratio * (soc_high - soc_low))

    return 0  # unreachable


def voltage_to_percent(
    voltage_mv: int,
    chemistry: CapacityEstimator | int,
) -> int | None:
    """Estimate battery state of charge from cell voltage.

    Args:
        voltage_mv: Battery voltage in millivolts, measured at rest.
        chemistry: Battery chemistry, as a CapacityEstimator enum or raw int.

    Returns:
        Estimated SOC as an integer in [0, 100], or None if the chemistry
        is unknown.
    """
    try:
        chemistry = CapacityEstimator(chemistry)
    except ValueError:
        return None

    match chemistry:
        case CapacityEstimator.LI_ION:
            return _interpolate(_SOC_LI_ION, voltage_mv)
        case CapacityEstimator.LIFEPO4:
            return _interpolate(_SOC_LIFEPO4, voltage_mv)
        case CapacityEstimator.LITHIUM_PRIMARY:
            return _interpolate(_SOC_LITHIUM_PRIMARY, voltage_mv)
        case CapacityEstimator.SUPERCAP:
            return _interpolate(_SOC_SUPERCAP, voltage_mv)
        case CapacityEstimator.SEEED_LI_ION:
            return _interpolate(_SOC_SEEED_LI_ION, voltage_mv)
        case _:
            return None
