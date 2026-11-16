"""Tests for the `opendisplay info` report.

The renderer is driven by a declarative section spec (``_INFO_SECTIONS``) so
that packets added to ``GlobalConfig`` surface without a hand-written branch per
field. These tests pin two things: that every populated packet reaches both the
tree and the ``--json`` output, and that packets absent from a device's config
stay absent from the report.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from typing import Any

import pytest
from rich.console import Console

from opendisplay.cli_info import _build_info_tree, _info_to_json, _InfoContext
from opendisplay.models.config import (
    DataBus,
    DataExtended,
    DisplayConfig,
    FlashConfig,
    GlobalConfig,
    LedConfig,
    ManufacturerData,
    NfcConfig,
    PassiveBuzzer,
    PowerOption,
    SensorData,
    SystemConfig,
    TouchController,
    WifiConfig,
)
from opendisplay.models.enums import LedType, SensorType
from opendisplay.protocol.responses import parse_read_ident
from opendisplay.sensors import SensorReading

_FW = {"major": 2, "minor": 26, "patch": 0, "sha": "987c6d9"}


def _display(**overrides: Any) -> DisplayConfig:
    base: dict[str, Any] = dict(
        instance_number=0,
        display_technology=1,
        panel_ic_type=0x0BB8,
        pixel_width=1872,
        pixel_height=1404,
        active_width_mm=209,
        active_height_mm=157,
        tag_type=0,
        rotation=0,
        reset_pin=0x0C,
        busy_pin=0x0D,
        dc_pin=0x08,
        cs_pin=0x0A,
        data_pin=0x09,
        partial_update_support=1,
        color_scheme=0,
        transmission_modes=0x19,  # STREAMING | DIRECT_WRITE | PIPE_WRITE
        clk_pin=0x07,
        reserved_pins=b"\xff" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )
    base.update(overrides)
    return DisplayConfig(**base)


def _config(**overrides: Any) -> GlobalConfig:
    base: dict[str, Any] = dict(
        system=SystemConfig(
            ic_type=2,
            communication_modes=0x05,
            device_flags=0,
            pwr_pin=0xFF,
            reserved=b"\x00" * 16,
            pwr_pin_2=0,
            pwr_pin_3=0,
        ),
        manufacturer=ManufacturerData(
            manufacturer_id=1,
            board_type=1,
            board_revision=1,
            reserved=b"\x00" * 14,
            simple_config_driver_index=0,
            simple_config_display_index=0,
            simple_config_power_index=0,
            simple_config_configured_at=0,
        ),
        power=PowerOption(
            power_mode=1,
            battery_capacity_mah=(3000).to_bytes(3, "little"),
            sleep_timeout_ms=40000,
            tx_power=8,
            sleep_flags=0,
            battery_sense_pin=1,
            battery_sense_enable_pin=0x28,
            battery_sense_flags=0,
            capacity_estimator=5,
            voltage_scaling_factor=0xA1,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            charge_enable_pin=0,
            charge_state_pin=0,
            charger_flags=0,
            min_wake_time_seconds=0,
            screen_timeout_seconds=0,
            reserved=b"\x00" * 4,
        ),
        displays=[_display()],
    )
    base.update(overrides)
    return GlobalConfig(**base)


def _ctx(config: GlobalConfig | None) -> _InfoContext:
    return _InfoContext(
        mac="AA:BB:CC:DD:EE:FF",
        device_name="OD405BD8",
        fw=_FW,
        config=config,
        width=1872,
        height=1404,
        color_scheme_name="MONO",
        rotation=0,
        board_type_name="reTerminal E1003",
    )


def _text(config: GlobalConfig | None) -> str:
    console = Console(width=120, record=True, force_terminal=False)
    console.print(_build_info_tree(_ctx(config)))
    return console.export_text()


# ── transmission modes ───────────────────────────────────────────────────────


def test_pipe_write_is_reported_when_advertised() -> None:
    assert "PIPE_WRITE" in _text(_config())


def test_pipe_write_is_absent_when_not_advertised() -> None:
    cfg = _config(displays=[_display(transmission_modes=0x09)])
    assert "PIPE_WRITE" not in _text(cfg)


def test_transmission_modes_in_json_include_pipe_write() -> None:
    assert "PIPE_WRITE" in _info_to_json(_ctx(_config()))["display"]["transmission_modes"]


# ── display capability fields ────────────────────────────────────────────────


def test_partial_update_support_is_reported() -> None:
    assert "Partial" in _text(_config())


def test_partial_update_full_frame_is_distinguished_from_line_rect() -> None:
    line_rect = _text(_config(displays=[_display(partial_update_support=1)]))
    full_frame = _text(_config(displays=[_display(partial_update_support=2)]))
    assert line_rect != full_frame


# ── packets that were previously never rendered ──────────────────────────────


def test_touch_controller_is_reported() -> None:
    tc = TouchController(
        instance_number=0,
        touch_ic_type=1,
        bus_id=0,
        i2c_addr_7bit=0x14,
        int_pin=0x02,
        rst_pin=0x30,
        display_instance=0,
        flags=0,
        poll_interval_ms=100,
        touch_data_start_byte=1,
        reserved=b"\x00" * 21,
    )
    out = _text(_config(touch_controllers=[tc]))
    assert "Touch" in out
    assert "0x14" in out


def test_passive_buzzer_is_reported() -> None:
    bz = PassiveBuzzer(
        instance_number=0,
        drive_pin=0x2D,
        enable_pin=0xFF,
        flags=0x01,
        duty_percent=50,
        reserved=b"\x00" * 27,
    )
    assert "Buzzer" in _text(_config(buzzers=[bz]))


def _c_str(value: str) -> bytes:
    """Encode as the firmware does: 32-byte null-terminated, zero-padded."""
    return value.encode().ljust(32, b"\x00")


def test_data_extended_identity_strings_are_reported() -> None:
    ext = DataExtended(
        manufacturer_name=_c_str("Seeed Studio"),
        model_name=_c_str("reTerminal E1003"),
        serial_number=_c_str("SN-12345"),
        friendly_name=_c_str("Kitchen display"),
        device_location=_c_str("Kitchen"),
        device_id=_c_str("dev-1"),
    )
    out = _text(_config(data_extended=ext))
    assert "SN-12345" in out
    assert "Kitchen display" in out


def test_identity_strings_are_decoded_not_raw_bytes() -> None:
    """The packet holds 32-byte buffers; the report must show text, not repr()."""
    ext = DataExtended(model_name=_c_str("reTerminal E1003"))
    out = _text(_config(data_extended=ext))
    assert "\\x00" not in out
    assert "b'" not in out


def test_empty_identity_fields_are_omitted() -> None:
    """An all-zero buffer means 'unset' and must not produce a line."""
    ext = DataExtended(model_name=_c_str("reTerminal E1003"))
    out = _text(_config(data_extended=ext))
    assert "Serial" not in out
    assert "Location" not in out


# ── human-readable values ────────────────────────────────────────────────────


def test_panel_ic_type_is_named_not_just_hex() -> None:
    """0x0bb8 (3000) is the ED103TC2; a bare hex id tells a reader nothing."""
    out = _text(_config())
    assert "ED103TC2" in out.upper()


def test_unknown_panel_ic_type_falls_back_to_hex() -> None:
    cfg = _config(displays=[_display(panel_ic_type=0xABCD)])
    assert "0xabcd" in _text(cfg)


def test_display_technology_is_named() -> None:
    assert "E_PAPER" in _text(_config()).upper()


def test_button_input_type_is_named_not_hex() -> None:
    from opendisplay.models.config import BinaryInputs

    button = BinaryInputs(
        instance_number=0,
        input_type=1,
        display_as=0,
        reserved_pins=b"\xff" * 3,
        input_flags=0,
        invert=0,
        pullups=0,
        pulldowns=0,
        button_data_byte_index=0,
        power_off_flags=0,
        power_off_hold_sec=0,
        reserved=b"\x00" * 12,
    )
    out = _text(_config(binary_inputs=[button]))
    assert "DIGITAL" in out.upper()
    assert "type 0x01" not in out


def test_full_frame_partial_support_is_spelled_out() -> None:
    cfg = _config(displays=[_display(partial_update_support=2)])
    assert "full-frame" in _text(cfg).lower()


# ── display resolution ───────────────────────────────────────────────────────


def test_resolution_reports_both_dimensions() -> None:
    assert "1872x1404px" in _text(_config())


def test_data_bus_is_reported() -> None:
    bus = DataBus(
        instance_number=0,
        bus_type=1,
        pin_1=0x14,
        pin_2=0x13,
        pin_3=0,
        pin_4=0,
        pin_5=0,
        pin_6=0,
        pin_7=0,
        bus_speed_hz=400000,
        bus_flags=0,
        pullups=3,
        pulldowns=0,
        reserved=b"\x00" * 8,
    )
    assert "Bus" in _text(_config(data_buses=[bus]))


def test_nfc_config_is_reported() -> None:
    nfc = NfcConfig(
        instance_number=0,
        nfc_ic_type=1,
        bus_instance=0,
        flags=0,
        field_detect_pin=0xFF,
        field_detect_mode=0,
        field_detect_active=0,
        field_detect_debounce_ms=0,
        power_pin=0xFF,
        power_active=0,
        power_on_delay_ms=0,
        power_off_delay_ms=0,
        adv_button_byte_index=0,
        adv_button_button_id=0,
        reserved_pin_1=0xFF,
        reserved_pin_2=0xFF,
        reserved=b"\x00" * 14,
    )
    assert "NFC" in _text(_config(nfc_configs=[nfc]))


def test_flash_config_is_reported() -> None:
    flash = FlashConfig(
        instance_number=0,
        flash_ic_type=1,
        bus_instance=0,
        flags=0,
        mosi_pin=0x10,
        sck_pin=0x11,
        cs_pin=0x12,
        power_pin=0xFF,
        power_active=0,
        power_on_delay_ms=0,
        power_off_delay_ms=0,
        mode=0,
        reserved=b"\x00" * 17,
    )
    assert "Flash" in _text(_config(flash_configs=[flash]))


# ── absent packets stay absent ───────────────────────────────────────────────


@pytest.mark.parametrize("label", ["Touch", "Buzzer", "NFC", "Flash"])
def test_absent_packets_are_not_rendered(label: str) -> None:
    assert label not in _text(_config())


def test_absent_packets_are_not_in_json() -> None:
    rendered = _info_to_json(_ctx(_config()))
    for key in ("touch_controllers", "buzzers", "nfc", "flash"):
        assert rendered.get(key) in (None, [])


# ── JSON back-compatibility ──────────────────────────────────────────────────


def test_existing_json_keys_are_preserved() -> None:
    rendered = _info_to_json(_ctx(_config()))
    assert set(rendered) >= {"mac", "display", "hardware", "power", "firmware"}
    assert set(rendered["display"]) >= {
        "width",
        "height",
        "active_width_mm",
        "active_height_mm",
        "diagonal_inches",
        "color_scheme",
        "rotation",
        "panel_ic_type",
        "full_update_mc",
        "transmission_modes",
    }
    assert set(rendered["hardware"]) >= {
        "ic",
        "manufacturer",
        "board_type",
        "board_revision",
        "leds",
        "sensors",
        "buttons",
    }
    assert set(rendered["power"]) >= {"mode", "battery_mah", "chemistry", "sleep_timeout_s", "tx_power_dbm"}
    assert rendered["firmware"] == {"major": 2, "minor": 26, "patch": 0, "sha": "987c6d9"}


def test_sleep_timeout_json_is_seconds_not_milliseconds() -> None:
    """The key is `_s`; the packet stores ms. Scripts parsing this must not break."""
    assert _info_to_json(_ctx(_config()))["power"]["sleep_timeout_s"] == 40.0


def test_zero_valued_power_fields_are_null_in_json() -> None:
    """Preserves the pre-existing `or None` behaviour these keys shipped with."""
    power = _info_to_json(_ctx(_config()))["power"]
    assert power["deep_sleep_time_s"] is None
    assert power["deep_sleep_current_ua"] is None


def test_deep_sleep_json_is_numeric_seconds() -> None:
    cfg = _config()
    cfg.power.deep_sleep_time_seconds = 30
    cfg.power.deep_sleep_current_ua = 12
    assert _info_to_json(_ctx(cfg))["power"]["deep_sleep_time_s"] == 30


# ── LEDs and sensors ─────────────────────────────────────────────────────────


def _led(led_type: int) -> LedConfig:
    return LedConfig(
        instance_number=0,
        led_type=led_type,
        led_1_r=0,
        led_2_g=0,
        led_3_b=0,
        led_4=0,
        led_flags=0,
        reserved=b"\x00" * 14,
    )


def test_led_type_is_named() -> None:
    assert "RGB" in _text(_config(leds=[_led(LedType.RGB)]))


def test_unknown_led_type_falls_back_to_hex() -> None:
    assert "0x7f" in _text(_config(leds=[_led(0x7F)]))


def test_sensor_is_reported_with_bus() -> None:
    sensor = SensorData(
        instance_number=0,
        sensor_type=1,
        bus_id=2,
        i2c_addr_7bit=0x44,
        msd_data_start_byte=0,
        reserved=b"\x00" * 24,
    )
    out = _text(_config(sensors=[sensor]))
    assert "Sensor 0" in out
    assert "bus 2" in out


# ── WiFi section ─────────────────────────────────────────────────────────────


def _wifi(**overrides: Any) -> WifiConfig:
    base: dict[str, Any] = dict(
        ssid="Antarctica-IOT",
        password="secret",
        encryption_type=3,
        server_url="",
        server_port=2446,
    )
    base.update(overrides)
    return WifiConfig.from_strings(**base)


def test_wifi_section_reports_ssid_and_encryption() -> None:
    out = _text(_config(wifi_config=_wifi()))
    assert "Antarctica-IOT" in out
    assert "WPA2" in out


def test_wifi_section_is_absent_without_an_ssid() -> None:
    assert "SSID" not in _text(_config(wifi_config=_wifi(ssid="")))


def test_wifi_server_is_shown_when_configured() -> None:
    out = _text(_config(wifi_config=_wifi(server_url="hub.local", server_port=2447)))
    assert "hub.local:2447" in out


def test_wifi_server_is_omitted_when_unset() -> None:
    assert "Server" not in _text(_config(wifi_config=_wifi()))


def test_unknown_wifi_encryption_falls_back_to_hex() -> None:
    assert "0x7f" in _text(_config(wifi_config=_wifi(encryption_type=0x7F)))


# ── deep sleep ───────────────────────────────────────────────────────────────


def test_deep_sleep_line_includes_current_draw() -> None:
    cfg = _config()
    cfg.power.deep_sleep_time_seconds = 30
    cfg.power.deep_sleep_current_ua = 12
    assert "30s @ 12 µA" in _text(cfg)


def test_deep_sleep_line_omits_current_when_unknown() -> None:
    cfg = _config()
    cfg.power.deep_sleep_time_seconds = 30
    assert "30s" in _text(cfg)
    assert "µA" not in _text(cfg)


# ── unknown enum fallbacks ───────────────────────────────────────────────────


def test_unknown_partial_update_value_falls_back_to_hex() -> None:
    cfg = _config(displays=[_display(partial_update_support=0x7F)])
    assert "0x7f" in _text(cfg)


def test_report_survives_missing_config() -> None:
    """A device that never returned a config still renders MAC and firmware."""
    out = _text(None)
    assert "OD405BD8" in out
    assert "2.26" in out


# ── sensor readings ──────────────────────────────────────────────────────────


def _sht40(instance: int = 0, start_byte: int = 7) -> SensorData:
    return SensorData(
        instance_number=instance,
        sensor_type=SensorType.SHT40,
        bus_id=2,
        i2c_addr_7bit=0x44,
        msd_data_start_byte=start_byte,
        reserved=b"\x00" * 24,
    )


def _ctx_with_readings(config: GlobalConfig, readings: dict[int, SensorReading]) -> _InfoContext:
    return replace(_ctx(config), sensor_readings=readings)


def _reading(instance: int = 0, temperature_c: float = 22.4, humidity_percent: float = 47.1) -> SensorReading:
    return SensorReading(
        instance_number=instance,
        sensor_type=int(SensorType.SHT40),
        sensor_type_enum=SensorType.SHT40,
        temperature_c=temperature_c,
        humidity_percent=humidity_percent,
    )


def _tree_text(ctx: _InfoContext) -> str:
    console = Console(width=120, record=True, force_terminal=False)
    console.print(_build_info_tree(ctx))
    return console.export_text()


def test_sensor_line_shows_live_values() -> None:
    ctx = _ctx_with_readings(_config(sensors=[_sht40()]), {0: _reading()})

    out = _tree_text(ctx)

    assert "SHT40" in out
    assert "22.4 °C" in out
    assert "47.1 %RH" in out


def test_sensor_line_without_reading_is_unchanged() -> None:
    """A sensor we could not read still reports its hardware."""
    ctx = _ctx_with_readings(_config(sensors=[_sht40()]), {})

    out = _tree_text(ctx)

    assert "Sensor 0" in out
    assert "bus 2" in out
    assert "°C" not in out.split("Sensors")[1]


def test_sensor_json_carries_readings() -> None:
    ctx = _ctx_with_readings(_config(sensors=[_sht40()]), {0: _reading()})

    entry = _info_to_json(ctx)["hardware"]["sensors"][0]

    assert entry["instance"] == 0
    assert entry["type"] == "SHT40"
    assert entry["bus"] == 2
    assert entry["temperature_c"] == 22.4
    assert entry["humidity_percent"] == 47.1


def test_sensor_json_nulls_when_unread() -> None:
    ctx = _ctx_with_readings(_config(sensors=[_sht40()]), {})

    entry = _info_to_json(ctx)["hardware"]["sensors"][0]

    assert entry["temperature_c"] is None
    assert entry["humidity_percent"] is None


def test_readings_match_their_own_sensor_instance() -> None:
    """Two sensors, one readable: the value must not leak onto the other row."""
    config = _config(sensors=[_sht40(instance=0), _sht40(instance=1, start_byte=1)])
    ctx = _ctx_with_readings(config, {1: _reading(instance=1, temperature_c=18.2)})

    entries = _info_to_json(ctx)["hardware"]["sensors"]

    assert entries[0]["temperature_c"] is None
    assert entries[1]["temperature_c"] == 18.2


# ── READ_IDENT (protocol section) ────────────────────────────────────────────


def _ctx_with_ident(frame_hex: str) -> _InfoContext:
    return dataclasses.replace(_ctx(_config()), ident=parse_read_ident(bytes.fromhex(frame_hex)))


def _text_of(ctx: _InfoContext) -> str:
    console = Console(width=120, record=True, force_terminal=False)
    console.print(_build_info_tree(ctx))
    return console.export_text()


def test_protocol_section_reports_a_3_0_identity() -> None:
    ctx = _ctx_with_ident("004604010029a00f800003")  # reTerminal E1002 on 3.0.2

    text = _text_of(ctx)
    for line in ("Protocol", "ESP32", "Config-driven", "yes", "Needs migration", "no", "4000 bytes", "128"):
        assert line in text

    assert _info_to_json(ctx)["ident"] == {
        "fw_target": "ESP32",
        "proto_minor": 4,
        "config_driven": True,
        "needs_migration": False,
        "builtin_driver": False,
        "max_config": 4000,
        "max_seq_steps": 128,
    }


def test_protocol_section_for_a_reply_without_the_tail_shows_only_what_was_sent() -> None:
    ctx = _ctx_with_ident("00460303011c00b800680103")  # EFR32BG22 on an early config-driven build

    text = _text_of(ctx)

    assert "EFR32BG22" in text
    assert "Config-driven" not in text
    assert "Max config" not in text


def test_protocol_section_is_absent_without_an_identity() -> None:
    ctx = _ctx(_config())

    assert "Protocol" not in _text_of(ctx)
    assert _info_to_json(ctx)["ident"] is None


def test_report_for_a_device_without_config_shows_identity_and_no_display() -> None:
    """A fresh 3.0 device has no config: the report is its firmware and identity, nothing made up."""
    ident = parse_read_ident(bytes.fromhex("004604010029a00f800003"))
    ctx = dataclasses.replace(_ctx(None), ident=ident, width=0, height=0, color_scheme_name="", board_type_name=None)

    text = _text_of(ctx)

    assert "Protocol" in text
    assert "Display" not in text
    assert "0x0px" not in text
    rendered = _info_to_json(ctx)
    assert rendered["display"] is None
    assert rendered["ident"]["config_driven"] is True


# ── drive modes (piece 3b, decision 5) ───────────────────────────────────────

from pathlib import Path  # noqa: E402

from opendisplay.protocol.config_parser import parse_config_response  # noqa: E402

_ALPHA = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
_FW3 = {"major": 3, "minor": 0, "patch": 2, "sha": "bg22-dev"}


def _ctx_v3(config: GlobalConfig) -> _InfoContext:
    return dataclasses.replace(_ctx(config), fw=_FW3)


def test_drive_modes_are_listed_with_scheme_and_passes() -> None:
    console = Console(width=120, record=True, force_terminal=False)
    console.print(_build_info_tree(_ctx_v3(_ALPHA)))
    text = console.export_text()

    assert "Drive modes" in text
    assert "Mode 0  full  BWRY, 1 pass" in text  # inherits the display's scheme
    assert "Mode 1  colour16  scheme 10, 3 passes" in text


def test_drive_modes_are_in_the_json() -> None:
    modes = _info_to_json(_ctx_v3(_ALPHA))["display"]["drive_modes"]

    assert modes == [
        {"index": 0, "name": "full", "scheme": "BWRY", "scheme_value": 3, "inherits_scheme": True, "passes": 1},
        {
            "index": 1,
            "name": "colour16",
            "scheme": "scheme 10",
            "scheme_value": 10,
            "inherits_scheme": False,
            "passes": 3,
        },
    ]


def test_a_config_without_drive_modes_has_no_section() -> None:
    console = Console(width=120, record=True, force_terminal=False)
    console.print(_build_info_tree(_ctx(_config())))

    assert "Drive modes" not in console.export_text()
    assert _info_to_json(_ctx(_config()))["display"]["drive_modes"] == []
