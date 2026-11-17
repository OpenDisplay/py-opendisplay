"""The library's own transports wait for a reply while the display is noted as redrawing."""

from __future__ import annotations

import asyncio
import time

import pytest

from opendisplay import OpenDisplayDevice, redraw
from opendisplay.exceptions import BLETimeoutError, OpenDisplayTimeoutError
from opendisplay.transport.connection import BLEConnection
from opendisplay.transport.ip import TcpTransport

ADDRESS = "AA:BB:CC:DD:EE:FF"
FW_3_0_2 = b"\x00\x43\x03\x00\x28" + b"0" * 40 + b"\x02"


def _ble() -> BLEConnection:
    conn = BLEConnection(ADDRESS)
    conn.redraw_keys = (ADDRESS,)
    return conn


def _reply_later(queue: asyncio.Queue[bytes], data: bytes, delay: float) -> None:
    asyncio.get_running_loop().call_later(delay, queue.put_nowait, data)


async def test_a_ble_read_waits_past_its_timeout_while_a_note_is_pending() -> None:
    conn = _ble()
    redraw.mark_redrawing(ADDRESS, allowance=0.3)
    _reply_later(conn._notification_queue, b"\x00\x40", 0.15)

    assert await conn.read_response(timeout=0.05) == b"\x00\x40"


async def test_a_ble_read_without_a_note_times_out_as_before() -> None:
    conn = _ble()
    _reply_later(conn._notification_queue, b"\x00\x40", 0.15)
    start = time.monotonic()

    with pytest.raises(BLETimeoutError, match="0.05"):
        await conn.read_response(timeout=0.05)
    assert time.monotonic() - start < 0.12


async def test_a_note_never_makes_a_read_wait_past_its_deadline() -> None:
    conn = _ble()
    redraw.mark_redrawing(ADDRESS, allowance=0.2)
    start = time.monotonic()

    with pytest.raises(BLETimeoutError):
        await conn.read_response(timeout=0.05)
    assert time.monotonic() - start < 0.4


async def test_a_tcp_read_waits_past_its_timeout_while_a_note_is_pending() -> None:
    transport = TcpTransport("127.0.0.1", 2446)
    reader = asyncio.StreamReader()
    transport._reader = reader
    transport.redraw_keys = ("127.0.0.1",)
    redraw.mark_redrawing("127.0.0.1", allowance=0.3)
    asyncio.get_running_loop().call_later(0.15, reader.feed_data, b"\x02\x00\x00\x40")

    assert await transport.read_response(timeout=0.05) == b"\x00\x40"


async def test_a_tcp_read_without_a_note_times_out_as_before() -> None:
    transport = TcpTransport("127.0.0.1", 2446)
    transport._reader = asyncio.StreamReader()
    transport.redraw_keys = ("127.0.0.1",)

    with pytest.raises(OpenDisplayTimeoutError):
        await transport.read_response(timeout=0.05)


async def test_the_wait_is_one_longer_read_not_a_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = BLEConnection(ADDRESS)
    written: list[bytes] = []

    async def write_command(data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        written.append(data)
        _reply_later(conn._notification_queue, FW_3_0_2, 0.15)

    monkeypatch.setattr(conn, "write_command", write_command)
    monkeypatch.setattr(OpenDisplayDevice, "TIMEOUT_ACK", 0.05)
    device = OpenDisplayDevice(transport=conn, auto_interrogate=False)
    device._attach(conn)
    redraw.mark_redrawing(ADDRESS, allowance=0.5)

    firmware = await device.read_firmware_version()

    assert firmware["major"] == 3
    assert len(written) == 1  # the firmware holds a command during a refresh: never re-sent


def test_the_device_gives_its_identities_to_its_own_transport() -> None:
    conn = BLEConnection(ADDRESS)
    device = OpenDisplayDevice(transport=conn, auto_interrogate=False)
    device._device_name = "OD4064C9"  # as name resolution leaves it
    device._attach(conn)

    assert set(conn.redraw_keys) == {"OD4064C9", ADDRESS}


def test_a_caller_transport_without_redraw_keys_is_unchanged(fake_transport) -> None:
    fake = fake_transport([])
    device = OpenDisplayDevice(transport=fake, auto_interrogate=False)
    device._attach(fake)

    assert not hasattr(fake, "redraw_keys")


async def test_a_cancelled_tcp_read_leaves_no_read_behind() -> None:
    # A caller's asyncio.timeout() cancels the read during its first wait; the next read must work.
    transport = TcpTransport("127.0.0.1", 2446)
    reader = asyncio.StreamReader()
    transport._reader = reader
    transport.redraw_keys = ("127.0.0.1",)

    first = asyncio.ensure_future(transport.read_response(timeout=1.0))
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    reader.feed_data(b"\x02\x00\x00\x40")
    assert await transport.read_response(timeout=0.5) == b"\x00\x40"
