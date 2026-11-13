"""Tests for mutual authentication — verifying the device's server proof (M8)."""

from __future__ import annotations

import asyncio

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.crypto import _DEVICE_ID, compute_server_proof, derive_session_key
from opendisplay.exceptions import AuthenticationFailedError, ProtocolError
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme

_KEY = bytes(range(16))
_SERVER_NONCE = bytes(range(100, 116))
_CLIENT_NONCE = bytes(range(200, 216))


class _FakeConn:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses

    async def write_command(self, data: bytes, response: bool = True) -> None:
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


# ─── Stray step-1 challenge frames at step 2 (#141) ──────────────────────────

_CHALLENGE_FRAME = b"\x00\x50\x00" + _SERVER_NONCE + _DEVICE_ID  # 23-byte step-1 reply


def _device_with_responses(responses: list[bytes]) -> OpenDisplayDevice:
    dev = OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        capabilities=DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO),
    )
    dev._connection = _FakeConn(responses)  # type: ignore[assignment]
    return dev


def test_authenticate_skips_a_duplicated_challenge_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A challenge delivered twice must not be read as the step-2 response.

    Both frames carry status 0x00, so the duplicate used to parse as "success" with
    the server nonce as the proof, failing with a misleading proof mismatch.
    """
    monkeypatch.setattr("opendisplay.device.generate_client_nonce", lambda: _CLIENT_NONCE)
    success = b"\x00\x50\x00" + _good_proof()
    device = _device_with_responses([_CHALLENGE_FRAME, _CHALLENGE_FRAME, success])

    asyncio.run(device.authenticate(_KEY))

    assert device._session_key is not None


def test_authenticate_gives_up_on_a_stream_of_challenge_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opendisplay.device.generate_client_nonce", lambda: _CLIENT_NONCE)
    strays = OpenDisplayDevice.MAX_STRAY_AUTH_FRAMES + 1
    device = _device_with_responses([_CHALLENGE_FRAME] * (1 + strays))

    with pytest.raises(ProtocolError, match="challenge"):
        asyncio.run(device.authenticate(_KEY))

    assert device._session_key is None
