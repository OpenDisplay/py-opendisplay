"""Tests for the config-container CRC (CRC-16/CCITT-FALSE).

The config CRC is the canonical CRC shared by the nRF firmware, the Silabs
firmware, and the website toolbox: CRC-16/CCITT-FALSE (init 0xFFFF, poly 0x1021,
MSB-first, no reflection, no final XOR) computed over the container body with the
two length bytes forced to zero.

Ground-truth vectors are taken from Firmware_NRF/config_parser.c
(config_toolbox_outer_crc16).
"""

from __future__ import annotations

import struct

from opendisplay.protocol.config_parser import parse_tlv_config
from opendisplay.protocol.config_serializer import calculate_config_crc, serialize_config


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


def _required_tlv() -> bytes:
    return (
        _packet(0, 0x01, _system_payload())
        + _packet(1, 0x02, _manufacturer_payload())
        + _packet(2, 0x04, _power_payload())
        + _packet(3, 0x20, b"\x00" * 46)
    )


# ── Ground-truth vectors ──────────────────────────────────────────────────────
def test_crc_vector_000001() -> None:
    assert calculate_config_crc(bytes.fromhex("000001")) == 0xDCBD


def test_crc_vector_2a0001aabb() -> None:
    assert calculate_config_crc(bytes.fromhex("2a0001aabb")) == 0xC239


def test_crc_length_field_is_zeroed() -> None:
    # [len_lo len_hi] 01 0001 + fourteen 00 bytes -> 0x0D7F for ANY length bytes,
    # which proves the length field is excluded from the CRC.
    body_tail = bytes.fromhex("010001") + (b"\x00" * 14)
    assert calculate_config_crc(bytes.fromhex("4200") + body_tail) == 0x0D7F
    assert calculate_config_crc(bytes.fromhex("0000") + body_tail) == 0x0D7F


# ── serialize_config round-trip ───────────────────────────────────────────────
def test_serialize_config_appends_predicted_crc() -> None:
    config = parse_tlv_config(_required_tlv())
    blob = serialize_config(config)

    body, stored_crc = blob[:-2], int.from_bytes(blob[-2:], "little")

    # Stored CRC is little-endian CRC-16/CCITT-FALSE over the body.
    assert stored_crc == calculate_config_crc(body)
    # serialize_config writes a zeroed length field, so zeroing it again is a no-op.
    assert body[0:2] == b"\x00\x00"
