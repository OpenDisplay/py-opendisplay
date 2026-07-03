"""Tests for the DataExtended identity packet (TLV type 0x2c, config.yaml id 44)."""

from __future__ import annotations

import struct

import pytest

from opendisplay.models.config import (
    DataExtended,
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
    WifiConfig,
)
from opendisplay.models.config_json import config_from_json, config_to_json
from opendisplay.protocol.config_parser import parse_tlv_config
from opendisplay.protocol.config_serializer import serialize_config, serialize_data_extended

# ---------------------------------------------------------------------------
# Helpers shared with test_models_new_packets.py pattern
# ---------------------------------------------------------------------------


def _packet(number: int, packet_type: int, payload: bytes) -> bytes:
    return bytes([number, packet_type]) + payload


def _system_payload() -> bytes:
    return struct.pack("<HBBB", 1, 0, 0, 0) + (b"\x00" * 17)


def _manufacturer_payload() -> bytes:
    return struct.pack("<HBB", 1, 0, 1) + (b"\x00" * 18)


def _power_payload() -> bytes:
    return (
        bytes([1])
        + (1000).to_bytes(3, byteorder="little")
        + struct.pack("<HbBBBBBHIH", 1000, 0, 0, 0xFF, 0xFF, 0, 1, 100, 0, 0)
        + (b"\x00" * 10)
    )


def _display_payload() -> bytes:
    return b"\x00" * 46


def _required_tlv() -> bytes:
    return (
        _packet(0, 0x01, _system_payload())
        + _packet(1, 0x02, _manufacturer_payload())
        + _packet(2, 0x04, _power_payload())
        + _packet(3, 0x20, _display_payload())
    )


def _field(text: str) -> bytes:
    return text.encode("utf-8").ljust(32, b"\x00")


def _data_extended_payload(**texts: str) -> bytes:
    order = (
        "manufacturer_name",
        "model_name",
        "serial_number",
        "friendly_name",
        "device_location",
        "device_id",
        "custom_string_1",
        "custom_string_2",
        "custom_string_3",
    )
    return b"".join(_field(texts.get(name, "")) for name in order)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


class TestDataExtended:
    def test_from_bytes_parses_all_fields(self):
        payload = _data_extended_payload(
            manufacturer_name="OpenDisplay",
            model_name="OD 4.26 Mono Kit",
            serial_number="SN-0001",
            friendly_name="Kitchen Tag",
            device_location="Kitchen",
            device_id="od-426-0001",
            custom_string_1="one",
            custom_string_2="two",
            custom_string_3="three",
        )
        ext = DataExtended.from_bytes(payload)

        assert ext.manufacturer_name_text == "OpenDisplay"
        assert ext.model_name_text == "OD 4.26 Mono Kit"
        assert ext.serial_number_text == "SN-0001"
        assert ext.friendly_name_text == "Kitchen Tag"
        assert ext.device_location_text == "Kitchen"
        assert ext.device_id_text == "od-426-0001"
        assert ext.custom_string_1_text == "one"
        assert ext.custom_string_2_text == "two"
        assert ext.custom_string_3_text == "three"

    def test_empty_fields_decode_to_empty_string(self):
        ext = DataExtended.from_bytes(bytes(DataExtended.SIZE))
        assert ext.manufacturer_name_text == ""
        assert ext.custom_string_3_text == ""

    def test_default_instance_is_all_empty(self):
        ext = DataExtended()
        assert ext.to_bytes() == bytes(DataExtended.SIZE)
        assert ext.friendly_name_text == ""

    def test_from_strings_round_trip(self):
        ext = DataExtended.from_strings(
            manufacturer_name="Seeed Studio",
            friendly_name="Desk Display",
            device_id="rt-e1002-42",
        )
        ext2 = DataExtended.from_bytes(ext.to_bytes())
        assert ext2 == ext
        assert ext2.manufacturer_name_text == "Seeed Studio"
        assert ext2.friendly_name_text == "Desk Display"
        assert ext2.device_id_text == "rt-e1002-42"
        assert ext2.model_name_text == ""

    def test_from_strings_truncates_at_field_size(self):
        ext = DataExtended.from_strings(friendly_name="x" * 40)
        assert len(ext.friendly_name) == 32
        assert ext.friendly_name_text == "x" * 32

    def test_utf8_multibyte_strings(self):
        ext = DataExtended.from_strings(device_location="Küche", friendly_name="Türschild")
        ext2 = DataExtended.from_bytes(ext.to_bytes())
        assert ext2.device_location_text == "Küche"
        assert ext2.friendly_name_text == "Türschild"

    def test_to_bytes_is_288_bytes(self):
        assert len(DataExtended.from_strings().to_bytes()) == 288
        assert DataExtended.SIZE == 288

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid DataExtended size"):
            DataExtended.from_bytes(bytes(DataExtended.SIZE - 1))

    def test_wifi_config_c_string_helpers_unchanged(self):
        # The helpers moved to module level; the public WifiConfig staticmethods
        # must keep delegating with identical behavior.
        assert WifiConfig.encode_c_string("abc", 8) == b"abc\x00\x00\x00\x00\x00"
        assert WifiConfig.encode_c_string("x" * 10, 4) == b"xxxx"
        assert WifiConfig.decode_c_string(b"abc\x00garbage") == "abc"
        assert WifiConfig.decode_c_string(bytes(8)) == ""


