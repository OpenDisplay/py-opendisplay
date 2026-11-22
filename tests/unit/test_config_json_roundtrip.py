"""Round-trip tests for config JSON export/import (M7).

Real hardware pins and reserved-blob fields (ADC thresholds, power_off flags)
must survive a binary -> JSON -> binary round-trip instead of being zeroed.
"""

from __future__ import annotations

import json

import pytest

from opendisplay.models.config import (
    BinaryInputs,
    DisplayConfig,
    FlashConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
    TouchController,
)
from opendisplay.models.config_json import config_from_json, config_to_json
from opendisplay.protocol.config_parser import _parse_power_option
from opendisplay.protocol.config_serializer import serialize_config, serialize_power_option


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
        power_off_flags=0x05,
        power_off_hold_sec=9,
        reserved=bytes(range(12)),
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
    assert b.power_off_flags == 0x05
    assert b.power_off_hold_sec == 9
    assert b.reserved == bytes(range(12))  # ADC ladder thresholds preserved


def _touch() -> TouchController:
    return TouchController(
        instance_number=0,
        touch_ic_type=1,
        bus_id=0,
        i2c_addr_7bit=0x5D,
        int_pin=7,
        rst_pin=8,
        display_instance=0,
        flags=0,
        poll_interval_ms=25,
        touch_data_start_byte=0,
        reserved=bytes([0x2A]) + bytes(20),
    )


def _flash() -> FlashConfig:
    return FlashConfig(
        instance_number=0,
        flash_ic_type=0,
        bus_instance=0,
        flags=1,
        mosi_pin=1,
        sck_pin=2,
        cs_pin=3,
        power_pin=0xFF,
        power_active=0,
        power_on_delay_ms=0,
        power_off_delay_ms=0,
        mode=0,
        reserved=bytes([0x0B, 0x0C, 0x0D]) + bytes(17),
    )


def _packet(exported: dict, packet_id: str) -> dict:
    return next(p["fields"] for p in exported["packets"] if p["id"] == packet_id)


def test_export_uses_the_toolbox_field_names() -> None:
    cfg = _base_config(
        displays=[_display()],
        binary_inputs=[_binary_input()],
        touch_controllers=[_touch()],
        flash_configs=[_flash()],
    )
    exported = config_to_json(cfg)
    display = _packet(exported, "32")
    assert display["cs_pin_2"] == "0xb"
    assert "reserved_pin_2" not in display
    assert display["reserved_pin_8"] == "0x11"
    inputs = _packet(exported, "37")
    assert [inputs[f"inputpin{i}"] for i in range(1, 9)] == [f"0x{p:x}" for p in range(21, 29)]
    assert inputs["isused"] == "0x3"
    assert "input_flags" not in inputs and "reserved_pin_1" not in inputs
    assert _packet(exported, "40")["enable_pin"] == "0x2a"
    flash = _packet(exported, "43")
    assert (flash["miso_pin"], flash["wp_pin"], flash["hold_pin"]) == ("0x0b", "0x0c", "0x0d")


def test_touch_enable_pin_and_flash_spi_pins_survive_json_roundtrip() -> None:
    cfg = _base_config(displays=[_display()], touch_controllers=[_touch()], flash_configs=[_flash()])
    back = config_from_json(config_to_json(cfg))
    assert back.touch_controllers[0].reserved[0] == 0x2A
    assert back.flash_configs[0].reserved[:3] == bytes([0x0B, 0x0C, 0x0D])
    assert serialize_config(back) == serialize_config(cfg)


def test_toolbox_field_names_import() -> None:
    """A file written by the OpenDisplay Toolbox (its config.yaml names)."""
    exported = config_to_json(_base_config())
    exported["packets"] += [
        {"id": "32", "fields": {"cs_pin": "0x2c", "cs_pin_2": "41", "reserved_pin_3": "0xff"}},
        {
            "id": "37",
            "fields": {
                **{f"inputpin{i}": "0xff" for i in range(1, 9)},
                "inputpin1": "0x2",
                "inputpin2": "0x3",
                "inputpin3": "0x5",
                "isused": "0x7",
            },
        },
        {"id": "40", "fields": {"enable_pin": "0x9"}},
        {"id": "43", "fields": {"miso_pin": "0x4", "wp_pin": "0x5", "hold_pin": "0x6"}},
    ]
    cfg = config_from_json(json.loads(json.dumps(exported)))
    assert cfg.displays[0].reserved_pins[:2] == bytes([41, 0xFF])
    b = cfg.binary_inputs[0]
    assert b.reserved_pins == bytes([2, 3, 5, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
    assert b.input_flags == 7
    assert cfg.touch_controllers[0].reserved[0] == 9
    assert cfg.flash_configs[0].reserved[:3] == bytes([4, 5, 6])


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


# ─── Packets the library does not recognise (GlobalConfig.unparsed_tail) ─────

# One length-prefixed packet as newer firmware writes it: number 5, type 0xB1, 3-byte body.
_UNKNOWN_PACKETS = bytes([5, 0xB1, 3, 0, 0xAA, 0xBB, 0xCC])


def test_unparsed_tail_survives_json_roundtrip() -> None:
    """Packets after the first unknown one must not be dropped by export -> import.

    On firmware that stores its panel drive data in such packets, losing them on
    a write-config would leave the device unable to drive its panel.
    """
    cfg = _base_config(displays=[_display()])
    cfg.unparsed_tail = _UNKNOWN_PACKETS

    exported = config_to_json(cfg)
    back = config_from_json(json.loads(json.dumps(exported)))

    assert back.unparsed_tail == _UNKNOWN_PACKETS
    assert serialize_config(back).count(_UNKNOWN_PACKETS) == 1


def test_json_without_unparsed_tail_imports_an_empty_one() -> None:
    cfg = _base_config(displays=[_display()])

    exported = config_to_json(cfg)

    assert "unparsed_tail" not in exported  # nothing to carry: the Config Builder format is unchanged
    assert config_from_json(exported).unparsed_tail == b""


def test_malformed_unparsed_tail_is_rejected() -> None:
    exported = config_to_json(_base_config(displays=[_display()]))
    exported["unparsed_tail"] = "0xnot-hex"

    with pytest.raises(ValueError, match="unparsed_tail"):
        config_from_json(exported)
