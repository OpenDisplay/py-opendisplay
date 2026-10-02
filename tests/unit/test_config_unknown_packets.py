"""Packets the parser does not recognise must survive a read-modify-write.

Packet bodies have no length field, so the parser cannot step over an unknown
packet ID and stops there. Re-serializing only what was parsed would then drop
the unknown packet and everything after it from the device (e.g. findmy_config
0x2D, which this library does not model yet).
"""

from __future__ import annotations

from test_config_json_roundtrip import _base_config, _display

from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import calculate_config_crc, serialize_config

UNKNOWN_PACKET = bytes([0, 0x2D]) + bytes(range(128))
# A known packet (led 0x21) after the unknown one: the parser cannot reach it either.
KNOWN_AFTER_UNKNOWN = bytes([0, 0x21]) + b"\x5a" * 22
TAIL = UNKNOWN_PACKET + KNOWN_AFTER_UNKNOWN


def _with_tail(blob: bytes, tail: bytes) -> bytes:
    body = blob[:-2] + tail
    return body + calculate_config_crc(body).to_bytes(2, "little")


def test_unknown_packet_and_everything_after_it_survive_roundtrip() -> None:
    device_blob = _with_tail(serialize_config(_base_config(displays=[_display()])), TAIL)

    rewritten = serialize_config(parse_config_response(device_blob))

    assert rewritten[:-2].endswith(TAIL)
    assert rewritten[-2:] == calculate_config_crc(rewritten[:-2]).to_bytes(2, "little")
