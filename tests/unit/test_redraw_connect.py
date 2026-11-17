"""Connect waits for a display noted as redrawing; without a note it fails as fast as before, with a hint."""

from __future__ import annotations

from typing import Any

import pytest

import opendisplay
from opendisplay import OpenDisplayDevice, redraw
from opendisplay.exceptions import BLEConnectionError, OpenDisplayTimeoutError

NAME = "OD4064C9"
ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(_: float) -> None:
        return None

    monkeypatch.setattr(redraw, "_sleep", sleep)


def _scans(monkeypatch: pytest.MonkeyPatch, found_on: int | None) -> list[float]:
    """Patch discovery: the display shows up on scan number ``found_on`` (1-based), or never."""
    calls: list[float] = []

    async def discover_devices(timeout: float = 10.0, **_: Any) -> dict[str, str]:
        calls.append(timeout)
        return {NAME: ADDRESS} if found_on is not None and len(calls) >= found_on else {"OD58D951": "11:22"}

    monkeypatch.setattr("opendisplay.discovery.discover_devices", discover_devices)
    return calls


async def _resolve(device: OpenDisplayDevice) -> None:
    await device._resolve_name()


async def test_resolution_keeps_scanning_while_a_note_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scans(monkeypatch, found_on=3)
    redraw.mark_redrawing(NAME)
    device = OpenDisplayDevice(device_name=NAME)

    await _resolve(device)

    assert device.mac_address == ADDRESS
    assert len(calls) == 3


async def test_resolution_without_a_note_fails_after_one_scan_with_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scans(monkeypatch, found_on=None)
    device = OpenDisplayDevice(device_name=NAME)

    with pytest.raises(BLEConnectionError) as err:
        await _resolve(device)

    assert redraw.REDRAW_HINT in str(err.value)
    assert len(calls) == 1


async def test_resolution_gives_up_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    t = [0.0]
    monkeypatch.setattr(redraw, "_clock", lambda: t[0])
    calls = _scans(monkeypatch, found_on=None)

    async def discover_and_tick(timeout: float = 10.0, **kw: Any) -> dict[str, str]:
        t[0] += timeout
        calls.append(timeout)
        return {}

    monkeypatch.setattr("opendisplay.discovery.discover_devices", discover_and_tick)
    redraw.mark_redrawing(NAME, allowance=25)

    with pytest.raises(BLEConnectionError, match="not found"):
        await _resolve(OpenDisplayDevice(device_name=NAME, discovery_timeout=10))

    assert calls == [10, 10, 5]  # never scans past the deadline


async def test_resolution_links_the_name_to_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _scans(monkeypatch, found_on=1)
    redraw.mark_redrawing(NAME)

    await _resolve(OpenDisplayDevice(device_name=NAME))

    assert redraw.remaining(ADDRESS) > 0


class _FlakyTransport:
    """Connect fails ``failures`` times with BLEConnectionError, then succeeds."""

    max_frame = 244
    supports_write_without_response = True
    mac_address = ADDRESS

    def __init__(self, failures: int) -> None:
        self.failures, self.attempts, self._connected = failures, 0, False

    async def connect(self) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise BLEConnectionError("not advertising")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        return None

    async def read_response(self, timeout: float = 5.0) -> bytes:
        raise OpenDisplayTimeoutError("silent")

    def drain_notifications(self) -> int:
        return 0

    async def clear_cache(self) -> bool:
        return False


async def test_ble_connect_is_retried_while_a_note_is_pending() -> None:
    transport = _FlakyTransport(failures=2)
    redraw.mark_redrawing(ADDRESS)

    async with OpenDisplayDevice(transport=transport, auto_interrogate=False):  # type: ignore[arg-type]
        assert transport.is_connected

    assert transport.attempts == 3


async def test_ble_connect_without_a_note_fails_at_once_with_the_hint() -> None:
    transport = _FlakyTransport(failures=1)

    with pytest.raises(BLEConnectionError) as err:
        async with OpenDisplayDevice(transport=transport, auto_interrogate=False):  # type: ignore[arg-type]
            pytest.fail("entered")

    assert transport.attempts == 1
    assert redraw.REDRAW_HINT in str(err.value)


async def test_a_silent_first_command_without_a_note_carries_the_hint(fake_transport) -> None:
    with pytest.raises(OpenDisplayTimeoutError) as err:
        async with opendisplay.connect(transport=fake_transport([OpenDisplayTimeoutError("silent")])):
            pytest.fail("entered")

    assert redraw.REDRAW_HINT in str(err.value)
