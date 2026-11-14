"""Length-prefixed config packets (types 0x80+) and the panel drive data they carry (0xAE-0xB1)."""

from __future__ import annotations

import dataclasses
import json
import logging
import struct

import pytest
from test_config_json_roundtrip import _base_config, _display

from opendisplay import ColorScheme, GlobalConfig
from opendisplay.models import config_from_json, config_to_json
from opendisplay.models.panel_drive import (
    COLOR_SCHEME_INHERIT,
    LUT_SLOT_NONE,
    PACKET_TYPE_DISPLAY_CALIBRATION,
    PACKET_TYPE_DISPLAY_LUT,
    PACKET_TYPE_DISPLAY_MODE,
    PACKET_TYPE_DISPLAY_PANEL,
    DisplayCalibration,
    DisplayLut,
    DisplayMode,
    DisplayPanel,
    LengthPrefixedPacket,
    _crc16_ccitt_false,
)
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import serialize_config

# ─── Packet bodies, built field by field from the firmware struct layouts ────


def _mode_body(
    *,
    index: int = 1,
    name: bytes = b"colour16",
    scheme: int = 10,
    passes: int = 3,
    sequence: bytes = b"\x02\x12\x00\x00\x00",
) -> bytes:
    lut_slots = bytes([0, 1, 1]) + bytes([LUT_SLOT_NONE]) * 5
    vsn1 = bytes([0, 20, 65, 0, 0, 0, 0, 0])
    head = struct.pack("<BB12sBBbbB8s8sH", 0, index, name, scheme, passes, -5, 32, 1, lut_slots, vsn1, len(sequence))
    assert len(head) == 37
    return head + sequence


def _lut_body(data: bytes = bytes(range(64)), *, slot: int = 1, crc: int | None = None) -> bytes:
    return struct.pack("<BBH", 0, slot, _crc16_ccitt_false(data) if crc is None else crc) + data


def _calibration_body() -> bytes:
    entries = [(0x0002, 3047, 870, 458), (0x0013, 879, -1546, 774)]
    return struct.pack("<BBBB", 0, 1, 0x01, len(entries)) + b"".join(struct.pack("<Hhhh", *e) for e in entries)


def _panel_body(sleep_sequence: bytes = b"\x01\x02\x00\x00") -> bytes:
    head = struct.pack("<BBBHH4sBB", 0, 0x03, 1, 30000, 250, bytes([0, 1, 3, 2]), 2, len(sleep_sequence))
    assert len(head) == 13
    return head + sleep_sequence


_PACKETS = [
    LengthPrefixedPacket(0, PACKET_TYPE_DISPLAY_PANEL, _panel_body()),
    LengthPrefixedPacket(0, PACKET_TYPE_DISPLAY_MODE, _mode_body()),
    LengthPrefixedPacket(1, PACKET_TYPE_DISPLAY_LUT, _lut_body()),
    LengthPrefixedPacket(0, PACKET_TYPE_DISPLAY_CALIBRATION, _calibration_body()),
    LengthPrefixedPacket(7, 0xC4, b"a type nobody has defined yet"),
]


def _config(packets: list[LengthPrefixedPacket] | None = None) -> GlobalConfig:
    config = _base_config(displays=[_display()])
    return dataclasses.replace(config, length_prefixed_packets=list(_PACKETS if packets is None else packets))


def _reparse(config: GlobalConfig) -> GlobalConfig:
    return parse_config_response(serialize_config(config))


# ─── The length-prefixed range ───────────────────────────────────────────────


def test_packet_wire_format() -> None:
    assert LengthPrefixedPacket(3, 0xB1, b"\xaa\xbb\xcc").to_bytes() == b"\x03\xb1\x03\x00\xaa\xbb\xcc"


