"""opendisplay.connect() and Device: connect, read version, config and identity, build the profile."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import opendisplay
from opendisplay import Device, Generation, OpenDisplayDevice
from opendisplay.exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    ConfigParseError,
    InvalidResponseError,
    NoStoredConfigError,
    OpenDisplayTimeoutError,
)
from opendisplay.models.config import GlobalConfig
from opendisplay.models.enums import ICType
from opendisplay.models.ident import FirmwareTarget
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import serialize_config

BG22_CONFIG = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
ESP32_CONFIG = dataclasses.replace(
    BG22_CONFIG,
    system=dataclasses.replace(BG22_CONFIG.system, ic_type=ICType.ESP32_S3),
    length_prefixed_packets=[],
)

NO_CONFIG = bytes.fromhex("ff400000")
IDENT_V3_NO_DISPLAY = bytes.fromhex("004604010029a00f800003")  # E1002, Firmware_Unified 3.0.2, no config

READ_CONFIG = b"\x00\x40"
READ_FW_VERSION = b"\x00\x43"
READ_IDENT = b"\x00\x46"


def _version(major: int, minor: int, patch: int = 0) -> bytes:
    sha = b"0" * 40
    return b"\x00\x43" + bytes([major, minor, len(sha)]) + sha + bytes([patch])


def _config_reply(config: GlobalConfig) -> bytes:
    """One READ_CONFIG frame carrying the whole config: [echo:2][chunk:2][total:2][data]."""
    blob = serialize_config(config)
    return READ_CONFIG + b"\x00\x00" + len(blob).to_bytes(2, "little") + blob


def _opcodes(written: list[bytes]) -> list[bytes]:
    return [frame[:2] for frame in written]


async def test_connect_to_a_2x_device_reads_version_and_config_but_not_identity(fake_transport) -> None:
    fake = fake_transport([_version(2, 27, 1), _config_reply(ESP32_CONFIG)])

    async with opendisplay.connect(transport=fake) as dev:
        assert isinstance(dev, Device)
        assert dev.profile.generation is Generation.V2
        assert (dev.profile.width, dev.profile.height) == (184, 360)
        assert (dev.profile.ident, dev.profile.ident_missing) == (None, False)
        assert dev.config.cached is not None

    assert _opcodes(fake.written) == [READ_FW_VERSION, READ_CONFIG]
    assert READ_IDENT not in _opcodes(fake.written)


async def test_connect_to_a_2x_efr32bg22_does_not_ask_for_the_identity(fake_transport) -> None:  # decision 3
    fake = fake_transport([_version(2, 26), _config_reply(BG22_CONFIG)])

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.generation is Generation.V2
        assert (dev.profile.ident, dev.profile.ident_missing) == (None, False)
        assert dev.profile.target is FirmwareTarget.EFR32BG22

    assert READ_IDENT not in _opcodes(fake.written)


async def test_connect_to_a_3_0_device_reads_its_identity(fake_transport) -> None:
    fake = fake_transport([_version(3, 0, 2), NO_CONFIG, IDENT_V3_NO_DISPLAY])

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.generation is Generation.V3
        assert dev.profile.configured is False
        assert dev.profile.max_config == 4000
        assert dev.profile.target is FirmwareTarget.ESP32
        assert dev.config.cached is None

    assert _opcodes(fake.written) == [READ_FW_VERSION, READ_CONFIG, READ_IDENT]


async def test_3_0_device_that_does_not_answer_the_identity_is_still_v3(fake_transport) -> None:  # Review Focus 1
    fake = fake_transport([_version(3, 0), _config_reply(ESP32_CONFIG), OpenDisplayTimeoutError("silent")])

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.generation is Generation.V3
        assert (dev.profile.ident, dev.profile.ident_missing) == (None, True)
        assert dev.profile.configured is True


async def test_stored_config_without_displays_connects_unconfigured_and_keeps_its_other_packets(
    fake_transport,
) -> None:
    # Firmware 3.0 stores a config write as sent, so a config without 0x20 is a real state. The
    # new entry point reads it leniently so that a repair keeps the other packets.
    stored = dataclasses.replace(BG22_CONFIG, displays=[])
    fake = fake_transport([_version(3, 0), _config_reply(stored), IDENT_V3_NO_DISPLAY])

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.configured is False
        assert dev.config.cached == parse_config_response(serialize_config(stored), require_display=False)
        assert dev.config.cached is not None
        assert dev.config.cached.security_config is not None
        assert dev.config.cached.length_prefixed_packets == BG22_CONFIG.length_prefixed_packets
        with pytest.raises(RuntimeError, match="capabilities unknown"):
            _ = dev.legacy.width


async def test_legacy_object_still_works_after_a_normal_connect(fake_transport) -> None:
    fake = fake_transport([_version(2, 27), _config_reply(ESP32_CONFIG)])

    async with opendisplay.connect(transport=fake) as dev:
        assert (dev.legacy.width, dev.legacy.height) == (184, 360)
        assert dev.legacy.config is dev.config.cached


async def test_legacy_open_display_device_stays_strict(fake_transport) -> None:
    fake = fake_transport([_config_reply(dataclasses.replace(ESP32_CONFIG, displays=[]))])

    with pytest.raises(ConfigParseError, match="display"):
        async with OpenDisplayDevice(transport=fake):
            pytest.fail("entered")


async def test_unknown_colour_scheme_does_not_stop_the_connect(fake_transport) -> None:  # Review Focus 3
    # An unknown 0x20 value, as a newer firmware might define. (Mode-level schemes such as
    # BWRY16 live in 0xAE packets and never in 0x20.)
    display = dataclasses.replace(ESP32_CONFIG.displays[0], color_scheme=0x7E)
    config = dataclasses.replace(ESP32_CONFIG, displays=[display])
    fake = fake_transport([_version(3, 0), _config_reply(config), IDENT_V3_NO_DISPLAY])

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.color_scheme == 0x7E
        assert dev.profile.configured is True
        assert dev.config.cached == parse_config_response(serialize_config(config))
        assert dev.legacy.config is dev.config.cached
        # The legacy object has the config but no capabilities (documented on Device.legacy).
        with pytest.raises(RuntimeError, match="capabilities unknown"):
            _ = dev.legacy.width


async def test_errors_from_the_config_read_propagate(fake_transport, monkeypatch) -> None:
    fake = fake_transport([_version(2, 27)])

    async def not_connected(self: OpenDisplayDevice) -> bytes:
        raise RuntimeError("Not connected")

    monkeypatch.setattr(OpenDisplayDevice, "_read_config_bytes", not_connected)

    with pytest.raises(RuntimeError, match="Not connected"):
        async with opendisplay.connect(transport=fake):
            pytest.fail("entered despite the failed config read")

    assert fake.is_connected is False


async def test_a_config_the_parser_rejects_for_another_reason_still_fails(fake_transport) -> None:
    reply = bytearray(_config_reply(ESP32_CONFIG))
    reply[6 + 3 + 1] = 0x7D  # first packet's type byte: an unknown legacy id where the system packet was
    fake = fake_transport([_version(2, 27), bytes(reply)])

    with pytest.raises(ConfigParseError, match="system"):
        async with opendisplay.connect(transport=fake):
            pytest.fail("entered")

    assert fake.is_connected is False


async def test_failure_while_building_the_profile_closes_the_link(fake_transport) -> None:  # Review Focus 4
    fake = fake_transport([b"\x00\x43\x01"])  # truncated version reply

    with pytest.raises(InvalidResponseError):
        async with opendisplay.connect(transport=fake):
            pytest.fail("entered despite the failed version read")

    assert fake.is_connected is False


async def test_cancellation_while_building_the_profile_closes_the_link(fake_transport) -> None:  # Review Focus 4
    import asyncio

    fake = fake_transport([_version(2, 27), asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        async with opendisplay.connect(transport=fake):
            pytest.fail("entered despite the cancelled config read")

    assert fake.is_connected is False


async def test_leaving_the_context_closes_the_link(fake_transport) -> None:
    fake = fake_transport([_version(2, 27), _config_reply(ESP32_CONFIG)])

    async with opendisplay.connect(transport=fake):
        assert fake.is_connected is True

    assert fake.is_connected is False


async def test_config_read_on_a_device_without_config_raises(fake_transport) -> None:
    fake = fake_transport([_version(3, 0), NO_CONFIG, IDENT_V3_NO_DISPLAY, NO_CONFIG])

    async with opendisplay.connect(transport=fake) as dev:
        with pytest.raises(NoStoredConfigError):
            await dev.config.read()


async def test_config_read_returns_and_caches_the_stored_config(fake_transport) -> None:
    fake = fake_transport([_version(2, 27), _config_reply(ESP32_CONFIG), _config_reply(BG22_CONFIG)])

    async with opendisplay.connect(transport=fake) as dev:
        config = await dev.config.read()

        assert config.system.ic_type == ICType.EFR32BG22
        assert dev.config.cached is config


async def test_config_write_refreshes_the_profile(fake_transport) -> None:
    blob = serialize_config(ESP32_CONFIG)
    chunks = 1 + max(0, (len(blob) - 200 + 199) // 200)
    acks = [b"\x00\x41"] + [b"\x00\x42"] * (chunks - 1)
    fake = fake_transport(
        [_version(3, 0), NO_CONFIG, IDENT_V3_NO_DISPLAY]
        + acks
        + [_version(3, 0), _config_reply(ESP32_CONFIG), IDENT_V3_NO_DISPLAY]
    )

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.configured is False

        await dev.config.write(ESP32_CONFIG)

        assert dev.profile.configured is True
        assert dev.config.cached is not None


def test_conflicting_addressing_is_rejected() -> None:  # Review Focus 5
    with pytest.raises(ValueError, match="exactly one addressing mode"):
        opendisplay.connect("AA:BB:CC:DD:EE:FF", host="10.0.0.5")


def test_address_and_name_together_are_rejected() -> None:
    with pytest.raises(ValueError, match="mac_address or device_name"):
        opendisplay.connect("AA:BB:CC:DD:EE:FF", name="OD4064C9")


async def test_auto_interrogate_false_sends_no_config_read(fake_transport) -> None:
    fake = fake_transport([])

    async with OpenDisplayDevice(transport=fake, auto_interrogate=False) as device:
        assert device.config is None

    assert fake.written == []


async def test_default_still_interrogates(fake_transport) -> None:
    fake = fake_transport([_config_reply(ESP32_CONFIG)])

    async with OpenDisplayDevice(transport=fake) as device:
        assert device.config is not None

    assert _opcodes(fake.written) == [READ_CONFIG]


async def test_3_0_device_with_a_malformed_identity_reply_still_connects(fake_transport, caplog) -> None:
    fake = fake_transport([_version(3, 0), _config_reply(ESP32_CONFIG), bytes.fromhex("00460401")])  # cut short

    async with opendisplay.connect(transport=fake) as dev:
        assert dev.profile.generation is Generation.V3
        assert (dev.profile.ident, dev.profile.ident_missing) == (None, True)

    assert "identity" in caplog.text.lower()


async def test_link_loss_during_the_identity_read_still_fails_the_connect(fake_transport) -> None:
    fake = fake_transport([_version(3, 0), _config_reply(ESP32_CONFIG), BLEConnectionError("link lost")])

    with pytest.raises(BLEConnectionError):
        async with opendisplay.connect(transport=fake):
            pytest.fail("entered despite the lost link")

    assert fake.is_connected is False


# Pins the current failure mode for piece 4 (SEC2). connect(encryption_key=...) runs the 2.x
# handshake inside OpenDisplayDevice.__aenter__, before the firmware version is read. Firmware 3.0
# (od_sec2.c od_sec2_command) answers CMD_AUTHENTICATE with [00][50][03] while unclaimed and
# [00][50][05] (AUTH_STATUS_V2_REQUIRED) once claimed, so connect() fails before a profile exists
# and the caller cannot learn that the device runs 3.0. Piece 4 must change this deliberately.


async def test_2x_key_on_an_unclaimed_3_0_device_fails_before_a_profile_exists(fake_transport) -> None:
    fake = fake_transport([b"\x00\x50\x03"])

    with pytest.raises(AuthenticationRequiredError):
        async with opendisplay.connect(transport=fake, encryption_key=bytes(16)):
            pytest.fail("entered")

    assert _opcodes(fake.written) == [b"\x00\x50"]  # no version read happened
    assert fake.is_connected is False


async def test_2x_key_on_a_claimed_3_0_device_fails_before_a_profile_exists(fake_transport) -> None:
    fake = fake_transport([b"\x00\x50\x05"])

    with pytest.raises(AuthenticationFailedError, match="0x05"):
        async with opendisplay.connect(transport=fake, encryption_key=bytes(16)):
            pytest.fail("entered")

    assert _opcodes(fake.written) == [b"\x00\x50"]
    assert fake.is_connected is False
