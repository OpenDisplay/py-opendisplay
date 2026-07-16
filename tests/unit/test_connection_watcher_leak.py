"""Regression tests for BlueZ notification-watcher leaks.

A watcher is registered by ``start_notify`` and only released when the client is
disconnected (or ``stop_notify`` is called). Every code path that can reach a
connected+subscribed client MUST guarantee a disconnect, or a stale watcher
lingers and delivers stray/duplicate notifications to a later connection.

Three leak paths, one test each:
  * Path 1 — ``OpenDisplayDevice.__aenter__`` raises after ``connect()`` (e.g.
    interrogate times out, or an outer ``asyncio.timeout`` cancels it). Because
    ``__aexit__`` only runs when ``__aenter__`` returns, the connection must be
    torn down inside ``__aenter__`` itself.
  * Path 2 — ``_clear_cache_and_drop`` disconnect is flaky.
  * Path 3 — the normal ``disconnect`` disconnect is flaky.
Paths 2 & 3 release the watcher with an explicit ``stop_notify`` BEFORE the
(possibly failing) disconnect, so nulling the client afterwards can't strand it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError

from opendisplay import OpenDisplayDevice
from opendisplay.transport.connection import BLEConnection


def _connected(client: object) -> BLEConnection:
    conn = BLEConnection("AA:BB:CC:DD:EE:FF")
    conn._client = client  # type: ignore[assignment]
    conn._notification_characteristic = MagicMock()  # a live subscription
    return conn


# --- Path 1: __aenter__ must disconnect on any post-connect failure -----------


@pytest.mark.asyncio
async def test_aenter_disconnects_when_interrogate_raises() -> None:
    """A failure after connect() (here interrogate) must disconnect the link;
    otherwise __aexit__ never runs and the connection + watcher leak."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake_conn = MagicMock()
    fake_conn.connect = AsyncMock()
    fake_conn.disconnect = AsyncMock()

    class _Boom(Exception):
        pass

    with patch("opendisplay.device.BLEConnection", return_value=fake_conn):
        device.interrogate = AsyncMock(side_effect=_Boom("no response"))  # type: ignore[method-assign]
        with pytest.raises(_Boom):
            await device.__aenter__()

    fake_conn.disconnect.assert_awaited_once()  # link torn down on the failure path


@pytest.mark.asyncio
async def test_aenter_disconnects_on_cancellation() -> None:
    """The real-world trigger: an outer asyncio.timeout() cancels the probe while
    interrogate is in flight. CancelledError is a BaseException, so the guard must
    catch BaseException — a plain 'except Exception' would leak here."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake_conn = MagicMock()
    fake_conn.connect = AsyncMock()
    fake_conn.disconnect = AsyncMock()

    with patch("opendisplay.device.BLEConnection", return_value=fake_conn):
        device.interrogate = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await device.__aenter__()

    fake_conn.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_aenter_success_does_not_disconnect() -> None:
    """The happy path must NOT disconnect — __aexit__ owns teardown there."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake_conn = MagicMock()
    fake_conn.connect = AsyncMock()
    fake_conn.disconnect = AsyncMock()

    with patch("opendisplay.device.BLEConnection", return_value=fake_conn):
        device.interrogate = AsyncMock()  # type: ignore[method-assign]
        result = await device.__aenter__()

    assert result is device
    fake_conn.disconnect.assert_not_awaited()


# --- Path 3: disconnect() releases the watcher before disconnecting -----------


@pytest.mark.asyncio
async def test_disconnect_stops_notifications_first() -> None:
    """disconnect() stops notifications before the ACL disconnect and clears state."""
    client = AsyncMock(is_connected=True)
    conn = _connected(client)
    char = conn._notification_characteristic

    await conn.disconnect()

    client.stop_notify.assert_awaited_once_with(char)
    client.disconnect.assert_awaited_once()
    assert conn._client is None
    assert conn._notification_characteristic is None


@pytest.mark.asyncio
async def test_disconnect_releases_watcher_even_if_disconnect_raises() -> None:
    """A disconnect that raises must not strand the watcher: stop_notify already
    released it, and disconnect() swallows the error rather than propagating."""
    client = AsyncMock(is_connected=True)
    client.disconnect = AsyncMock(side_effect=BleakError("link wedged"))
    conn = _connected(client)
    char = conn._notification_characteristic

    await conn.disconnect()  # must not raise

    client.stop_notify.assert_awaited_once_with(char)  # watcher released pre-disconnect
    assert conn._client is None


# --- Path 2: _clear_cache_and_drop releases the watcher before disconnecting ---


@pytest.mark.asyncio
async def test_clear_cache_and_drop_stops_notifications_first() -> None:
    """The connect-retry drop path also releases the watcher before disconnecting."""
    client = AsyncMock(is_connected=True)
    client.clear_cache = AsyncMock()
    conn = _connected(client)
    char = conn._notification_characteristic

    await conn._clear_cache_and_drop()

    client.stop_notify.assert_awaited_once_with(char)
    client.disconnect.assert_awaited_once()
    assert conn._client is None
    assert conn._notification_characteristic is None
