"""Packets the parser does not recognise must survive a read-modify-write.

LEGACY packet bodies (ids < 0x80) have no length field, so the parser cannot
step over an unknown legacy ID and stops there. Re-serializing only what was
parsed would then drop the unknown packet and everything after it from the
device. (Ids 0x80+ carry a length and are skipped individually instead -- see
test_drive_packets.py.)

0x2D findmy_config used to be this test's example of an unknown packet; it is
now modelled (as opaque bytes), which is itself asserted below -- the packet
BEHIND a 0x2D must no longer be lost.
"""

from __future__ import annotations

from test_config_json_roundtrip import _base_config, _display

from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import calculate_config_crc, serialize_config

# 0x5E: genuinely unknown, and (unlike 0x2E-0x31) not a burned id with a known
# size. The legacy range is frozen, so this can only come from a NEWER writer
# violating the freeze -- the parser still must not destroy what it kept.
UNKNOWN_PACKET = bytes([0, 0x5E]) + bytes(range(64))
# A known packet (led 0x21) after the unknown one: the parser cannot reach it either.
KNOWN_AFTER_UNKNOWN = bytes([0, 0x21]) + b"\x5a" * 22
TAIL = UNKNOWN_PACKET + KNOWN_AFTER_UNKNOWN

FINDMY = bytes([0, 0x2D]) + bytes(range(128))


def _with_tail(blob: bytes, tail: bytes) -> bytes:
    body = blob[:-2] + tail
    return body + calculate_config_crc(body).to_bytes(2, "little")


def test_unknown_legacy_packet_and_everything_after_it_survive_roundtrip() -> None:
    device_blob = _with_tail(serialize_config(_base_config(displays=[_display()])), TAIL)

    rewritten = serialize_config(parse_config_response(device_blob))

    assert rewritten[:-2].endswith(TAIL)
    assert rewritten[-2:] == calculate_config_crc(rewritten[:-2]).to_bytes(2, "little")


def test_findmy_is_no_longer_an_unknown_packet() -> None:
    """The original interop hole: 0x2D hid everything serialized after it.
    It is now parsed (opaquely), so the LED packet behind it must come back as
    a real object and nothing lands in unparsed_tail."""
    device_blob = _with_tail(
        serialize_config(_base_config(displays=[_display()])),
        FINDMY + KNOWN_AFTER_UNKNOWN,
    )

    cfg = parse_config_response(device_blob)
    assert cfg.findmy_raw == bytes(range(128))
    assert cfg.unparsed_tail == b""
    assert any(True for _ in cfg.leds)  # the packet behind 0x2D was reached

    rewritten = serialize_config(cfg)
    assert parse_config_response(
        rewritten[:3] + rewritten[3:]  # full wrapper round-trip
    ).findmy_raw == bytes(range(128))
