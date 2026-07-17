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
from opendisplay.protocol.config_parser import _parse_power_option
from opendisplay.protocol.config_serializer import serialize_power_option


def _base_config(**overrides) -> GlobalConfig:
    power = overrides.pop("power", None) or PowerOption(
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
        charge_enable_pin=0xFF,
        charge_state_pin=0xFF,
        charger_flags=0,
        min_wake_time_seconds=0,
        screen_timeout_seconds=0,
        reserved=b"\x00" * 4,
    )
    return GlobalConfig(
        system=SystemConfig(ic_type=0, communication_modes=0, device_flags=0, pwr_pin=0xFF, reserved=b"\x00" * 15),
        manufacturer=ManufacturerData(manufacturer_id=0, board_type=0, board_revision=0, reserved=b"\x00" * 6),
        power=power,
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


def _sentinel_power() -> PowerOption:
    """PowerOption with distinct non-zero values in the promoted offsets 20-25 and tail 26-29."""
    return PowerOption(
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
        charge_enable_pin=0x11,  # offset 20
        charge_state_pin=0x22,  # offset 21
        charger_flags=0x33,  # offset 22
        min_wake_time_seconds=0x4455,  # offsets 23-24 (uint16 LE)
        screen_timeout_seconds=0x66,  # offset 25
        reserved=bytes([0x77, 0x88, 0x99, 0xAA]),  # offsets 26-29
    )


def test_power_option_named_fields_survive_json_roundtrip() -> None:
    """The five promoted fields (offsets 20-25) and the 4-byte reserved tail (26-29)
    must survive binary -> JSON -> binary by name instead of being zeroed."""
    cfg = _base_config(displays=[_display()], power=_sentinel_power())

    back = config_from_json(config_to_json(cfg))
    p = back.power
    assert p.charge_enable_pin == 0x11
    assert p.charge_state_pin == 0x22
    assert p.charger_flags == 0x33
    assert p.min_wake_time_seconds == 0x4455
    assert p.screen_timeout_seconds == 0x66
    assert p.reserved == bytes([0x77, 0x88, 0x99, 0xAA])

    packet = serialize_power_option(p)
    assert len(packet) == 30
    # offsets 20-29 preserved byte-for-byte, not zeroed
    assert packet[20:30] == bytes([0x11, 0x22, 0x33, 0x55, 0x44, 0x66, 0x77, 0x88, 0x99, 0xAA])


def test_power_option_parse_serialize_symmetry_new_offsets() -> None:
    """serialize -> parse round-trips the promoted offsets 20-25 and tail 26-29."""
    original = _sentinel_power()
    packet = serialize_power_option(original)
    assert len(packet) == 30
    parsed = _parse_power_option(packet)
    assert parsed.charge_enable_pin == 0x11
    assert parsed.charge_state_pin == 0x22
    assert parsed.charger_flags == 0x33
    assert parsed.min_wake_time_seconds == 0x4455
    assert parsed.screen_timeout_seconds == 0x66
    assert parsed.reserved == bytes([0x77, 0x88, 0x99, 0xAA])
    assert serialize_power_option(parsed) == packet


def test_legacy_zero_reserved_still_parses() -> None:
    """A legacy JSON with reserved="0x0" must still import as zero bytes."""
    cfg = _base_config(displays=[_display()])
    data = config_to_json(cfg)
    for packet in data["packets"]:
        if packet["name"] == "display":
            packet["fields"]["reserved"] = "0x0"
    back = config_from_json(data)
    assert back.displays[0].reserved == bytes(13)
