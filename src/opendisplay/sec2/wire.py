"""The SEC2 wire layer (SECURITY_V2.md § 3, § 5.1): opcodes, errors, fragmentation, SEC_STATUS.

A port of the wire half of the website's od-sec2.js (``BODY_LEN``, ``SEC_ERR``, ``fragment``,
``Reassembler``, ``parseSecError``, ``parseSecStatus``, ``recordNonce``). The opcodes and a few
values are provisional in the spec (§ 3.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from ..exceptions import InvalidResponseError
from .errors import Sec2DeviceError, Sec2FragmentError, SecErr

CMD_SEC_STATUS: Final = 0x0054  # plaintext, always allowed
CMD_SEC_CLAIM: Final = 0x0055  # plaintext, fragmented, BLE only, claim mode
CMD_SEC_AUTH: Final = 0x0056  # plaintext, fragmented, BLE only, claimed device
CMD_SEC_KEY: Final = 0x0057  # sealed only, BLE only
CMD_AUTHENTICATE_V1: Final = 0x0050  # the 2.x handshake; 3.0 answers it with a clean refusal
AUTH_STATUS_V2_REQUIRED: Final = 0x05  # 3.0's answer to the 2.x handshake on a claimed device
VERSION: Final = 0x02
FRAG_MAX_MIN: Final = 20
FRAG_MAX_MAX: Final = 244
FRAG_LAST: Final = 0x80
_FRAG_HEAD: Final = 4  # [0x00][cmd_lo][sub][frag]
_MAX_FRAGMENTS: Final = 127


class ClaimSub(IntEnum):
    """CMD_SEC_CLAIM sub-opcodes (§ 8.1)."""

    BEGIN = 0x01
    NONCE = 0x02
    CONFIRM = 0x03
    ABORT = 0x04
    EXIT = 0x05


class AuthSub(IntEnum):
    """CMD_SEC_AUTH sub-opcodes (§ 9.2)."""

    HELLO = 0x01
    FINISH = 0x02


class KeySub(IntEnum):
    """CMD_SEC_KEY sub-opcodes (§ 11)."""

    ROTATE = 0x01
    ROTATE_CONFIRM = 0x02
    UNCLAIM = 0x03


UNCLAIM_GUARD: Final = bytes([0xA5, 0x5A])

# Fixed body length of every handshake message, by direction, opcode and sub (§ 8.1, § 9.2).
_BODY_LEN: Final[dict[str, dict[int, dict[int, int]]]] = {
    "h2d": {CMD_SEC_CLAIM: {1: 66, 2: 16, 3: 16, 4: 0, 5: 0}, CMD_SEC_AUTH: {1: 82, 2: 16}},
    "d2h": {CMD_SEC_CLAIM: {1: 81, 2: 44, 3: 18, 4: 0, 5: 0}, CMD_SEC_AUTH: {1: 81, 2: 18}},
}


def body_length(direction: str, cmd: int, sub: int) -> int:
    """The fixed body length of a handshake message (there is no length field)."""
    return _BODY_LEN[direction][cmd][sub]


class Refusal(IntEnum):
    """Sealed record NACK codes (§ 9.5) and dispatcher refusals (§ 3.4)."""

    ENGINE_FAULT = 0x00
    RECORD = 0xF1
    REPLAY = 0xF2
    INTEGRITY = 0xF3
    CLAIM_MODE = 0xFD
    OVERSIZE = 0xFE


# § 6 rule 1: always plaintext, never sealed, even inside a session.
PLAINTEXT_SET: Final = frozenset({0x0043, 0x0044, 0x0046, 0x0050, 0x0054, 0x0055, 0x0056})


def is_plaintext_opcode(cmd: int) -> bool:
    """Whether ``cmd`` is in the plaintext set (§ 6 rule 1)."""
    masked = cmd & 0xFFFF
    return masked in PLAINTEXT_SET


def _check_frag_max(frag_max: int) -> None:
    if not FRAG_MAX_MIN <= frag_max <= FRAG_MAX_MAX:
        raise ValueError(f"frag_max must be {FRAG_MAX_MIN}..{FRAG_MAX_MAX}, got {frag_max}")


def fragment(cmd_lo: int, sub: int, body: bytes, frag_max: int) -> list[bytes]:
    """The deterministic split of § 3.3, used for both directions.

    Every non-LAST fragment carries ``frag_max - 4`` bytes and the LAST one the rest (1 to
    ``frag_max - 4``); an empty body is one empty LAST fragment.
    """
    _check_frag_max(frag_max)
    chunk = frag_max - _FRAG_HEAD
    if not body:
        return [bytes([0x00, cmd_lo, sub, FRAG_LAST])]
    count = -(-len(body) // chunk)
    if count > _MAX_FRAGMENTS:
        raise ValueError("body too long to fragment")
    return [
        bytes([0x00, cmd_lo, sub, index | (FRAG_LAST if index == count - 1 else 0)])
        + body[index * chunk : (index + 1) * chunk]
        for index in range(count)
    ]


def parse_sec_error(frame: bytes) -> tuple[int, int, SecErr | int] | None:
    """``(cmd_lo, sub, err)`` of a 4-byte ``[FF][cmd_lo][sub][err]`` for opcodes 0x54-0x57, else None."""
    if len(frame) != 4 or frame[0] != 0xFF or not 0x54 <= frame[1] <= 0x57:
        return None
    try:
        err: SecErr | int = SecErr(frame[3])
    except ValueError:
        err = frame[3]
    return frame[1], frame[2], err


class Reassembler:  # pylint: disable=too-few-public-methods  # one message, one feed() loop
    """Client-side reassembly of one D->H handshake message (§ 3.3).

    Strict, as a client may be: fragments must follow the deterministic split for the frag_max
    this client declared, in order, with the expected sub, to exactly the fixed body length.
    """

    def __init__(self, cmd_lo: int, sub: int, body_len: int, frag_max: int) -> None:
        _check_frag_max(frag_max)
        self._cmd_lo, self._sub, self._body_len = cmd_lo, sub, body_len
        self._chunk = frag_max - _FRAG_HEAD
        self._parts: list[bytes] = []
        self._got = 0
        self._next = 0
        self._finished = False

    def _fail(self, message: str) -> Sec2FragmentError:
        self._finished = True
        return Sec2FragmentError(message)

    def feed(self, frame: bytes) -> bytes | None:
        """Take one frame; the body once the LAST fragment arrives, else ``None``.

        Frames of other commands are ignored (``None``).

        Raises:
            Sec2DeviceError: The device answered with a SEC error for this opcode.
            Sec2FragmentError: The fragment breaks the rules; the partial message is discarded.
        """
        if self._finished:
            raise Sec2FragmentError("message already complete")
        error = parse_sec_error(frame)
        if error is not None and error[0] == self._cmd_lo:
            self._finished = True
            raise Sec2DeviceError(error[0], error[1], int(error[2]))
        if len(frame) < _FRAG_HEAD or frame[0] != 0x00 or frame[1] != self._cmd_lo:
            return None
        if frame[2] != self._sub:
            raise self._fail(f"fragment for sub {frame[2]}, expected {self._sub}")
        index, last, data = frame[3] & 0x7F, bool(frame[3] & FRAG_LAST), frame[_FRAG_HEAD:]
        if index != self._next:
            raise self._fail(f"fragment index {index}, expected {self._next}")
        remaining = self._body_len - self._got
        if not last:
            if len(data) != self._chunk or len(data) >= remaining:
                raise self._fail(f"non-deterministic split (non-LAST fragment of {len(data)} bytes)")
        elif (len(data) != 0) if self._body_len == 0 else (len(data) != remaining or not 1 <= len(data) <= self._chunk):
            raise self._fail(f"LAST fragment of {len(data)} bytes, expected {remaining}")
        self._parts.append(bytes(data))
        self._got += len(data)
        self._next += 1
        if last:
            self._finished = True
            return b"".join(self._parts)
        return None


class SecState(IntEnum):
    """The device's security state in SEC_STATUS (§ 5.1)."""

    UNCLAIMED = 0
    CLAIM_PENDING = 1  # a claim was started on this connection and is not yet confirmed
    CLAIMED = 2
    LOCKED = 3


