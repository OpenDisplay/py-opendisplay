"""Tests for GATT cache clearing (BLEConnection.clear_cache + device.clear_gatt_cache).

clear_gatt_cache() is used on the Silabs OTA path to drop a Bluetooth proxy's
stale per-MAC GATT cache before triggering the bootloader, so the post-reboot
AppLoader connection re-discovers the OTA service.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import BLEConnectionError
from opendisplay.transport.connection import BLEConnection


def _connected(client: object) -> BLEConnection:
    conn = BLEConnection("AA:BB:CC:DD:EE:FF")
    conn._client = client
    return conn


@pytest.mark.asyncio
async def test_clear_cache_calls_backend_and_returns_result() -> None:
    """When the backend supports clear_cache, its result is returned."""
    client = MagicMock(is_connected=True)
    client.clear_cache = AsyncMock(return_value=True)
    conn = _connected(client)

    assert await conn.clear_cache() is True
    client.clear_cache.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_clear_cache_propagates_false() -> None:
    """A backend that clears only its in-memory cache returns False through us."""
    client = MagicMock(is_connected=True)
    client.clear_cache = AsyncMock(return_value=False)
    conn = _connected(client)

    assert await conn.clear_cache() is False


@pytest.mark.asyncio
async def test_clear_cache_no_backend_support_returns_false() -> None:
    """Direct BlueZ on a bleak build without clear_cache: graceful False, no raise."""
    client = MagicMock(is_connected=True, spec=["is_connected"])  # no clear_cache attr
    conn = _connected(client)

    assert await conn.clear_cache() is False


@pytest.mark.asyncio
async def test_clear_cache_not_connected_raises() -> None:
    """clear_cache requires an active connection."""
    conn = BLEConnection("AA:BB:CC:DD:EE:FF")  # _client is None
    with pytest.raises(BLEConnectionError, match="Not connected"):
        await conn.clear_cache()

    client = MagicMock(is_connected=False)
    conn2 = _connected(client)
    with pytest.raises(BLEConnectionError, match="Not connected"):
        await conn2.clear_cache()


@pytest.mark.asyncio
async def test_device_clear_gatt_cache_delegates_to_connection() -> None:
    """OpenDisplayDevice.clear_gatt_cache() forwards to the connection and returns it."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake_conn = MagicMock()
    fake_conn.clear_cache = AsyncMock(return_value=True)
    device._connection = fake_conn

    assert await device.clear_gatt_cache() is True
    fake_conn.clear_cache.assert_awaited_once_with()
