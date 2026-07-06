"""Tests for graceful handling of device error frames (§4 minor)."""

from __future__ import annotations

import asyncio

import pytest
from epaper_dithering import ColorScheme

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import (
    BLETimeoutError,
    InvalidResponseError,
    ProtocolError,
    TruncatedConfigError,
)
from opendisplay.models.capabilities import DeviceCapabilities
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

    async def write_command(self, data: bytes) -> None:
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

    async def write_command(self, data: bytes) -> None:
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


def test_is_compressed_failure_frame_accepts_both_forms() -> None:
    """Both the legacy {0xFF,0xFF} and spec-conformant {0xFF,0x70} count as failures."""
    assert is_compressed_failure_frame(b"\xff\xff") is True
    assert is_compressed_failure_frame(b"\xff\x70") is True
    # Not a failure frame: valid ACK, wrong prefix, or wrong length.
    assert is_compressed_failure_frame(b"\x00\x70") is False
    assert is_compressed_failure_frame(b"\xff\x40") is False
    assert is_compressed_failure_frame(b"\xff") is False
