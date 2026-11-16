"""READ_IDENT (0x0046): what a device says about itself, with and without the 3.0 tail.

The two full frames are captures from hardware: a reTerminal E1002 on Firmware_Unified 3.0.2
with no config stored, and an EFR32BG22 tag on an early config-driven build that answers
without the tail.
"""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.device.channel import LegacySession
from opendisplay.exceptions import BLETimeoutError, IdentNotSupportedError, InvalidResponseError
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme
from opendisplay.models.ident import FirmwareTarget, IdentDisplay
from opendisplay.protocol.responses import parse_read_ident

V3_NO_DISPLAY = bytes.fromhex("004604010029a00f800003")
V2_ONE_DISPLAY = bytes.fromhex("00460303011c00b800680103")


def test_parses_a_3_0_reply_without_displays() -> None:
    ident = parse_read_ident(V3_NO_DISPLAY)

    assert (ident.proto_minor, ident.fw_target, ident.displays) == (4, FirmwareTarget.ESP32, ())
    assert (ident.flags, ident.max_config, ident.max_seq_steps, ident.escapes) == (0x29, 4000, 128, 0x03)
    assert (ident.config_driven, ident.needs_migration, ident.builtin_driver) == (True, False, False)
    assert (ident.supports_window, ident.supports_fill) == (True, True)
    assert ident.extra == b""


def test_parses_a_reply_without_a_tail_as_2x() -> None:
    ident = parse_read_ident(V2_ONE_DISPLAY)

    assert ident.fw_target is FirmwareTarget.EFR32BG22
    assert ident.displays == (IdentDisplay(panel_ic_type=0x001C, pixel_width=184, pixel_height=360, color_scheme=3),)
    assert ident.has_tail is False
    assert (ident.flags, ident.config_driven, ident.needs_migration, ident.max_config) == (None, None, None, None)


def test_needs_migration_flag() -> None:
    assert parse_read_ident(bytes.fromhex("00460401000ba00f800003")).needs_migration is True


def test_two_displays_then_tail() -> None:
    frame = bytes.fromhex("004604050223002003e0010527009001c8000001a00f800000")

    ident = parse_read_ident(frame)

    assert ident.fw_target is FirmwareTarget.NRF52840
    assert [d.pixel_width for d in ident.displays] == [800, 400]
    assert (ident.max_config, ident.supports_window) == (4000, False)


def test_unknown_target_is_kept_as_int() -> None:
    assert parse_read_ident(bytes.fromhex("0046047f00")).fw_target == 0x7F


def test_bytes_after_the_tail_are_kept() -> None:
    ident = parse_read_ident(V3_NO_DISPLAY + b"\xaa\xbb")

    assert (ident.max_config, ident.extra) == (4000, b"\xaa\xbb")


@pytest.mark.parametrize(
    "frame",
    [
        bytes.fromhex("0046"),  # too short for the header
        bytes.fromhex("00460303011c00b8006801"),  # record cut short
        bytes.fromhex("004604010029a00f"),  # tail cut short
        bytes.fromhex("004304010029a00f800003"),  # wrong echo
    ],
)
def test_malformed_reply_raises_invalid_response(frame: bytes) -> None:
    with pytest.raises(InvalidResponseError):
        parse_read_ident(frame)


def _device() -> OpenDisplayDevice:
    caps = DeviceCapabilities(width=800, height=480, color_scheme=ColorScheme.BWGBRY)
    return OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=caps)


async def test_read_ident_sends_plaintext_0046_and_returns_the_ident(fake_transport) -> None:
    fake = fake_transport([V3_NO_DISPLAY])
    device = _device()
    device._attach(fake)
    device._channel.session = LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8)

    ident = await device.read_ident()

    assert fake.written == [b"\x00\x46"]  # not sealed, even inside a session
    assert ident.max_config == 4000
    assert fake.timeouts == [OpenDisplayDevice.TIMEOUT_IDENT]


@pytest.mark.parametrize("reply", [BLETimeoutError, bytes.fromhex("0046fe"), bytes.fromhex("ff46")])
async def test_read_ident_on_firmware_without_it_raises_not_supported(fake_transport, reply) -> None:
    device = _device()
    device._attach(fake_transport([reply]))

    with pytest.raises(IdentNotSupportedError):
        await device.read_ident()


@pytest.mark.parametrize(("reply", "timed_out"), [(BLETimeoutError, True), (bytes.fromhex("0046fe"), False)])
async def test_not_supported_says_whether_the_device_was_silent(fake_transport, reply, timed_out: bool) -> None:
    """Silence can also be a slow link, so a caller deciding "this is 2.x" must be able to tell it
    from a device that actively refused the command."""
    device = _device()
    device._attach(fake_transport([reply]))

    with pytest.raises(IdentNotSupportedError) as exc:
        await device.read_ident()

    assert exc.value.timed_out is timed_out
