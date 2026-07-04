"""Tests for graceful handling of device error frames (§4 minor)."""

from __future__ import annotations

import asyncio

import pytest
from epaper_dithering import ColorScheme

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import InvalidResponseError, ProtocolError
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.protocol.responses import check_response_type, unpack_command_code


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
