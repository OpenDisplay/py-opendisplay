"""SecureChannel and LegacySession: the seam between commands and the transport."""

from __future__ import annotations

import time

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.crypto import _DEVICE_ID, compute_server_proof, derive_session_key
from opendisplay.device.channel import LegacySession, SecureChannel

_KEY = bytes(range(16))
_SERVER_NONCE = bytes(range(100, 116))
_CLIENT_NONCE = bytes(range(200, 216))


def _session(**kwargs: object) -> LegacySession:
    return LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8, **kwargs)  # type: ignore[arg-type]


# ─── Plaintext ───────────────────────────────────────────────────────────────


async def test_plain_channel_passes_frames_through(fake_transport) -> None:
    channel = SecureChannel(fake_transport([b"\x00\x43"]))

    await channel.write(b"\x00\x43")

    assert channel.transport.written == [b"\x00\x43"]
    assert await channel.read(1.0) == b"\x00\x43"


def test_chunk_sizes_plain_ble(fake_transport) -> None:
    channel = SecureChannel(fake_transport(max_frame=244))

    assert channel.direct_chunk_size() == 230
    assert channel.pipe_data_size(244) == 241


def test_chunk_sizes_large_frame_transport(fake_transport) -> None:
    assert SecureChannel(fake_transport(max_frame=4094)).direct_chunk_size() == 4092


# ─── Legacy (2.x) session ────────────────────────────────────────────────────


def test_chunk_sizes_in_legacy_session(fake_transport) -> None:
    channel = SecureChannel(fake_transport(max_frame=244))
    channel.session = _session()

    assert channel.direct_chunk_size() == 154
    assert channel.pipe_data_size(244) == 212


def test_seal_advances_the_nonce_counter() -> None:
    session = _session()

    first = session.seal(b"\x00\x71abc")
    second = session.seal(b"\x00\x71abc")

    assert session.nonce_counter == 2
    assert first != second


async def test_write_in_session_sends_the_sealed_frame(fake_transport) -> None:
    channel = SecureChannel(fake_transport())
    channel.session = _session()

    await channel.write(b"\x00\x43")

    assert channel.transport.written == [_session().seal(b"\x00\x43")]


async def test_reset_drops_the_session(fake_transport) -> None:
    channel = SecureChannel(fake_transport())
    channel.session = _session()

    channel.reset()

    assert channel.session is None


async def test_reauth_runs_before_write_at_90_percent(fake_transport, monkeypatch: pytest.MonkeyPatch) -> None:
    channel = SecureChannel(fake_transport(), reauth_key=_KEY, session_timeout=lambda: 100)
    await _authenticate(channel, monkeypatch)
    channel.session.established_at = time.monotonic() - 91  # type: ignore[union-attr]
    calls: list[int] = []

    async def spy(key: bytes) -> None:
        calls.append(len(channel.transport.written))

    monkeypatch.setattr(channel, "authenticate", spy)
    written_before = len(channel.transport.written)

    await channel.write_stream_frame(b"\x00\x81\x00", response=False)
    assert calls == []  # never from a stream frame

    await channel.write(b"\x00\x43")
    assert calls == [written_before + 1]  # once, before the 0x0043 frame went out


async def test_no_reauth_when_timeout_is_zero(fake_transport, monkeypatch: pytest.MonkeyPatch) -> None:
    channel = SecureChannel(fake_transport(), reauth_key=_KEY, session_timeout=lambda: 0)
    await _authenticate(channel, monkeypatch)
    channel.session.established_at = time.monotonic() - 10_000  # type: ignore[union-attr]
    calls: list[bytes] = []

    async def spy(key: bytes) -> None:
        calls.append(key)

    monkeypatch.setattr(channel, "authenticate", spy)

    await channel.write(b"\x00\x43")

    assert calls == []


async def test_authenticate_restarts_the_nonce_counter(fake_transport, monkeypatch: pytest.MonkeyPatch) -> None:
    channel = SecureChannel(fake_transport())
    await _authenticate(channel, monkeypatch)
    channel.session.seal(b"\x00\x43")  # type: ignore[union-attr]

    await _authenticate(channel, monkeypatch)

    assert channel.session.nonce_counter == 0  # type: ignore[union-attr]


# ─── Through the device object ───────────────────────────────────────────────


async def test_command_before_connect_raises_not_connected() -> None:
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")

    with pytest.raises(RuntimeError, match="Device not connected"):
        await device.read_firmware_version()


def test_disconnect_callback_before_attach_does_not_raise() -> None:
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")

    device._on_disconnect()
    device._on_disconnect()


async def test_manual_authenticate_uses_encrypted_chunk_size(fake_transport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opendisplay.device.channel.generate_client_nonce", lambda: _CLIENT_NONCE)
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._attach(fake_transport(_handshake_frames()))
    assert device._channel.direct_chunk_size() == 230

    await device.authenticate(_KEY)

    assert device._channel.session is not None
    assert device._channel.direct_chunk_size() == 154


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _handshake_frames() -> list[bytes]:
    session_key = derive_session_key(_KEY, _CLIENT_NONCE, _SERVER_NONCE, _DEVICE_ID)
    proof = compute_server_proof(session_key, _SERVER_NONCE, _CLIENT_NONCE, _DEVICE_ID)
    return [b"\x00\x50\x00" + _SERVER_NONCE, b"\x00\x50\x00" + proof]


async def _authenticate(channel: SecureChannel, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a real 2.x handshake against the channel's fake transport."""
    monkeypatch.setattr("opendisplay.device.channel.generate_client_nonce", lambda: _CLIENT_NONCE)
    channel.transport._responses.extend(_handshake_frames())
    await channel.authenticate(_KEY)
