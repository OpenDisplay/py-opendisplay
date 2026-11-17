"""The SEC2 sealed session (SECURITY_V2.md § 9) and its handshake, CMD_SEC_AUTH HELLO / FINISH.

``Sec2Session`` implements the ``Session`` protocol of ``device/channel.py``, next to the 2.x
``LegacySession``. A port of od-sec2.js ``ClientRecordLayer`` / ``sealH2d`` / ``sealD2h`` and of
od-sec2-session.js ``authenticate``.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from cryptography.exceptions import InvalidTag

from ..protocol.commands import ENCRYPTED_CHUNK_SIZE
from . import kdf, wire
from .errors import (
    Sec2DeviceError,
    Sec2MutualAuthError,
    Sec2RecordError,
    Sec2SessionLostError,
    typed_device_error,
)
from .primitives import PrivateKey, ccm_decrypt, ccm_encrypt, ecdh, generate_key_pair

if TYPE_CHECKING:
    from ..transport import Transport

_LOGGER = logging.getLogger(__name__)

SEALED_D2H: Final = 0xE5
CTR_MAX: Final = 0xFFFFFFFE
H2D_OVERHEAD: Final = 2 + 4 + 12  # opcode, counter, tag
D2H_OVERHEAD: Final = 1 + 4 + 12  # 0xE5, counter, tag
_MAX_FAILURES: Final = 3
_SESSION_GONE_STATUS: Final = 0xFE
TIMEOUT_HANDSHAKE: Final = 5.0


def _h2d_aad(cmd_hi: int, cmd_lo: int) -> bytes:
    return bytes([wire.VERSION, cmd_hi, cmd_lo])


_D2H_AAD: Final = bytes([wire.VERSION, SEALED_D2H])


def seal_d2h(k_d2h: bytes, counter: int, plaintext_frame: bytes) -> bytes:
    """Seal one device->host frame (the device's side; for tests and reference devices)."""
    body = ccm_encrypt(k_d2h, wire.record_nonce(wire.DIR_D2H, counter), _D2H_AAD, plaintext_frame)
    return bytes([SEALED_D2H]) + counter.to_bytes(4, "little") + body


def open_h2d(k_h2d: bytes, frame: bytes) -> bytes:
    """Open one host->device frame and return its payload (the device's side; for tests)."""
    counter = int.from_bytes(frame[2:6], "little")
    return ccm_decrypt(k_h2d, wire.record_nonce(wire.DIR_H2D, counter), _h2d_aad(frame[0], frame[1]), frame[6:])


@dataclass
class Sec2Session:  # pylint: disable=too-many-instance-attributes
    """A firmware 3.0 session: per-direction AES-CCM records with strictly increasing counters.

    One object per connection; drop it on any disconnect (§ 9.7 rule 1).
    """

    k_h2d: bytes = field(repr=False)
    k_d2h: bytes = field(repr=False)
    sid: bytes = field(default=b"", repr=False)
    send_counter: int = 0
    recv_counter: int = 0
    failures: int = 0
    timeout_s: int = 3600
    established_at: float = 0.0
    # 154 until the protocol header raises the encrypted chunk budget under SEC2 (§ 9.4).
    direct_chunk_size: int = ENCRYPTED_CHUNK_SIZE

    def seal(self, frame: bytes) -> bytes:
        """Seal ``[cmd_hi][cmd_lo][payload]``; plaintext-set opcodes go out unsealed (§ 6 rule 1).

        Every call takes a new counter, so a retransmission is never a replay.

        Raises:
            Sec2SessionLostError: The counter is exhausted; a new handshake is needed.
        """
        if len(frame) < 2:
            raise ValueError("command too short")
        if wire.is_plaintext_opcode(int.from_bytes(frame[:2], "big")):
            return frame
        if self.send_counter >= CTR_MAX:
            raise Sec2SessionLostError("SEC2 counter exhausted; a new handshake is needed")
        self.send_counter += 1
        nonce = wire.record_nonce(wire.DIR_H2D, self.send_counter)
        body = ccm_encrypt(self.k_h2d, nonce, _h2d_aad(frame[0], frame[1]), frame[2:])
        return frame[:2] + self.send_counter.to_bytes(4, "little") + body

    def is_sealed(self, raw: bytes) -> bool:
        """Whether ``raw`` is a sealed device frame: first byte 0xE5, at least 17 bytes."""
        return len(raw) >= D2H_OVERHEAD and raw[0] == SEALED_D2H

    def open(self, raw: bytes) -> bytes:
        """Open a sealed device frame and return the plaintext response frame (§ 9.5 client rule 1).

        Raises:
            Sec2RecordError: The counter is not greater than the last one, or the tag fails.
            Sec2SessionLostError: The third frame whose tag failed: disconnect and re-authenticate.
        """
        counter = int.from_bytes(raw[1:5], "little")
        if counter <= self.recv_counter or counter > CTR_MAX:
            raise Sec2RecordError(f"device counter {counter} is not greater than {self.recv_counter}; discarded")
        try:
            plaintext = ccm_decrypt(self.k_d2h, wire.record_nonce(wire.DIR_D2H, counter), _D2H_AAD, raw[5:])
        except InvalidTag as err:
            self.failures += 1
            if self.failures >= _MAX_FAILURES:
                raise Sec2SessionLostError("three sealed frames failed their integrity check") from err
            raise Sec2RecordError("a sealed frame failed its integrity check; discarded") from err
        self.recv_counter = counter
        return plaintext

    def allows_plaintext(self, raw: bytes) -> bool:
        """Whether an unsealed frame may be accepted inside this session (§ 9.5 client rules 2-5).

        A plaintext answer to a plaintext-set opcode, and a 4-byte SEC error, are accepted
        (advisory). The session-gone frame ``[00][cmd_lo][FE]`` is accepted so the caller can
        act on it. Anything else is discarded.
        """
        if len(raw) < 2:
            return False
        if len(raw) == 3 and raw[0] == 0x00 and raw[2] == _SESSION_GONE_STATUS:
            return True
        if wire.parse_sec_error(raw) is not None:
            return True
        return raw[0] in (0x00, 0xFF) and wire.is_plaintext_opcode(raw[1])

    def pipe_data_size(self, frame_eff: int) -> int:
        """Chunk data of a sealed PIPE_WRITE frame: ``frame_eff`` - 18 (record) - 1 (seq)."""
        return frame_eff - H2D_OVERHEAD - 1


async def send_fragments(transport: Transport, cmd_lo: int, sub: int, body: bytes, frag_max: int) -> None:
    """Send one handshake message as fragments, each a write-with-response (§ 3.3)."""
    for frame in wire.fragment(cmd_lo, sub, body, frag_max):
        await transport.write_command(frame, response=True)


async def receive(transport: Transport, cmd_lo: int, sub: int, frag_max: int) -> bytes:
    """Reassemble one handshake reply; frames of other commands are skipped.

    Raises:
        Sec2DeviceError: The device answered with a SEC error.
        Sec2FragmentError: The reply broke the fragment rules.
    """
    reassembler = wire.Reassembler(cmd_lo, sub, wire.body_length("d2h", 0x0000 | cmd_lo, sub), frag_max)
    while True:
        body = reassembler.feed(await transport.read_response(timeout=TIMEOUT_HANDSHAKE))
        if body is not None:
            return body


async def authenticate(  # pylint: disable=too-many-arguments
    transport: Transport,
    k_dev: bytes,
    uid: bytes,
    *,
    frag_max: int = wire.FRAG_MAX_MIN,
    _key_pair: tuple[PrivateKey, bytes] | None = None,
    _nc: bytes | None = None,
) -> Sec2Session:
    """Open a session with a claimed device: SEC_AUTH HELLO, then FINISH (§ 9.2, § 9.3).

    ``uid`` is the one stored with the key, never the one the device reports now (§ 4.3).
    ``_key_pair`` and ``_nc`` exist only so tests can replay the vectors.

    Raises:
        Sec2AuthFailedError: The device refused our proof (wrong key). No session is opened.
        Sec2MutualAuthError: The device's proof failed: a wrong device or an attacker.
        Sec2DeviceError: Another SEC error (NOT_CLAIMED, LOCKED, RATE_LIMITED, ...).
    """
    cmd_lo = wire.CMD_SEC_AUTH & 0xFF
    transport.drain_notifications()  # § 9.7 rule 3
    private, qc = _key_pair if _key_pair is not None else generate_key_pair()
    nc = _nc if _nc is not None else secrets.token_bytes(16)
    try:
        hello_body = bytes([wire.VERSION, frag_max]) + qc + nc
        await send_fragments(transport, cmd_lo, wire.AuthSub.HELLO, hello_body, frag_max)
        hello = await receive(transport, cmd_lo, wire.AuthSub.HELLO, frag_max)
        dflags, qd, nd = hello[0], hello[1:65], hello[65:81]
        ta = kdf.auth_transcript(dflags, uid, qc, qd, nc, nd)
        prk = kdf.extract(kdf.key_hierarchy(k_dev, uid).k_ext, ecdh(private, qd))
        await send_fragments(transport, cmd_lo, wire.AuthSub.FINISH, kdf.kdf(prk, "OD2/auth-c", ta), frag_max)
        finish = await receive(transport, cmd_lo, wire.AuthSub.FINISH, frag_max)
    except Sec2DeviceError as err:
        raise typed_device_error(err) from err
    if not hmac.compare_digest(finish[:16], kdf.kdf(prk, "OD2/auth-d", ta)):
        raise Sec2MutualAuthError()
    keys = kdf.session_keys(prk, ta)
    _LOGGER.info("SEC2 session established")
    return Sec2Session(
        k_h2d=keys.k_h2d,
        k_d2h=keys.k_d2h,
        sid=keys.sid,
        timeout_s=int.from_bytes(finish[16:18], "little"),
        established_at=time.monotonic(),
    )
