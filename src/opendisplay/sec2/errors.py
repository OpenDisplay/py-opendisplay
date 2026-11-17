"""Typed SEC2 errors (SECURITY_V2.md § 3.2)."""

from __future__ import annotations

from enum import IntEnum

from ..exceptions import ProtocolError


class SecErr(IntEnum):
    """``OD_SEC_ERR_*``, shared by opcodes 0x0054-0x0057 (§ 3.2)."""

    MALFORMED = 0x01
    BAD_STATE = 0x02
    RATE_LIMITED = 0x03
    NOT_IN_CLAIM_MODE = 0x04
    ALREADY_CLAIMED = 0x05
    NOT_CLAIMED = 0x06
    AUTH_FAILED = 0x07
    EXPIRED = 0x08
    BAD_POINT = 0x09
    CRYPTO_ERROR = 0x0A
    STORAGE_ERROR = 0x0B
    BUSY = 0x0C
    ATTEMPTS_EXHAUSTED = 0x0D
    LOCKED = 0x0E
    WRONG_TRANSPORT = 0x0F


class Sec2Error(ProtocolError):
    """A SEC2 handshake or session failed."""


class Sec2BadPointError(Sec2Error):
    """A received P-256 public key is not a valid point (§ 2, point validation)."""

    def __init__(self, message: str = "The peer's P-256 public key is not a valid point; refusing it.") -> None:
        super().__init__(message)


class Sec2FragmentError(Sec2Error):
    """A handshake fragment broke the rules of § 3.3 (order, split, length or sub)."""


class Sec2DeviceError(Sec2Error):
    """The device answered a SEC opcode with ``[FF][cmd_lo][sub][err]`` (§ 3.2)."""

    def __init__(self, cmd_lo: int, sub: int, err: int, message: str | None = None) -> None:
        self.cmd_lo = cmd_lo
        self.sub = sub
        try:
            self.err: SecErr | int = SecErr(err)
        except ValueError:
            self.err = err
        name = self.err.name if isinstance(self.err, SecErr) else f"0x{err:02x}"
        super().__init__(message or f"SEC2 opcode 0x00{cmd_lo:02x} sub {sub} refused: {name}")


class Sec2AuthFailedError(Sec2DeviceError):
    """The device refused our proof (AUTH_FAILED): wrong key or a bad claim confirmation.

    Unauthenticated: never delete a stored key because of it (§ 8.8).
    """


class Sec2MutualAuthError(Sec2Error):
    """The device could not prove it holds the key: a wrong device or an attacker (§ 9.3)."""

    def __init__(self, message: str = "The device failed mutual authentication; it may be another device.") -> None:
        super().__init__(message)


class Sec2RecordError(Sec2Error):
    """A sealed frame from the device was refused (counter or integrity, § 9.5); it was discarded."""


class Sec2SessionLostError(Sec2Error):
    """The SEC2 session is gone (three bad frames, counter exhausted, or the device dropped it).

    Open a new session with a fresh handshake.
    """
