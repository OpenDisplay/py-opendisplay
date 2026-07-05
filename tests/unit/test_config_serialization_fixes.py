"""Regression tests for config serialization correctness (C7, C8, M5, M6)."""

from __future__ import annotations

import dataclasses

from opendisplay.models.config import DataBus, DisplayConfig, ManufacturerData, PowerOption
from opendisplay.protocol.config_parser import (
    _parse_manufacturer_data,
    _parse_power_option,
)
from opendisplay.protocol.config_serializer import (
    serialize_data_bus,
    serialize_manufacturer_data,
    serialize_power_option,
)


# ── C7: ManufacturerData round-trip preserves simple-config metadata ──────────
def test_manufacturer_data_roundtrip_preserves_simple_config() -> None:
    m = ManufacturerData(
        manufacturer_id=0x2446,
        board_type=1,
        board_revision=2,
        reserved=b"\xaa" * 6,
        simple_config_driver_index=5,
        simple_config_display_index=6,
        simple_config_power_index=7,
        simple_config_configured_at=123456,
    )
    parsed = _parse_manufacturer_data(serialize_manufacturer_data(m))
    assert parsed.simple_config_driver_index == 5
    assert parsed.simple_config_display_index == 6
    assert parsed.simple_config_power_index == 7
    assert parsed.simple_config_configured_at == 123456
    assert parsed.reserved == b"\xaa" * 6


# ── M5: public SIZE constants match the firmware structs ──────────────────────
def test_public_sizes_match_firmware() -> None:
    assert PowerOption.SIZE == 30
    assert DisplayConfig.SIZE == 46
    assert DataBus.SIZE == 30


def test_display_config_from_bytes_parses_46_byte_packet() -> None:
    data = bytes(range(46))
    cfg = DisplayConfig.from_bytes(data)
    assert cfg.reserved == data[33:46]
    assert len(cfg.reserved) == 13


def test_data_bus_from_bytes_yields_full_14_byte_reserved() -> None:
    data = bytes(range(30))
    bus = DataBus.from_bytes(data)
    assert len(bus.reserved) == 14
    assert bus.reserved == data[16:30]


# ── C8: serializers pad short reserved buffers to the fixed size ──────────────
def test_data_bus_serialize_pads_short_reserved_to_full_length() -> None:
    bus = DataBus.from_bytes(bytes(30))
    short = dataclasses.replace(bus, reserved=b"\x01\x02")  # only 2 bytes
    out = serialize_data_bus(short)
    assert len(out) == 30  # would be 18 without padding, desyncing the stream


# ── M6: tx_power is treated as unsigned end-to-end ────────────────────────────
def test_tx_power_unsigned_roundtrip() -> None:
    raw = bytearray(30)
    raw[6] = 0xF4  # 244 unsigned; would be -12 if parsed signed
    po = PowerOption.from_bytes(bytes(raw))
    assert po.tx_power == 244

    parsed = _parse_power_option(bytes(raw))
    assert parsed.tx_power == 244

    out = serialize_power_option(po)  # must not raise struct.error
    assert out[6] == 0xF4
    assert len(out) == 30
