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


def test_the_security_bits_of_the_tail() -> None:
    # Web od-v3.js parseReadIdent: bit3 SEC_V2, bit4 CLAIMED, bit5 CLAIM_MODE, bit6 LEGACY_SECRETS_OPEN.
    virgin = parse_read_ident(V3_NO_DISPLAY)  # flags 0x29: the E1002 just flashed, in claim mode
    assert (virgin.sec2, virgin.claimed, virgin.claim_mode, virgin.legacy_secrets_open) == (True, False, True, False)

    claimed = parse_read_ident(bytes.fromhex("004604010019a00f800003"))  # 0x19
    assert (claimed.sec2, claimed.claimed, claimed.claim_mode) == (True, True, False)

    secrets = parse_read_ident(bytes.fromhex("004604010049a00f800003"))  # 0x49
    assert secrets.legacy_secrets_open is True

    old = parse_read_ident(V2_ONE_DISPLAY)
    assert (old.sec2, old.claimed, old.claim_mode, old.legacy_secrets_open) == (None, None, None, None)


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


# Captured 2026-11-26 from the E1002 running a 3.0.3 test build (Firmware_Unified
# fix/esp32-firmware-version 63701e1, a release-workflow dry run, not a published release). It
# predates the board field (#107): one display record, the tail, and nothing after it.
E1002_303 = bytes.fromhex("004604010123002003e0010409a00f800003")


def test_firmware_before_the_board_field_reports_no_board() -> None:
    ident = parse_read_ident(E1002_303)

    assert ident.board is None and ident.extra == b""


# Captured 2026-11-26 from the EE02 (OD688CB0) on the published 3.1.1 release: protocol minor 5,
# ESP32, one display (0x0042, 1200x1600, scheme 8), the tail, then [8]"s3-n16r8".
EE02_311 = bytes.fromhex("00460501014200b00440060809a00f8000030873332d6e31367238")


def test_parses_the_release_board_a_3_1_device_reports() -> None:
    """Firmware_Unified #107: [board_len][board] after escapes, the manifest's boards[].board."""
    ident = parse_read_ident(EE02_311)

    assert ident.board == "s3-n16r8"
    assert ident.extra == b""
    assert (ident.proto_minor, ident.fw_target) == (5, FirmwareTarget.ESP32)
    assert ident.displays == (IdentDisplay(panel_ic_type=0x0042, pixel_width=1200, pixel_height=1600, color_scheme=8),)


# Captured 2026-11-26 from the E1002 (OD4064C9) right after `opendisplay update` took it from the
# 3.0.3 test build to the published 3.1.1: one display (0x0023, 800x480, scheme 4), the tail, then
# [16]"s3-n32r8-extuart".
E1002_311 = bytes.fromhex("004605010123002003e0010409a00f8000031073332d6e333272382d65787475617274")


def test_parses_a_longer_release_board() -> None:
    ident = parse_read_ident(E1002_311)

    assert ident.board == "s3-n32r8-extuart" and ident.extra == b""
    assert ident.displays == (IdentDisplay(panel_ic_type=0x0023, pixel_width=800, pixel_height=480, color_scheme=4),)


# The edge cases below alter the EE02's capture after its tail (no firmware sends them).
_EE02_TO_TAIL = EE02_311[: -len(b"\x08s3-n16r8")]


def test_board_len_zero_means_not_a_release_board() -> None:
    assert parse_read_ident(_EE02_TO_TAIL + b"\x00").board is None


def test_bytes_after_the_board_are_kept() -> None:
    ident = parse_read_ident(EE02_311 + b"\xaa\xbb")

    assert ident.board == "s3-n16r8" and ident.extra == b"\xaa\xbb"


@pytest.mark.parametrize("tail", [b"\x08s3-n16", b"\x04BG22", b"\x03b g", b"\x21" + b"a" * 33])
def test_a_board_field_that_breaks_the_rules_is_ignored(tail: bytes) -> None:
    """Cut short, outside [a-z0-9_-] or over 32 characters: no board, and the bytes stay in extra."""
    ident = parse_read_ident(_EE02_TO_TAIL + tail)

    assert ident.board is None and ident.extra == tail
