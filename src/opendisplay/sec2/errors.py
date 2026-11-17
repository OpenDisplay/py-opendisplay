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


class Sec2KeyRequiredError(Sec2Error):
    """A claimed device: no key opened a session. Every key was kept (§ 8.8 rule 4)."""

    def __init__(self, tried: int) -> None:
        self.tried = tried
        super().__init__(
            f"The display is claimed and none of the {tried} keys tried opened a session; every key was kept. "
            "If another client rotated the key, import the new key from that client; "
            "if the key is lost, factory-reset the display (five quick power cycles)."
            if tried
            else "The display is claimed and no key for it is known. Pass its device key or a key store that has it; "
            "if the key is lost, factory-reset the display (five quick power cycles)."
        )


class Sec2StorageError(Sec2Error):
    """A key could not be stored durably; the caller must not let the device take it (§ 8.8)."""


class NotInClaimModeError(Sec2DeviceError):
    """The device is unclaimed and in normal mode."""


class AlreadyClaimedError(Sec2DeviceError):
    """The device already has a key."""


class ClaimAttemptsExhaustedError(Sec2DeviceError):
    """The third code of this claim-mode session was used; the device rebooted into normal mode."""


class DeviceLockedError(Sec2DeviceError):
    """The device has evidence of a claim but cannot read its key record (§ 10.1)."""


class RateLimitedError(Sec2DeviceError):
    """Too many handshake starts in the last minute (§ 8.7)."""


class Sec2BusyError(Sec2DeviceError):
    """A display pass or flash erase holds the device."""


_ADVICE: dict[SecErr, tuple[type[Sec2DeviceError], str]] = {
    SecErr.NOT_IN_CLAIM_MODE: (
        NotInClaimModeError,
        "The display is not in claim mode. Power-cycle it twice quickly to start claim mode, then try again.",
    ),
    SecErr.ALREADY_CLAIMED: (
        AlreadyClaimedError,
        "The display is already claimed. Connect with its key; without the key it needs a factory reset "
        "(five quick power cycles).",
    ),
    SecErr.ATTEMPTS_EXHAUSTED: (
        ClaimAttemptsExhaustedError,
        "All three codes of this claim attempt were used and the display left claim mode. Power-cycle it "
        "twice quickly to try again.",
    ),
    SecErr.LOCKED: (
        DeviceLockedError,
        "The display cannot read its key record and is locked. It needs a factory reset (five quick power cycles).",
    ),
    SecErr.RATE_LIMITED: (
        RateLimitedError,
        "Too many attempts in the last minute. Wait a minute and try again.",
    ),
    SecErr.BUSY: (Sec2BusyError, "The display is busy (refreshing or erasing). Try again in a few seconds."),
    SecErr.AUTH_FAILED: (
        Sec2AuthFailedError,
        "The display refused the key or the confirmation. If you have another key for it, try that one.",
    ),
}


def typed_device_error(err: Sec2DeviceError) -> Sec2DeviceError:
    """The typed error, with what the user has to do, for a device's SEC refusal."""
    entry = _ADVICE.get(err.err) if isinstance(err.err, SecErr) else None
    if entry is None:
        return err
    cls, advice = entry
    return cls(err.cmd_lo, err.sub, int(err.err), advice)


class Sec2CodeMismatchError(Sec2Error):
    """The typed code differs from the client's own: the claim was aborted (§ 8.4)."""

    def __init__(self) -> None:
        super().__init__(
            "The code does not match. Someone nearby may be interfering. The display may now be claimed by "
            "someone else. If so, factory-reset it and try again."
        )


class Sec2InterferenceError(Sec2Error):
    """A check that only an interfering party fails: the claim was aborted."""


class Sec2ClaimUnconfirmedError(Sec2Error):
    """The device did not confirm the claim (``tag_d`` mismatch); the key is kept as a candidate."""

    def __init__(self) -> None:
        super().__init__("Could not confirm the claim with the display; the key is kept and will be tried again.")
