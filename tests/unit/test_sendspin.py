"""Tests for the Sendspin artwork bridge (optional ``sendspin`` extra)."""

from __future__ import annotations

import asyncio
import importlib
import io
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryClientPairingStore
from epaper_dithering import ColorScheme
from PIL import Image

from opendisplay.models.config import (
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.sendspin import (
    SendspinArtworkBridge,
    _ArtworkSink,
    _pick_address,
    _server_url,
    artwork_edge,
    load_or_create_identity,
)

URL = "ws://10.0.0.5:8927/sendspin"


def _display(width: int, height: int) -> DisplayConfig:
    return DisplayConfig(
        instance_number=0,
        display_technology=0,
        panel_ic_type=0,
        pixel_width=width,
        pixel_height=height,
        active_width_mm=10,
        active_height_mm=10,
        tag_type=0,
        rotation=0,
        reset_pin=0xFF,
        busy_pin=0xFF,
        dc_pin=0xFF,
        cs_pin=0xFF,
        data_pin=0,
        partial_update_support=0,
        color_scheme=ColorScheme.MONO.value,
        transmission_modes=0,
        clk_pin=0,
        reserved_pins=b"\x00" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )


def _config(*displays: DisplayConfig) -> GlobalConfig:
    return GlobalConfig(
        system=SystemConfig(
            ic_type=0,
            communication_modes=0,
            device_flags=0,
            pwr_pin=0xFF,
            reserved=b"\x00" * 17,
        ),
        manufacturer=ManufacturerData(
            manufacturer_id=0,
            board_type=0,
            board_revision=0,
            reserved=b"\x00" * 18,
        ),
        power=PowerOption(
            power_mode=0,
            battery_capacity_mah=b"\x00\x00\x00",
            sleep_timeout_ms=0,
            tx_power=0,
            sleep_flags=0,
            battery_sense_pin=0xFF,
            battery_sense_enable_pin=0xFF,
            battery_sense_flags=0,
            capacity_estimator=0,
            voltage_scaling_factor=0,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            charge_enable_pin=0xFF,
            charge_state_pin=0xFF,
            charger_flags=0,
            min_wake_time_seconds=0,
            screen_timeout_seconds=0,
            reserved=b"\x00" * 12,
        ),
        displays=list(displays),
    )


def _png(color: str, size: int = 4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


class _Recorder:
    """Artwork handler that records what it was given."""

    def __init__(self) -> None:
        self.calls: list[Image.Image | None] = []
        self.release = asyncio.Event()
        self.release.set()
        self.fail_next = False

    async def __call__(self, image: Image.Image | None) -> None:
        await self.release.wait()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("upload failed")
        self.calls.append(image)

    def colors(self) -> list[tuple[int, ...] | None]:
        return [None if im is None else im.convert("RGB").getpixel((0, 0)) for im in self.calls]  # type: ignore[misc]


# ── sizing ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("width", "height"), [(800, 480), (480, 800)])
def test_artwork_edge_is_shorter_side(width: int, height: int) -> None:
    assert artwork_edge(_config(_display(width, height))) == 480


def test_artwork_edge_uses_first_display() -> None:
    assert artwork_edge(_config(_display(296, 128), _display(800, 480))) == 128


def test_artwork_edge_without_display_raises() -> None:
    with pytest.raises(ValueError, match="no display"):
        artwork_edge(_config())


# ── import guard ──────────────────────────────────────────────────────────────


def test_import_without_extra_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [m for m in sys.modules if m == "aiosendspin" or m.startswith("aiosendspin.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "aiosendspin", None)
    monkeypatch.delitem(sys.modules, "opendisplay.sendspin")
    with pytest.raises(ImportError, match=r"py-opendisplay\[sendspin\]"):
        importlib.import_module("opendisplay.sendspin")


# ── identity ──────────────────────────────────────────────────────────────────


def test_identity_is_created_once_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "state" / "identity.key"
    first = load_or_create_identity(path)
    second = load_or_create_identity(path)
    assert first.peer_id == second.peer_id
    assert path.stat().st_mode & 0o777 == 0o600


# ── server URL ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "path", "expected"),
    [
        ("10.0.0.5", "/sendspin", "ws://10.0.0.5:8927/sendspin"),
        ("10.0.0.5", None, "ws://10.0.0.5:8927/sendspin"),
        ("10.0.0.5", "sendspin", "ws://10.0.0.5:8927/sendspin"),
        ("fd00::5", "/sendspin", "ws://[fd00::5]:8927/sendspin"),
    ],
)
def test_server_url(host: str, path: str | None, expected: str) -> None:
    assert _server_url(host, 8927, path) == expected


def test_pick_address_prefers_ipv4() -> None:
    assert _pick_address(["fd00::5", "10.0.0.5"]) == "10.0.0.5"
    assert _pick_address(["fd00::5"]) == "fd00::5"


# ── artwork sink ──────────────────────────────────────────────────────────────


async def test_sink_skips_identical_image() -> None:
    rec = _Recorder()
    sink = _ArtworkSink(rec)
    sink.feed(_png("red"))
    await sink.join()
    sink.feed(_png("red"))
    await sink.join()
    assert rec.colors() == [(255, 0, 0)]


async def test_sink_clear_does_not_reset_duplicate_check() -> None:
    rec = _Recorder()
    sink = _ArtworkSink(rec)
    for frame in (_png("red"), b"", _png("red")):
        sink.feed(frame)
        await sink.join()
    assert rec.colors() == [(255, 0, 0), None]


async def test_sink_delivers_only_latest_while_busy() -> None:
    rec = _Recorder()
    rec.release.clear()
    sink = _ArtworkSink(rec)
    sink.feed(_png("red"))
    await asyncio.sleep(0.05)  # red is now being handled and blocks
    sink.feed(_png("lime"))
    sink.feed(_png("blue"))
    rec.release.set()
    await sink.join()
    assert rec.colors() == [(255, 0, 0), (0, 0, 255)]


async def test_sink_survives_undecodable_frame() -> None:
    rec = _Recorder()
    sink = _ArtworkSink(rec)
    sink.feed(b"not a png")
    await sink.join()
    sink.feed(_png("red"))
    await sink.join()
    assert rec.colors() == [(255, 0, 0)]


async def test_sink_retries_image_after_handler_failure() -> None:
    rec = _Recorder()
    rec.fail_next = True
    sink = _ArtworkSink(rec)
    sink.feed(_png("red"))
    await sink.join()
    sink.feed(_png("red"))
    await sink.join()
    assert rec.colors() == [(255, 0, 0)]


async def test_sink_seeded_hash_skips_known_image() -> None:
    import hashlib

    rec = _Recorder()
    sink = _ArtworkSink(rec, last_hash=hashlib.sha256(_png("red")).hexdigest())
    sink.feed(_png("red"))
    await sink.join()
    assert rec.calls == []


# ── bridge ────────────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.url: str | None = None
        self.disconnected = False
        self._art: list[Callable[[int, bytes], None]] = []
        self._gone: list[Callable[[], None]] = []
        self._group: list[Callable[[object], None]] = []
        self.available: list[bool] = []

    def add_artwork_listener(self, callback: Callable[[int, bytes], None]) -> Callable[[], None]:
        self._art.append(callback)
        return lambda: None

    def add_disconnect_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._gone.append(callback)
        return lambda: None

    def add_group_update_listener(self, callback: Callable[[object], None]) -> Callable[[], None]:
        self._group.append(callback)
        return lambda: None

    async def send_available(self, *, available: bool) -> None:
        self.available.append(available)

    async def connect(self, url: str) -> None:
        if self.fail:
            raise ConnectionRefusedError("refused")
        self.url = url

    async def disconnect(self) -> None:
        self.disconnected = True
        self.drop()

    def send_art(self, data: bytes) -> None:
        for cb in self._art:
            cb(0, data)

    def group_update(self) -> None:
        for cb in self._group:
            cb(object())

    def drop(self) -> None:
        for cb in self._gone:
            cb()


class _Sleeps:
    """Records backoff delays; sets ``reached`` once ``limit`` sleeps happened."""

    def __init__(self, limit: int) -> None:
        self.delays: list[float] = []
        self.limit = limit
        self.reached = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.limit:
            self.reached.set()
            await asyncio.Event().wait()  # park until the bridge is stopped
        await asyncio.sleep(0)


def _bridge(
    clients: list[_FakeClient],
    sleeps: _Sleeps,
    handler: _Recorder | None = None,
    store: InMemoryClientPairingStore | None = None,
) -> SendspinArtworkBridge:
    made = iter(clients)
    return SendspinArtworkBridge(
        _config(_display(800, 480)),
        name="Kitchen",
        identity=Identity.generate(),
        pairing_store=store or InMemoryClientPairingStore(),
        on_artwork=handler or _Recorder(),
        url=URL,
        client_factory=lambda: next(made),
        sleep=sleeps,
    )


async def test_bridge_backs_off_exponentially_up_to_a_minute() -> None:
    sleeps = _Sleeps(limit=8)
    bridge = _bridge([_FakeClient(fail=True) for _ in range(8)], sleeps)
    await bridge.start()
    await asyncio.wait_for(sleeps.reached.wait(), 1)
    await bridge.stop()
    assert sleeps.delays == [1, 2, 4, 8, 16, 32, 60, 60]


async def test_bridge_reconnects_after_drop() -> None:
    sleeps = _Sleeps(limit=2)
    first, second = _FakeClient(), _FakeClient()
    bridge = _bridge([first, second, _FakeClient(fail=True)], sleeps)
    await bridge.start()
    await asyncio.sleep(0.05)
    assert first.url == URL
    first.drop()
    await asyncio.sleep(0.05)
    assert second.url == URL
    await bridge.stop()
    assert sleeps.delays[0] == 1


async def test_bridge_forwards_artwork() -> None:
    rec = _Recorder()
    client = _FakeClient()
    bridge = _bridge([client], _Sleeps(limit=1), handler=rec)
    await bridge.start()
    await asyncio.sleep(0.05)
    client.send_art(_png("red"))
    await asyncio.sleep(0.1)
    await bridge.stop()
    assert rec.colors() == [(255, 0, 0)]


async def test_bridge_stop_disconnects_and_is_idempotent() -> None:
    client = _FakeClient()
    bridge = _bridge([client], _Sleeps(limit=1))
    await bridge.start()
    await asyncio.sleep(0.05)
    await bridge.stop()
    await bridge.stop()
    assert client.disconnected


async def test_bridge_enables_unpaired_access() -> None:
    store = InMemoryClientPairingStore()
    bridge = _bridge([_FakeClient()], _Sleeps(limit=1), store=store)
    await bridge.start()
    await bridge.stop()
    assert (await store.get_pairing_config()).unpaired_access_enabled


async def test_bridge_reports_state_after_group_update() -> None:
    """aiosendspin 9.1.1 sends no client/state when only the artwork role is
    activated, so the server would hold artwork back forever."""
    client = _FakeClient()
    bridge = _bridge([client], _Sleeps(limit=1))
    await bridge.start()
    await asyncio.sleep(0.05)
    client.group_update()
    await asyncio.sleep(0.05)
    await bridge.stop()
    assert client.available == [True]


async def test_client_registers_as_display_in_music_assistant() -> None:
    """Music Assistant 2.10 turns a client without metadata@v1 into a speaker
    (player type PLAYER); with it the display gets the DISPLAY type and icon."""
    from aiosendspin.client import SendspinClient
    from aiosendspin.models.types import Roles

    bridge = _bridge([], _Sleeps(limit=1))
    client = bridge._default_client_factory()
    assert isinstance(client, SendspinClient)
    assert set(client.roles) == {Roles.ARTWORK, Roles.METADATA}
