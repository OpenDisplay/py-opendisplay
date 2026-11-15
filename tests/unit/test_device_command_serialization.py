"""Tests for command serialization and session-state clearing (C5 / M9)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.device.channel import LegacySession
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme
from opendisplay.transport.connection import BLEConnection


def _make_device() -> OpenDisplayDevice:
    caps = DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO)
    return OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=caps)


@pytest.mark.asyncio
async def test_transaction_serializes_concurrent_commands() -> None:
    """Two command round-trips must not interleave."""
    device = _make_device()
    order: list[str] = []

    async def op(name: str, hold: float) -> None:
        async with device._transaction():
            order.append(f"{name}-start")
            await asyncio.sleep(hold)
            order.append(f"{name}-end")

    # gather schedules A first, so A acquires the lock; B must wait for A to finish.
    await asyncio.gather(op("A", 0.02), op("B", 0.0))

    assert order == ["A-start", "A-end", "B-start", "B-end"]


@pytest.mark.asyncio
async def test_transaction_is_reentrant_within_a_task() -> None:
    """A nested transaction in the same task must not deadlock."""
    device = _make_device()

    async def nested() -> str:
        async with device._transaction():
            async with device._transaction():
                return "ok"

    assert await asyncio.wait_for(nested(), timeout=1.0) == "ok"


def test_channel_reset_drops_the_session() -> None:
    device = _make_device()
    device._attach(MagicMock())
    device._channel.session = LegacySession(
        session_key=b"k" * 16, session_id=b"i" * 8, nonce_counter=5, established_at=123.0
    )

    device._channel.reset()

    assert device._channel.session is None


def test_on_disconnect_clears_session() -> None:
    device = _make_device()
    device._attach(MagicMock())
    device._channel.session = LegacySession(session_key=b"k" * 16, session_id=b"i" * 8, nonce_counter=9)

    device._on_disconnect()

    assert device._channel.session is None


def test_connection_disconnect_callback_is_invoked() -> None:
    calls: list[bool] = []
    conn = BLEConnection("AA:BB:CC:DD:EE:FF", disconnected_callback=lambda: calls.append(True))

    conn._on_disconnect(MagicMock())

    assert calls == [True]
