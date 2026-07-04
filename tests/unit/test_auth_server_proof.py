"""Tests for mutual authentication — verifying the device's server proof (M8)."""

from __future__ import annotations

import asyncio

import pytest
from epaper_dithering import ColorScheme

from opendisplay import OpenDisplayDevice
from opendisplay.crypto import _DEVICE_ID, compute_server_proof, derive_session_key
from opendisplay.exceptions import AuthenticationFailedError
from opendisplay.models.capabilities import DeviceCapabilities

_KEY = bytes(range(16))
_SERVER_NONCE = bytes(range(100, 116))
_CLIENT_NONCE = bytes(range(200, 216))


class _FakeConn:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses

    async def write_command(self, data: bytes) -> None:
        pass

    async def read_response(self, timeout: float) -> bytes:
        return self._responses.pop(0)


def _device(success_proof: bytes) -> OpenDisplayDevice:
    dev = OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        capabilities=DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO),
    )
    challenge = b"\x00\x50\x00" + _SERVER_NONCE  # old format -> default device_id
    success = b"\x00\x50\x00" + success_proof  # status OK + 16-byte proof
    dev._connection = _FakeConn([challenge, success])  # type: ignore[assignment]
    return dev


def _good_proof() -> bytes:
    session_key = derive_session_key(_KEY, _CLIENT_NONCE, _SERVER_NONCE, _DEVICE_ID)
    return compute_server_proof(session_key, _SERVER_NONCE, _CLIENT_NONCE, _DEVICE_ID)


def test_authenticate_accepts_valid_server_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opendisplay.device.generate_client_nonce", lambda: _CLIENT_NONCE)
    device = _device(_good_proof())

    asyncio.run(device.authenticate(_KEY))

    assert device._session_key is not None


def test_authenticate_rejects_wrong_server_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opendisplay.device.generate_client_nonce", lambda: _CLIENT_NONCE)
    device = _device(b"\xff" * 16)  # bogus proof a MITM would send

    with pytest.raises(AuthenticationFailedError, match="mutual authentication"):
        asyncio.run(device.authenticate(_KEY))

    assert device._session_key is None