_FLAG_CLAIM_MODE: Final = 0x01
_FLAG_SESSION_ACTIVE: Final = 0x02
_FLAG_LEGACY_SECRETS_OPEN: Final = 0x08
_FLAG_ROTATION_SUGGESTED: Final = 0x10
_FLAG_CLAIM_MODE_FRESH: Final = 0x20
_FLAG_CONFIG_LOCKED: Final = 0x40
DFLAG_HAS_PANEL: Final = 0x01
DFLAG_HAS_RESET_PIN: Final = 0x02
DFLAG_KEY_LEGACY: Final = 0x04
DFLAG_CLAIM_MODE_FRESH: Final = 0x08
_STATUS_LEN: Final = 19


@dataclass(frozen=True)
class SecStatus:  # pylint: disable=too-many-instance-attributes
    """A SEC_STATUS answer. Unauthenticated, so advisory: no key is ever deleted because of it."""

    sec_ver: int
    state: SecState | int
    flags: int
    window_s: int  # seconds left in claim mode; 0 outside it
    uid: bytes
    timeout_s: int  # the lifetime a session opened now gets
    dflags: int
    renders_left: int  # codes left in this claim-mode session; 0 outside it

    @property
    def claim_mode(self) -> bool:
        """The device is in claim mode."""
        return bool(self.flags & _FLAG_CLAIM_MODE)

    @property
    def session_active(self) -> bool:
        """A session is open on this connection."""
        return bool(self.flags & _FLAG_SESSION_ACTIVE)

    @property
    def legacy_secrets_open(self) -> bool:
        """Unclaimed, but the stored config holds 2.x secrets (§ 10.5)."""
        return bool(self.flags & _FLAG_LEGACY_SECRETS_OPEN)

    @property
    def rotation_suggested(self) -> bool:
        """The device key came from 2.x and should be rotated."""
        return bool(self.flags & _FLAG_ROTATION_SUGGESTED)

    @property
    def claim_mode_fresh(self) -> bool:
        """This claim-mode session is a fresh one (config writes allowed)."""
        return bool(self.flags & _FLAG_CLAIM_MODE_FRESH)

    @property
    def config_locked(self) -> bool:
        """Claim mode refuses config writes now (§ 7.4)."""
        return bool(self.flags & _FLAG_CONFIG_LOCKED)

    @property
    def has_panel(self) -> bool:
        """A drawable display is configured; the device will try to render the code."""
        return bool(self.dflags & DFLAG_HAS_PANEL)

    @property
    def key_legacy(self) -> bool:
        """The device key was migrated from 2.x."""
        return bool(self.dflags & DFLAG_KEY_LEGACY)


