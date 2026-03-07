"""Test required packet enforcement for parse and write paths."""

from __future__ import annotations

import logging
import struct

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import ConfigParseError
from opendisplay.models import config_from_json, config_to_json
from opendisplay.models.config import (
    BinaryInputs,
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.protocol.config_parser import parse_tlv_config
from opendisplay.protocol.config_serializer import serialize_binary_inputs


def _system_payload() -> bytes:
    return struct.pack("<HBBB", 1, 0, 0, 0) + (b"\x00" * 17)


def _manufacturer_payload() -> bytes:
    return struct.pack("<HBB", 1, 0, 1) + (b"\x00" * 18)


def _power_payload() -> bytes:
    return (
        bytes([1])  # power_mode
        + (1000).to_bytes(3, byteorder="little")
        + struct.pack("<HbBBBBBHIH", 1000, 0, 0, 0xFF, 0xFF, 0, 1, 100, 0, 0)
        + (b"\x00" * 10)
    )


def _packet(number: int, packet_type: int, payload: bytes) -> bytes:
    return bytes([number, packet_type]) + payload


def _display_payload() -> bytes:
    return b"\x00" * 46


def _required_tlv(
    *,
    include_system: bool = True,
    include_manufacturer: bool = True,
    include_power: bool = True,
    include_display: bool = True,
) -> bytes:
    parts: list[bytes] = []
    packet_number = 0

    if include_system:
        parts.append(_packet(packet_number, 0x01, _system_payload()))
        packet_number += 1
    if include_manufacturer:
        parts.append(_packet(packet_number, 0x02, _manufacturer_payload()))
        packet_number += 1
    if include_power:
        parts.append(_packet(packet_number, 0x04, _power_payload()))
        packet_number += 1
    if include_display:
        parts.append(_packet(packet_number, 0x20, _display_payload()))

    return b"".join(parts)


def _wifi_payload() -> bytes:
    ssid = b"MyWifi".ljust(32, b"\x00")
    password = b"secret123".ljust(32, b"\x00")
    encryption_type = bytes([0x03])  # WPA2
    server_url = b"opendisplay.local".ljust(64, b"\x00")
    server_port = (2446).to_bytes(2, byteorder="big")
    reserved = b"\x00" * 29
    return ssid + password + encryption_type + server_url + server_port + reserved


def _wifi_payload_legacy() -> bytes:
    ssid = b"MyWifi".ljust(32, b"\x00")
    password = b"secret123".ljust(32, b"\x00")
    encryption_type = bytes([0x03])  # WPA2
    return ssid + password + encryption_type


def _binary_input_payload(*, button_data_byte_index: int = 0) -> bytes:
    return (
        bytes([0x00, 0x01, 0x00])  # instance_number, input_type, display_as
        + bytes([0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17])  # reserved pins
        + bytes([0x01, 0x02, 0x03, 0x04])  # flags, invert, pullups, pulldowns
        + bytes([button_data_byte_index & 0xFF])
        + (b"\x00" * 14)
    )


def test_parse_tlv_requires_system_manufacturer_power() -> None:
    """Parser should fail when required packets are missing."""
    data = _required_tlv(include_manufacturer=False)

    with pytest.raises(ConfigParseError, match="Missing required packet\\(s\\): manufacturer"):
        parse_tlv_config(data)


def test_parse_tlv_succeeds_when_required_packets_present() -> None:
    """Parser should succeed when all required packets are present."""
    cfg = parse_tlv_config(_required_tlv())

    assert cfg.system.ic_type == 1
    assert cfg.manufacturer.manufacturer_id == 1
    assert cfg.power.power_mode == 1
    assert len(cfg.displays) == 1


def test_parse_tlv_requires_display() -> None:
    """Parser should fail when display packet is missing."""
    data = _required_tlv(include_display=False)

    with pytest.raises(ConfigParseError, match="Missing required packet\\(s\\): display"):
        parse_tlv_config(data)


def test_parse_tlv_supports_wifi_config_packet_without_unknown_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Parser should recognize packet 0x26 (wifi_config) and not warn as unknown."""
    data = _required_tlv() + _packet(4, 0x26, _wifi_payload())

    with caplog.at_level(logging.WARNING, logger="opendisplay.protocol.config_parser"):
        cfg = parse_tlv_config(data)

    assert cfg.wifi_config is not None
    assert cfg.wifi_config.ssid_text == "MyWifi"
    assert cfg.wifi_config.server_port == 2446
    assert "Unknown packet type 0x26" not in caplog.text


def test_parse_tlv_supports_legacy_wifi_config_size() -> None:
    """Parser should accept legacy 65-byte wifi_config packets."""
    data = _required_tlv() + _packet(4, 0x26, _wifi_payload_legacy())

    cfg = parse_tlv_config(data)

    assert cfg.wifi_config is not None
    assert cfg.wifi_config.ssid_text == "MyWifi"
    assert cfg.wifi_config.password_text == "secret123"
    assert cfg.wifi_config.encryption_type == 0x03
    assert cfg.wifi_config.server_url_text == ""
    assert cfg.wifi_config.server_port == 2446


def test_parse_tlv_binary_input_parses_button_data_byte_index() -> None:
    """Parser should expose BinaryInputs.button_data_byte_index from packet 0x25."""
    data = _required_tlv() + _packet(4, 0x25, _binary_input_payload(button_data_byte_index=7))

    cfg = parse_tlv_config(data)

    assert len(cfg.binary_inputs) == 1
    assert cfg.binary_inputs[0].button_data_byte_index == 7


def _required_json(*, include_display: bool = True) -> dict:
    packets = [
        {"id": "1", "fields": {}},
        {"id": "2", "fields": {}},
        {"id": "4", "fields": {}},
    ]
    if include_display:
        packets.append({"id": "32", "fields": {}})
    return {"version": 1, "minor_version": 1, "packets": packets}


def _minimal_display() -> DisplayConfig:
    return DisplayConfig(
        instance_number=0,
        display_technology=0,
        panel_ic_type=0,
        pixel_width=0,
        pixel_height=0,
        active_width_mm=0,
        active_height_mm=0,
        tag_type=0,
        rotation=0,
        reset_pin=0xFF,
        busy_pin=0xFF,
        dc_pin=0xFF,
        cs_pin=0xFF,
        data_pin=0xFF,
        partial_update_support=0,
        color_scheme=0,
        transmission_modes=0,
        clk_pin=0xFF,
        reserved_pins=b"\x00" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )


def test_config_from_json_requires_display() -> None:
    """JSON import should fail when display packet is missing."""
    with pytest.raises(ValueError, match="Missing required packet\\(s\\): display"):
        config_from_json(_required_json(include_display=False))


def test_config_from_json_succeeds_when_display_present() -> None:
    """JSON import should succeed when required packets and a display are present."""
    cfg = config_from_json(_required_json(include_display=True))

    assert cfg.system.ic_type == 0
    assert cfg.manufacturer.manufacturer_id == 0
    assert cfg.power.power_mode == 0
    assert len(cfg.displays) == 1


def test_config_from_json_binary_input_reads_button_data_byte_index() -> None:
    """JSON import should map button_data_byte_index to BinaryInputs."""
    payload = _required_json(include_display=True)
    payload["packets"].append(
        {
            "id": "37",
            "fields": {
                "instance_number": "0x0",
                "input_type": "1",
                "display_as": "0",
                "input_flags": "0x0",
                "invert": "0x0",
                "pullups": "0x0",
                "pulldowns": "0x0",
                "button_data_byte_index": "0x9",
            },
        }
    )

    cfg = config_from_json(payload)

    assert len(cfg.binary_inputs) == 1
    assert cfg.binary_inputs[0].button_data_byte_index == 9


def test_config_to_json_binary_input_exports_button_data_byte_index() -> None:
    """JSON export should include button_data_byte_index for binary inputs."""
    cfg = GlobalConfig(
        system=_minimal_system(),
        manufacturer=_minimal_manufacturer(),
        power=_minimal_power(),
        displays=[_minimal_display()],
        binary_inputs=[
            BinaryInputs(
                instance_number=0,
                input_type=1,
                display_as=0,
                reserved_pins=b"\x00" * 8,
                input_flags=0,
                invert=0,
                pullups=0,
                pulldowns=0,
                button_data_byte_index=5,
                reserved=b"\x00" * 14,
            )
        ],
    )

    exported = config_to_json(cfg)
    packet = next(p for p in exported["packets"] if p["id"] == "37")

    assert packet["fields"]["button_data_byte_index"] == "0x5"


def test_serialize_binary_inputs_writes_button_data_byte_index_byte() -> None:
    """BinaryInputs serializer should write the byte-index field at offset 15."""
    binary = BinaryInputs(
        instance_number=0,
        input_type=1,
        display_as=0,
        reserved_pins=b"\x00" * 8,
        input_flags=0,
        invert=0,
        pullups=0,
        pulldowns=0,
        button_data_byte_index=6,
        reserved=b"\x00" * 14,
    )

    payload = serialize_binary_inputs(binary)

    assert len(payload) == 30
    assert payload[15] == 6


def _minimal_system() -> SystemConfig:
    return SystemConfig(
        ic_type=1,
        communication_modes=1,
        device_flags=0,
        pwr_pin=0xFF,
        reserved=b"\x00" * 17,
    )


def _minimal_manufacturer() -> ManufacturerData:
    return ManufacturerData(
        manufacturer_id=1,
        board_type=0,
        board_revision=1,
        reserved=b"\x00" * 18,
    )


def _minimal_power() -> PowerOption:
    return PowerOption(
        power_mode=1,
        battery_capacity_mah=(1000).to_bytes(3, "little"),
        sleep_timeout_ms=1000,
        tx_power=0,
        sleep_flags=0,
        battery_sense_pin=0xFF,
        battery_sense_enable_pin=0xFF,
        battery_sense_flags=0,
        capacity_estimator=1,
        voltage_scaling_factor=100,
        deep_sleep_current_ua=0,
        deep_sleep_time_seconds=0,
        reserved=b"\x00" * 10,
    )


@pytest.mark.asyncio
async def test_write_config_requires_system_manufacturer_power() -> None:
    """Write path should fail when required packets are missing."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    cfg = GlobalConfig(
        system=_minimal_system(),
        manufacturer=None,  # type: ignore[arg-type]
        power=None,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="Config missing required packets: manufacturer, power"):
        await device.write_config(cfg)


@pytest.mark.asyncio
async def test_write_config_still_requires_display() -> None:
    """Write path should still require at least one display block."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    cfg = GlobalConfig(
        system=_minimal_system(),
        manufacturer=_minimal_manufacturer(),
        power=_minimal_power(),
        displays=[],
    )

    with pytest.raises(ValueError, match="at least one display"):
        await device.write_config(cfg)
