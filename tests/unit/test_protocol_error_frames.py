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
    device._connection = _FakeConn([b"\xff\x40\x00\x00"])  # type: ignore[assignment]

    with pytest.raises(ProtocolError, match="no stored configuration"):
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
    device._connection = _ScriptedConn([_FIRST_CHUNK, BLETimeoutError()])  # type: ignore[assignment]

    with pytest.raises(TruncatedConfigError, match="truncated"):
        asyncio.run(device.interrogate())


def test_interrogate_raises_on_stalled_empty_chunk() -> None:
    """An empty chunk (no progress) raises TruncatedConfigError instead of looping forever."""
    device = _make_device()
    # Second chunk carries only the echo + chunk number, no payload -> no progress.
    empty_chunk = b"\x00\x40" + b"\x00\x01"
    device._connection = _ScriptedConn([_FIRST_CHUNK, empty_chunk])  # type: ignore[assignment]

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
    real_chunk = b"\x00\x40" + b"\x00\x01" + b"\x02" * 90
    device._connection = _ScriptedConn([_FIRST_CHUNK, stray, real_chunk])  # type: ignore[assignment]

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
    device._connection = _ScriptedConn([_FIRST_CHUNK, *strays])  # type: ignore[assignment]

    with pytest.raises(ProtocolError, match="frames from other commands"):
        asyncio.run(device.interrogate())


def test_interrogate_still_reports_error_frame_after_a_stray() -> None:
    """The no-config NACK answers this command, so it is passed through to the
    caller rather than skipped as a foreign frame."""
    device = _make_device()
    stray = b"\x00\x43\x01\x00\x28"
    device._connection = _ScriptedConn([stray, b"\xff\x40\x00\x00"])  # type: ignore[assignment]

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
