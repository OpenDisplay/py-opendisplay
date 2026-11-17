"""Typed device keys: which security scheme a key belongs to.

``LegacyKey`` is a firmware 2.x master key (the 0x0050 handshake); ``DeviceKey`` is a firmware 3.0
device key (SEC2). A plain ``bytes`` key keeps working and is tried with whichever scheme the
device speaks.
"""

from __future__ import annotations

from typing import Final, Self

KEY_LEN: Final = 16


class _Key(bytes):
    def __new__(cls, value: bytes) -> Self:
        if len(value) != KEY_LEN:
            raise ValueError(f"A key is {KEY_LEN} bytes, got {len(value)}")
        return super().__new__(cls, value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<{KEY_LEN} bytes>)"


class LegacyKey(_Key):
    """A firmware 2.x master key."""


class DeviceKey(_Key):
    """A firmware 3.0 (SEC2) device key."""