def parse_sec_status(frame: bytes) -> SecStatus:
    """Parse ``[00][54][sec_ver][state][flags][window_s:2][uid:8][timeout_s:2][dflags][renders_left]``.

    Raises:
        InvalidResponseError: Not a 19-byte SEC_STATUS answer.
    """
    if len(frame) < _STATUS_LEN or frame[0] != 0x00 or frame[1] != CMD_SEC_STATUS & 0xFF:
        raise InvalidResponseError(f"Not a SEC_STATUS answer: {frame.hex() or '<empty>'}")
    try:
        state: SecState | int = SecState(frame[3])
    except ValueError:
        state = frame[3]
    return SecStatus(
        sec_ver=frame[2],
        state=state,
        flags=frame[4],
        window_s=int.from_bytes(frame[5:7], "little"),
        uid=bytes(frame[7:15]),
        timeout_s=int.from_bytes(frame[15:17], "little"),
        dflags=frame[17],
        renders_left=frame[18],
    )


DIR_H2D: Final = b"H"
DIR_D2H: Final = b"D"


def record_nonce(direction: bytes, counter: int) -> bytes:
    """The 13-byte record nonce: ``direction || counter:4 LE || 8 zero bytes`` (§ 9.4)."""
    return direction + counter.to_bytes(4, "little") + bytes(8)
