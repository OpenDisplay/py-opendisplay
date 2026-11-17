"""A BUSY handshake is retried after the display's pass, spaced under the device's rate limit."""

from __future__ import annotations

import secrets

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay import redraw
from opendisplay.sec2 import wire
from opendisplay.sec2.errors import RateLimitedError, Sec2BusyError
from opendisplay.sec2.keystore import MemoryKeyStore

NAME = "OD4064C9"


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A fake clock that each sleep advances."""
    t = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        t[0] += seconds

    monkeypatch.setattr(redraw, "_clock", lambda: t[0])
    monkeypatch.setattr(redraw, "_sleep", sleep)
    return sleeps


def _claimed() -> tuple[RefDevice, MemoryKeyStore]:
    key = secrets.token_bytes(16)
    device = RefDevice(k_dev=key)
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)
    return device, store


async def test_busy_hello_is_retried_until_the_session_opens(slept: list[float]) -> None:
    device, store = _claimed()
    device.busy_replies = 2

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        assert dev.security.sec2_session_open

    assert slept == [10.0, 10.0]
    assert device.handshake_starts == 3


async def test_busy_retries_stop_at_the_deadline(slept: list[float]) -> None:
    device, store = _claimed()
    device.busy_replies = 99

    with pytest.raises(Sec2BusyError):
        async with opendisplay.connect(transport=device, key_store=store):
            pytest.fail("entered")

    assert device.handshake_starts <= 6  # at most 6 BUSY a minute against the limiter's 10
    assert sum(slept) <= 60.0


async def test_rate_limited_is_never_retried(slept: list[float]) -> None:
    device, store = _claimed()
    device.busy_replies, device.refusal = 1, wire.SecErr.RATE_LIMITED

    with pytest.raises(RateLimitedError):
        async with opendisplay.connect(transport=device, key_store=store):
            pytest.fail("entered")

    assert device.handshake_starts == 1 and slept == []


async def test_busy_begin_is_retried(slept: list[float]) -> None:
    device = RefDevice(claim_mode=True)
    device.busy_replies = 1

    async with opendisplay.connect(transport=device, key_store=MemoryKeyStore()) as dev:
        flow = await dev.security.claim()
        await flow.begin()
        code = await flow.exchange_nonces()
        await flow.confirm(code.display)

    assert device.claimed and slept == [10.0]