def test_packets_survive_a_binary_round_trip_in_order(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        back = _reparse(_config())

    assert back.length_prefixed_packets == _PACKETS
    assert back.unparsed_tail == b""
    assert caplog.records == []  # an unknown 0x80+ type is skippable by design, not a warning


def test_serialized_packets_follow_every_legacy_packet() -> None:
    blob = serialize_config(_config())
    tail = b"".join(packet.to_bytes() for packet in _PACKETS)

    assert blob[-2 - len(tail) : -2] == tail  # last thing before the CRC


def test_a_legacy_packet_after_a_length_prefixed_one_is_still_parsed() -> None:
    """The length lets the parser step over a 0x80+ packet instead of stopping at it."""
    plain = serialize_config(_config(packets=[]))
    header, body, crc = plain[:3], plain[3:-2], plain[-2:]
    first_packet_size = 2 + 22  # system_config
    spliced = body[:first_packet_size] + _PACKETS[-1].to_bytes() + body[first_packet_size:]

    back = parse_config_response(header + spliced + crc)

    assert back.length_prefixed_packets == [_PACKETS[-1]]
    assert back.displays and back.power is not None


def test_truncated_packet_is_kept_as_unparsed_tail(caplog: pytest.LogCaptureFixture) -> None:
    plain = serialize_config(_config(packets=[]))
    truncated = bytes([0, 0xB1, 200, 0]) + b"only a few bytes"

    with caplog.at_level(logging.WARNING):
        back = parse_config_response(plain[:-2] + truncated + plain[-2:])

    assert back.length_prefixed_packets == []
    assert back.unparsed_tail == truncated
    assert "Truncated length-prefixed packet 0xb1" in caplog.text


def test_packets_survive_a_json_round_trip() -> None:
    exported = json.loads(json.dumps(config_to_json(_config())))

    back = config_from_json(exported)

    assert back.length_prefixed_packets == _PACKETS
    assert serialize_config(back) == serialize_config(_config())


def test_json_without_packets_has_no_extra_key() -> None:
    assert "length_prefixed_packets" not in config_to_json(_config(packets=[]))


@pytest.mark.parametrize(
    "entries",
    [
        "0xb1",  # not a list
        [{"number": 0, "id": "177"}],  # no body
        [{"number": 0, "id": "177", "body": "0xzz"}],  # body is not hex
        [{"number": 0, "id": "32", "body": "0x00"}],  # a legacy type is not length-prefixed
        [{"number": 256, "id": "177", "body": "0x00"}],  # number does not fit a byte
    ],
)
def test_malformed_json_packets_are_rejected(entries: object) -> None:
    exported = config_to_json(_config(packets=[]))
    exported["length_prefixed_packets"] = entries

    with pytest.raises(ValueError, match="length_prefixed_packets"):
        config_from_json(exported)


# ─── Panel drive data ────────────────────────────────────────────────────────


def test_display_mode() -> None:
    (mode,) = _config().display_modes

    assert (mode.instance_number, mode.mode_index, mode.name) == (0, 1, "colour16")
    assert (mode.passes, mode.temp_min_c, mode.temp_max_c, mode.temp_policy) == (3, -5, 32, 1)
    assert mode.lut_slots == (0, 1, 1)  # one per pass; the unused slots are dropped
    assert mode.vsn1 == (0, 20, 65)
    assert mode.sequence == b"\x02\x12\x00\x00\x00"
    assert mode.color_scheme_enum == 10  # BWRY16: a scheme the library has no enum member for


def test_display_mode_scheme_inherits_or_names_a_known_scheme() -> None:
    assert DisplayMode.from_body(_mode_body(scheme=COLOR_SCHEME_INHERIT)).color_scheme_enum is None
    assert DisplayMode.from_body(_mode_body(scheme=3)).color_scheme_enum is ColorScheme.BWRY


def test_display_lut_checks_its_crc() -> None:
    (lut,) = _config().display_luts

    assert (lut.instance_number, lut.lut_slot, lut.data) == (0, 1, bytes(range(64)))
    assert lut.crc_valid
    assert not DisplayLut.from_body(_lut_body(crc=0x1234)).crc_valid


def test_crc_matches_the_ccitt_false_check_value() -> None:
    assert _crc16_ccitt_false(b"123456789") == 0x29B1


def test_display_calibration() -> None:
    (calibration,) = _config().display_calibrations

    assert (calibration.instance_number, calibration.mode_index, calibration.measured) == (0, 1, True)
    first, last = calibration.entries
    assert (first.lab_l, first.lab_a, first.lab_b) == (30.47, 8.7, 4.58)
    assert last.lab_a == -15.46  # signed
    # codes 0x0013 = 0b01_00_11: pass 0 -> 3, pass 1 -> 0, pass 2 -> 1
    assert [last.code_for_pass(p) for p in range(3)] == [3, 0, 1]


def test_display_panel() -> None:
    (panel,) = _config().display_panels

    assert panel.busy_usable and panel.lut_supported
    assert (panel.busy_idle_level, panel.busy_timeout_ms, panel.busy_fixed_delay_ms) == (1, 30000, 250)
    assert panel.code_map == (0, 1, 3, 2)
    assert panel.mode_count == 2
    assert panel.sleep_sequence == b"\x01\x02\x00\x00"


@pytest.mark.parametrize(
    ("decode", "body"),
    [
        (DisplayMode.from_body, _mode_body()[:20]),  # shorter than the head
        (DisplayMode.from_body, _mode_body()[:-1]),  # shorter than its declared sequence
        (DisplayLut.from_body, b"\x00\x01"),
        (DisplayCalibration.from_body, _calibration_body()[:-3]),  # last entry cut short
        (DisplayPanel.from_body, _panel_body()[:-1]),
    ],
)
def test_short_bodies_are_rejected(decode, body: bytes) -> None:
    with pytest.raises(ValueError, match="too short"):
        decode(body)


def test_a_malformed_packet_is_skipped_but_still_written_back(caplog: pytest.LogCaptureFixture) -> None:
    broken = LengthPrefixedPacket(0, PACKET_TYPE_DISPLAY_MODE, b"\x00\x01")
    config = _config(packets=[broken, _PACKETS[1]])

    with caplog.at_level(logging.WARNING):
        modes = config.display_modes

    assert [mode.name for mode in modes] == ["colour16"]
    assert "Ignoring malformed config packet 0xae" in caplog.text
    assert _reparse(config).length_prefixed_packets == [broken, _PACKETS[1]]


def test_a_config_without_panel_drive_data_has_empty_views() -> None:
    config = _config(packets=[])

    assert config.display_modes == config.display_luts == config.display_calibrations == config.display_panels == []
