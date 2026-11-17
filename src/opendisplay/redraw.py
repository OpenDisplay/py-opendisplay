"""Which displays are redrawing their panel: an in-process record, no I/O.

A display that redraws goes silent: no advertisements, and commands are held until the refresh
ends (Firmware_Unified: Bluetooth starts only after the boot screen; ``loop()`` is blocked for a
whole refresh). Telling "redrawing" from "gone" is impossible over the air. So the library notes
"this display is redrawing until T" whenever it sends something that makes the firmware redraw,
and connects and replies wait until T instead of failing at their normal timeout. Waiting ends as
soon as the display answers.

A note is keyed by every identity the library knows for the display (name, BLE address, host),
case-insensitively. A note only ever extends; it is never cleared early, and simply expires.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, TypeVar

_T = TypeVar("_T")

REDRAW_ALLOWANCE_S: Final = 60.0  # firmware: a refresh takes up to 60 s
BUSY_RETRY_S: Final = 10.0  # ≤ 6 BUSY a minute against the device limiter's 10 (SECURITY_V2 § 8.7)
REDRAW_HINT: Final = (
    "If the display is redrawing (some colour panels take about 30 s after a power-up, reboot or "
    "config change), try again in 30 s."
)

_clock: Callable[[], float] = time.monotonic
_sleep = asyncio.sleep
_deadlines: dict[str, float] = {}


def _keys(keys: Sequence[str | None]) -> list[str]:
    return [k.lower() for k in keys if k]


def mark_redrawing(*keys: str | None, allowance: float = REDRAW_ALLOWANCE_S) -> None:
    """Note that the display known by ``keys`` is redrawing for up to ``allowance`` seconds."""
    deadline = _clock() + allowance
    for key in _keys(keys):
        _deadlines[key] = max(_deadlines.get(key, 0.0), deadline)


def link(*keys: str | None) -> None:
    """Give every key the latest deadline any of them has (a name resolved to an address)."""
    names = _keys(keys)
    latest = max((_deadlines.get(k, 0.0) for k in names), default=0.0)
    if latest > _clock():
        for key in names:
            _deadlines[key] = latest


def remaining(*keys: str | None) -> float:
    """Seconds until the latest pending deadline among ``keys``; 0.0 when none is pending."""
    latest = max((_deadlines.get(k, 0.0) for k in _keys(keys)), default=0.0)
    return max(0.0, latest - _clock())


def wait_timeout(keys: Sequence[str | None], timeout: float) -> float:
    """How long to wait for a reply: ``timeout``, stretched to a pending deadline."""
    return max(timeout, remaining(*keys))


async def retry_busy(op: Callable[[], Awaitable[_T]], *keys: str | None) -> _T:
    """Run a SEC2 handshake start; on BUSY, retry it every ``BUSY_RETRY_S`` until the redraw ends.

    BUSY means a display pass holds the device, and each one counts in the device's handshake rate
    limiter (SECURITY_V2 § 8.7), so retries are spaced, not immediate. Without a pending note the
    allowance starts at the first BUSY. Every other error, ``RateLimitedError`` included, is raised
    at once.
    """
    from .sec2.errors import Sec2BusyError  # pylint: disable=import-outside-toplevel  # no cycle at import

    deadline: float | None = None
    while True:
        try:
            return await op()
        except Sec2BusyError:
            if deadline is None:  # a pending note, or one allowance from this first BUSY
                deadline = _clock() + (remaining(*keys) or REDRAW_ALLOWANCE_S)
                mark_redrawing(*keys, allowance=deadline - _clock())
            await _sleep(BUSY_RETRY_S)
            if _clock() >= deadline:
                raise


def _reset() -> None:
    """Forget every note (tests)."""
    _deadlines.clear()