# ---------------------------------------------------------------------------
# TLV parse / serialize
# ---------------------------------------------------------------------------


class TestDataExtendedTlv:
    def test_parse_from_tlv(self):
        tlv = _required_tlv() + _packet(4, 0x2C, _data_extended_payload(friendly_name="Hallway", device_id="tag-7"))
        config = parse_tlv_config(tlv)

        assert config.data_extended is not None
        assert config.data_extended.friendly_name_text == "Hallway"
        assert config.data_extended.device_id_text == "tag-7"

    def test_absent_packet_leaves_none(self):
        config = parse_tlv_config(_required_tlv())
        assert config.data_extended is None

    def test_packets_after_data_extended_still_parse(self):
        # data_extended must not truncate parsing of packets that follow it.
        tlv = (
            _required_tlv()
            + _packet(4, 0x2C, _data_extended_payload(serial_number="SN-9"))
            + _packet(5, 0x21, b"\x00" * 22)  # LED config
        )
        config = parse_tlv_config(tlv)

        assert config.data_extended is not None
        assert config.data_extended.serial_number_text == "SN-9"
        assert len(config.leds) == 1

    def test_serialize_data_extended_matches_payload(self):
        payload = _data_extended_payload(manufacturer_name="OpenDisplay", custom_string_2="two")
        assert serialize_data_extended(DataExtended.from_bytes(payload)) == payload

    def test_serialize_config_round_trip(self):
        tlv = _required_tlv() + _packet(
            4, 0x2C, _data_extended_payload(friendly_name="Bureau", device_location="Office")
        )
        config = parse_tlv_config(tlv)

        # serialize_config wraps in [pad:2][version:1]...[crc:2]
        reparsed = parse_tlv_config(serialize_config(config)[3:-2])

        assert reparsed.data_extended is not None
        assert reparsed.data_extended.friendly_name_text == "Bureau"
        assert reparsed.data_extended.device_location_text == "Office"

    def test_serialize_config_omits_when_none(self):
        config = parse_tlv_config(_required_tlv())
        reparsed = parse_tlv_config(serialize_config(config)[3:-2])
        assert reparsed.data_extended is None


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestDataExtendedJson:
    def _minimal_config(self, **kwargs) -> GlobalConfig:
        return GlobalConfig(
            system=SystemConfig(
                ic_type=2,
                communication_modes=0x01,
                device_flags=0x01,
                pwr_pin=0x2B,
                reserved=b"\x00" * 15,
            ),
            manufacturer=ManufacturerData(
                manufacturer_id=1,
                board_type=0,
                board_revision=1,
                reserved=b"\x00" * 18,
            ),
            power=PowerOption(
                power_mode=1,
                battery_capacity_mah=(2000).to_bytes(3, "little"),
                sleep_timeout_ms=1000,
                tx_power=8,
                sleep_flags=0,
                battery_sense_pin=0xFF,
                battery_sense_enable_pin=0xFF,
                battery_sense_flags=0,
                capacity_estimator=1,
                voltage_scaling_factor=100,
                deep_sleep_current_ua=0,
                deep_sleep_time_seconds=0,
                reserved=b"\x00" * 10,
            ),
            displays=[
                DisplayConfig(
                    instance_number=0,
                    display_technology=1,
                    panel_ic_type=33,
                    pixel_width=152,
                    pixel_height=296,
                    active_width_mm=0,
                    active_height_mm=0,
                    tag_type=0,
                    rotation=0,
                    reset_pin=0xFF,
                    busy_pin=0xFF,
                    dc_pin=0xFF,
                    cs_pin=0xFF,
                    data_pin=0,
                    partial_update_support=1,
                    color_scheme=0,
                    transmission_modes=0x0A,
                    clk_pin=0,
                    reserved_pins=b"\x00" * 7,
                    full_update_mC=0,
                    reserved=b"\x00" * 13,
                )
            ],
            version=1,
            **kwargs,
        )

    def test_data_extended_round_trip(self):
        ext = DataExtended.from_strings(
            manufacturer_name="OpenDisplay",
            model_name='7.3" Color Kit',
            friendly_name="Wohnzimmer",
            custom_string_3="drei",
        )
        cfg = self._minimal_config(data_extended=ext)

        exported = config_to_json(cfg)
        packet = next(p for p in exported["packets"] if p["id"] == "44")
        assert packet["name"] == "data_extended"
        assert packet["fields"]["friendly_name"] == "Wohnzimmer"

        reimported = config_from_json(exported)
        assert reimported.data_extended is not None
        assert reimported.data_extended.manufacturer_name_text == "OpenDisplay"
        assert reimported.data_extended.model_name_text == '7.3" Color Kit'
        assert reimported.data_extended.friendly_name_text == "Wohnzimmer"
        assert reimported.data_extended.custom_string_3_text == "drei"

    def test_absent_data_extended_not_exported(self):
        exported = config_to_json(self._minimal_config())
        assert all(p["id"] != "44" for p in exported["packets"])
        assert config_from_json(exported).data_extended is None
