"""A stored config without a display packet: strict by default, lenient on request.

Firmware 3.0 stores a config write as sent, so a config without 0x20 is a real state; a repair
must keep its other packets (Firmware_Unified f6d3a83, PROTOCOL_CHANGES_3.0 row 10).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from opendisplay.exceptions import ConfigParseError
from opendisplay.models.config import SystemConfig
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import serialize_config

BG22_CONFIG = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
NO_DISPLAY_BLOB = serialize_config(dataclasses.replace(BG22_CONFIG, displays=[]))


def test_strict_parse_rejects_a_config_without_a_display_with_the_same_message() -> None:
    with pytest.raises(ConfigParseError) as err:
        parse_config_response(NO_DISPLAY_BLOB)

    assert str(err.value) == "Missing required packet(s): display"


def test_lenient_parse_keeps_every_other_packet() -> None:
    config = parse_config_response(NO_DISPLAY_BLOB, require_display=False)

    assert config.displays == []
    assert config == dataclasses.replace(BG22_CONFIG, displays=[])
    assert config.security_config is not None
    assert config.length_prefixed_packets == BG22_CONFIG.length_prefixed_packets
    assert (len(config.leds), len(config.binary_inputs), len(config.data_buses)) == (1, 1, 1)


def test_lenient_parse_still_requires_system_manufacturer_and_power() -> None:
    # Only the display packet becomes optional. GlobalConfig cannot be built without a system
    # packet, so cut the system packet (the first one: [0][0x01][22 bytes]) out of the bytes.
    packets = NO_DISPLAY_BLOB[3:-2]
    assert packets[1] == 0x01
    rest = packets[2 + SystemConfig.SIZE :]
    raw = NO_DISPLAY_BLOB[:3] + rest + NO_DISPLAY_BLOB[-2:]

    with pytest.raises(ConfigParseError, match="system"):
        parse_config_response(raw, require_display=False)


def test_a_config_with_a_display_parses_the_same_either_way() -> None:
    blob = serialize_config(BG22_CONFIG)

    assert parse_config_response(blob, require_display=False) == parse_config_response(blob)
