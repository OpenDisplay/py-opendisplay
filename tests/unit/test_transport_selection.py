"""Transport selection + TCP auth-gating in OpenDisplayDevice.

Covers the __init__ mutual-exclusion rules and the __aenter__ selection order
(explicit transport > host > BLE), plus the critical no-double-encrypt gate:
over TCP the app-layer AUTHENTICATE (0x0050) must never run.
"""

from __future__ import annotations

import pytest

import opendisplay.device as device_mod
from opendisplay import OpenDisplayDevice
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme
from opendisplay.transport.ip import TcpTransport


class _SpyTcp(TcpTransport):
    """Real TcpTransport subclass with an in-memory link (so isinstance holds)."""

    def __init__(self, host: str, port: int, *, timeout: float = 10.0, tls: bool = False, psk: bytes | None = None):
        super().__init__(host, port, timeout=timeout, tls=tls, psk=psk)
        self.connected = False
        self.written: list[bytes] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)


class _SpyBle:
    """Minimal BLE-connection stand-in (records construction + writes)."""

    max_frame = 244
    supports_write_without_response = False
    device_name = "ble"

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.connected = False
        self.written: list[bytes] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)


def _caps() -> DeviceCapabilities:
    return DeviceCapabilities(width=8, height=8, color_scheme=ColorScheme.MONO)


# ── __init__ mutual exclusion ────────────────────────────────────────────────


def test_requires_an_addressing_mode() -> None:
    with pytest.raises(ValueError):
        OpenDisplayDevice()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mac_address": "AA:BB:CC:DD:EE:FF", "host": "1.2.3.4"},
        {"mac_address": "AA:BB:CC:DD:EE:FF", "transport": object()},
        {"host": "1.2.3.4", "transport": object()},
        {"device_name": "tag", "host": "1.2.3.4"},
    ],
)
def test_combined_addressing_modes_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        OpenDisplayDevice(**kwargs)


def test_mac_and_name_still_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", device_name="tag")


# ── __aenter__ selection order ───────────────────────────────────────────────


async def test_host_builds_tcp_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_mod, "TcpTransport", _SpyTcp)
    dev = OpenDisplayDevice(host="10.0.0.5", port=2447, tls=True, psk=b"k", capabilities=_caps())
    async with dev as connected:
        conn = connected._conn
        assert isinstance(conn, _SpyTcp)
        assert conn.host == "10.0.0.5"
        assert conn.port == 2447
        assert conn.tls is True


async def test_mac_builds_ble_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_mod, "BLEConnection", _SpyBle)
    dev = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=_caps())
    async with dev as connected:
        assert isinstance(connected._conn, _SpyBle)


async def test_explicit_transport_wins(fake_transport) -> None:
    fake = fake_transport()
    dev = OpenDisplayDevice(transport=fake, capabilities=_caps())
    async with dev as connected:
        assert connected._conn is fake
        assert fake.is_connected


def test_default_port_is_2446() -> None:
    dev = OpenDisplayDevice(host="1.2.3.4")
    assert dev._port == 2446


# ── no-double-encrypt gate ───────────────────────────────────────────────────


async def test_tcp_with_key_does_not_authenticate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_mod, "TcpTransport", _SpyTcp)
    calls: list[bytes] = []

    async def _spy_auth(self: OpenDisplayDevice, key: bytes) -> None:
        calls.append(key)

    monkeypatch.setattr(OpenDisplayDevice, "authenticate", _spy_auth)
    dev = OpenDisplayDevice(host="10.0.0.5", encryption_key=b"0123456789abcdef", capabilities=_caps())
    async with dev as connected:
        conn = connected._conn
        assert isinstance(conn, _SpyTcp)
        # The app-layer challenge/response must not run over TCP (TLS gates it).
        assert calls == []
        assert all(w[:2] != b"\x00\x50" for w in conn.written)
        assert connected._session_key is None


async def test_ble_with_key_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: the BLE path DOES invoke authenticate for the same key."""
    monkeypatch.setattr(device_mod, "BLEConnection", _SpyBle)
    calls: list[bytes] = []

    async def _spy_auth(self: OpenDisplayDevice, key: bytes) -> None:
        calls.append(key)

    monkeypatch.setattr(OpenDisplayDevice, "authenticate", _spy_auth)
    dev = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", encryption_key=b"0123456789abcdef", capabilities=_caps())
    async with dev:
        assert calls == [b"0123456789abcdef"]
