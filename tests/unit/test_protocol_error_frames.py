"""Tests for graceful handling of device error frames (§4 minor)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import (
    BLETimeoutError,
    ConfigParseError,
    InvalidResponseError,
    NoStoredConfigError,
    ProtocolError,
    TruncatedConfigError,
)
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme
from opendisplay.protocol.responses import (
    check_response_type,
    is_compressed_failure_frame,
    unpack_command_code,
)


def test_unpack_command_code_short_raises_invalid_response() -> None:
    with pytest.raises(InvalidResponseError):
        unpack_command_code(b"\x00")


def test_check_response_type_unknown_code_raises_invalid_response() -> None:
    # {0xFF, 0xFF} is the firmware compressed-failure frame; not a CommandCode.
    with pytest.raises(InvalidResponseError):
        check_response_type(b"\xff\xff")


class _FakeConn:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses

    async def write_command(self, data: bytes, response: bool = True) -> None:
        pass

    async def read_response(self, timeout: float) -> bytes:
        return self._responses.pop(0)


def test_interrogate_reports_no_config_error_frame() -> None:
    device = OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        capabilities=DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO),
    )
    device._attach(_FakeConn([b"\xff\x40\x00\x00"]))  # type: ignore[arg-type]

    with pytest.raises(NoStoredConfigError, match="no stored configuration"):
        asyncio.run(device.interrogate())


class _ScriptedConn:
    """Replays scripted config-read responses; a scripted exception is raised."""

    def __init__(self, responses: list[bytes | Exception]) -> None:
        self._responses = responses

    async def write_command(self, data: bytes, response: bool = True) -> None:
        pass

    async def read_response(self, timeout: float) -> bytes:
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_device() -> OpenDisplayDevice:
    return OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        capabilities=DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO),
    )


# First chunk: echo(0x0040) + chunkNum(0) + totalLen(100, little-endian) + 10 data bytes.
# total_length (100) exceeds the delivered payload, so more chunks are expected.
_FIRST_CHUNK = b"\x00\x40" + b"\x00\x00" + (100).to_bytes(2, "little") + b"\x01" * 10


def test_interrogate_raises_on_config_read_timeout() -> None:
    """A chunk read timing out mid-transfer raises TruncatedConfigError, not a hang."""
    device = _make_device()
    device._attach(_ScriptedConn([_FIRST_CHUNK, BLETimeoutError()]))  # type: ignore[arg-type]

    with pytest.raises(TruncatedConfigError, match="truncated"):
        asyncio.run(device.interrogate())


def test_interrogate_raises_on_stalled_empty_chunk() -> None:
    """An empty chunk (no progress) raises TruncatedConfigError instead of looping forever."""
    device = _make_device()
    # Second chunk carries only the echo + chunk number, no payload -> no progress.
    empty_chunk = b"\x00\x40" + b"\x01\x00"
    device._attach(_ScriptedConn([_FIRST_CHUNK, empty_chunk]))  # type: ignore[arg-type]

    with pytest.raises(TruncatedConfigError, match="stalled"):
        asyncio.run(device.interrogate())


def test_interrogate_ignores_stray_frame_from_another_command(caplog: pytest.LogCaptureFixture) -> None:
    """A frame belonging to a different exchange must not be spliced into the
    config: strip_command_echo returns a non-matching frame unchanged, so its
    echo would be eaten as the chunk-number field and its body appended as data.
    """
    device = _make_device()
    # 90 payload bytes still owed after the first chunk's 10.
    stray = b"\x00\x43\x01\x00\x28" + b"\xaa" * 20  # firmware-version reply, wrong exchange
    real_chunk = b"\x00\x40" + b"\x01\x00" + b"\x02" * 90
    device._attach(_ScriptedConn([_FIRST_CHUNK, stray, real_chunk]))  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="opendisplay.device"), pytest.raises(ConfigParseError):
        asyncio.run(device.interrogate())

    # Reaching the parser at all proves total_length was satisfied by config
    # chunks alone: the stray was consumed and dropped rather than appended.
    assert "Ignoring stray READ_FW_VERSION (0x0043) frame" in caplog.text
    assert device._connection._responses == []  # type: ignore[attr-defined]


def test_interrogate_gives_up_after_too_many_stray_frames() -> None:
    """Foreign frames are bounded, so a device streaming unrelated frames fails
    with a clear error rather than renewing the read timeout indefinitely."""
    device = _make_device()
    stray = b"\x00\x43\x01\x00\x28"
    strays: list[bytes | Exception] = [stray] * (OpenDisplayDevice.MAX_STRAY_CONFIG_FRAMES + 1)
    device._attach(_ScriptedConn([_FIRST_CHUNK, *strays]))  # type: ignore[arg-type]

    with pytest.raises(ProtocolError, match="frames from other commands"):
        asyncio.run(device.interrogate())


def test_interrogate_still_reports_error_frame_after_a_stray() -> None:
    """The no-config NACK answers this command, so it is passed through to the
    caller rather than skipped as a foreign frame."""
    device = _make_device()
    stray = b"\x00\x43\x01\x00\x28"
    device._attach(_ScriptedConn([stray, b"\xff\x40\x00\x00"]))  # type: ignore[arg-type]

    with pytest.raises(ProtocolError, match="no stored configuration"):
        asyncio.run(device.interrogate())


def test_is_compressed_failure_frame_accepts_both_forms() -> None:
    """Both the legacy {0xFF,0xFF} and spec-conformant {0xFF,0x70} count as failures."""
    assert is_compressed_failure_frame(b"\xff\xff") is True
    assert is_compressed_failure_frame(b"\xff\x70") is True
    # Not a failure frame: valid ACK, wrong prefix, or wrong length.
    assert is_compressed_failure_frame(b"\x00\x70") is False
    assert is_compressed_failure_frame(b"\xff\x40") is False
    assert is_compressed_failure_frame(b"\xff") is False


# A 30-byte config in three 10-byte chunks, as the firmware numbers them (0, 1, 2).
_C0 = b"\x00\x40" + b"\x00\x00" + (30).to_bytes(2, "little") + bytes(range(10))
_C1 = b"\x00\x40" + (1).to_bytes(2, "little") + bytes(range(10, 20))
_C2 = b"\x00\x40" + (2).to_bytes(2, "little") + bytes(range(20, 30))
_VERSION = b"\x00\x43\x02\x1b\x07abcdefg"


@pytest.mark.parametrize("frames", [[_C0, _C1, _C1, _C2], [_C0, _C0, _C1, _C2], [_C0, _C1, _C2]])
def test_a_duplicated_config_chunk_is_dropped(frames: list[bytes], caplog: pytest.LogCaptureFixture) -> None:
    """A Bluetooth stack (an ESPHome proxy, for one) can deliver a notification twice. Taken as the
    next chunk, the duplicate corrupted the config and left the real last chunk for the next
    command (HA integration #177: 'expected 0x0043, got 0x0040')."""
    device = _make_device()
    device._attach(_ScriptedConn([*frames, _VERSION]))  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="opendisplay.device"):
        data = asyncio.run(device._read_config_bytes())
        version = asyncio.run(device.read_firmware_version())

    assert data == bytes(range(30))
    assert version["major"] == 2
    assert ("delivered twice" in caplog.text) == (len(frames) == 4)


def test_a_config_chunk_out_of_order_fails_the_read() -> None:
    device = _make_device()
    device._attach(_ScriptedConn([_C0, _C2, _C1]))  # type: ignore[arg-type]

    with pytest.raises(ProtocolError, match="expected chunk 1, got chunk 2"):
        asyncio.run(device._read_config_bytes())


def test_a_repeated_chunk_number_with_other_bytes_fails_the_read() -> None:
    device = _make_device()
    other = b"\x00\x40" + (1).to_bytes(2, "little") + bytes(10)
    device._attach(_ScriptedConn([_C0, _C1, other, _C2]))  # type: ignore[arg-type]

    with pytest.raises(ProtocolError, match="expected chunk 2, got chunk 1"):
        asyncio.run(device._read_config_bytes())


def test_endless_repeats_of_a_chunk_give_up() -> None:
    device = _make_device()
    device._attach(_ScriptedConn([_C0] + [_C1] * (OpenDisplayDevice.MAX_STRAY_CONFIG_FRAMES + 2)))  # type: ignore[arg-type]

    with pytest.raises(ProtocolError, match="delivered twice"):
        asyncio.run(device._read_config_bytes())
