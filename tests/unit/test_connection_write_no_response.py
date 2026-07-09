"""Tests for BLE Write Without Response on 0x71 data writes (send-without-reply)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opendisplay.transport.connection import BLEConnection


def _make_conn_with_client(supports_wnr: bool) -> tuple[BLEConnection, MagicMock]:
    conn = BLEConnection("AA:BB:CC:DD:EE:FF")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    conn._client = client
    conn._notification_characteristic = MagicMock()
    conn._write_no_response_supported = supports_wnr
    return conn, client


@pytest.mark.asyncio
async def test_write_command_uses_write_without_response_when_supported() -> None:
    conn, client = _make_conn_with_client(supports_wnr=True)
    await conn.write_command(b"\x00\x71data", response=False)
    _, kwargs = client.write_gatt_char.call_args
    assert kwargs["response"] is False


@pytest.mark.asyncio
async def test_write_command_falls_back_when_wnr_unsupported() -> None:
    """If the characteristic doesn't advertise WNR, response=False degrades to a Write Request."""
    conn, client = _make_conn_with_client(supports_wnr=False)
    await conn.write_command(b"\x00\x71data", response=False)
    _, kwargs = client.write_gatt_char.call_args
    assert kwargs["response"] is True


@pytest.mark.asyncio
async def test_write_command_defaults_to_write_with_response() -> None:
    conn, client = _make_conn_with_client(supports_wnr=True)
    await conn.write_command(b"\x00\x70start")
    _, kwargs = client.write_gatt_char.call_args
    assert kwargs["response"] is True


def _make_client_with_char(properties: list[str]) -> MagicMock:
    char = MagicMock()
    char.properties = properties
    service = MagicMock()
    service.characteristics = [char]
    client = MagicMock()
    client.is_connected = True
    client.services.get_service.return_value = service
    client.start_notify = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_setup_notifications_detects_wnr_property() -> None:
    conn = BLEConnection("AA:BB:CC:DD:EE:FF")
    conn._client = _make_client_with_char(["read", "write", "write-without-response", "notify"])

    await conn._setup_notifications()

    assert conn._write_no_response_supported is True


@pytest.mark.asyncio
async def test_setup_notifications_without_wnr_property() -> None:
    conn = BLEConnection("AA:BB:CC:DD:EE:FF")
    conn._client = _make_client_with_char(["read", "write", "notify"])

    await conn._setup_notifications()

    assert conn._write_no_response_supported is False
