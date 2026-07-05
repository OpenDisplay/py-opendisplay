"""Round-trip tests for config JSON export/import (M7).

Real hardware pins and reserved-blob fields (ADC thresholds, power_off flags)
must survive a binary -> JSON -> binary round-trip instead of being zeroed.
"""

from __future__ import annotations

from opendisplay.models.config import (
    BinaryInputs,
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.models.config_json import config_from_json, config_to_json


def _base_config(**overrides) -> GlobalConfig:
    return GlobalConfig(
        system=SystemConfig(ic_type=0, communication_modes=0, device_flags=0, pwr_pin=0xFF, reserved=b"\x00" * 15),
        manufacturer=ManufacturerData(manufacturer_id=0, board_type=0, board_revision=0, reserved=b"\x00" * 6),
        power=PowerOption(
            power_mode=0,
            battery_capacity_mah=b"\x00\x00\x00",
            sleep_timeout_ms=0,
            tx_power=0,
            sleep_flags=0,
            battery_sense_pin=0xFF,
            battery_sense_enable_pin=0xFF,
            battery_sense_flags=0,
            capacity_estimator=0,
            voltage_scaling_factor=0,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            reserved=b"\x00" * 10,
        ),
        **overrides,
    )


def _display() -> DisplayConfig:
    return DisplayConfig(
        instance_number=0,
        display_technology=0,
        panel_ic_type=1,
        pixel_width=100,
        pixel_height=100,
        active_width_mm=10,
        active_height_mm=10,
        tag_type=0,
        rotation=0,
        reset_pin=1,
        busy_pin=2,
        dc_pin=3,
        cs_pin=4,
        data_pin=5,
        partial_update_support=0,
        color_scheme=0,
        transmission_modes=0,
        clk_pin=6,
        reserved_pins=bytes([11, 12, 13, 14, 15, 16, 17]),
        full_update_mC=1234,
        reserved=bytes(range(13)),
    )


def _binary_input() -> BinaryInputs:
    return BinaryInputs(
        instance_number=0,
        input_type=1,
        display_as=2,
        reserved_pins=bytes([21, 22, 23, 24, 25, 26, 27, 28]),
        input_flags=3,
        invert=0,
        pullups=1,
        pulldowns=0,
        button_data_byte_index=4,
        reserved=bytes(range(14)),
    )


def test_display_pins_full_update_and_reserved_survive_json_roundtrip() -> None:
    cfg = _base_config(displays=[_display()])
    back = config_from_json(config_to_json(cfg))
    d = back.displays[0]
    assert d.reserved_pins == bytes([11, 12, 13, 14, 15, 16, 17])
    assert d.full_update_mC == 1234
    assert d.reserved == bytes(range(13))


def test_binary_input_pins_and_reserved_survive_json_roundtrip() -> None:
    cfg = _base_config(displays=[_display()], binary_inputs=[_binary_input()])
    back = config_from_json(config_to_json(cfg))
    b = back.binary_inputs[0]
    assert b.reserved_pins == bytes([21, 22, 23, 24, 25, 26, 27, 28])
    assert b.reserved == bytes(range(14))  # ADC thresholds / power_off blob preserved


def test_legacy_zero_reserved_still_parses() -> None:
    """A legacy JSON with reserved="0x0" must still import as zero bytes."""
    cfg = _base_config(displays=[_display()])
    data = config_to_json(cfg)
    for packet in data["packets"]:
        if packet["name"] == "display":
            packet["fields"]["reserved"] = "0x0"
    back = config_from_json(data)
    assert back.displays[0].reserved == bytes(13)
