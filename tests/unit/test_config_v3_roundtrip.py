"""A real config with panel drive data, read from hardware, parses completely and is written back unchanged.

The fixture is the config of an EFR32BG22 tag running an early config-driven build
(184x360 BWRY), captured over BLE. Its encryption key was zeroed and the CRC recomputed
before it was saved.
"""

from __future__ import annotations

from pathlib import Path

from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import serialize_config

FIXTURE = (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()


def test_real_config_with_drive_data_parses_completely() -> None:
    config = parse_config_response(FIXTURE)

    assert config.unparsed_tail == b""
    assert [p.packet_type for p in config.length_prefixed_packets] == [0xB1, 0xAE, 0xAE, 0xAF, 0xAF, 0xB0]
    assert [(m.name, m.mode_index) for m in config.display_modes] == [("full", 0), ("colour16", 1)]
    assert config.display_panels[0].code_map == (0, 1, 3, 2)
    assert all(lut.crc_valid for lut in config.display_luts)
    assert (config.displays[0].pixel_width, config.displays[0].pixel_height) == (184, 360)


def test_real_config_with_drive_data_round_trips_byte_for_byte() -> None:
    assert serialize_config(parse_config_response(FIXTURE)) == FIXTURE


def test_fixture_carries_no_key() -> None:
    """Guards the fixture itself: a real key must never be committed with it."""
    security = parse_config_response(FIXTURE).security_config

    assert security is not None
    assert security.encryption_key == bytes(16)
