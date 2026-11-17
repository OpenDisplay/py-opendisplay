"""Claiming an unclaimed firmware 3.0 device: CMD_SEC_CLAIM (SECURITY_V2.md § 8).

A port of od-sec2.js ``ClaimClient`` over a transport. The order protects the user:

1. BEGIN, then NONCE: the client checks the device's commitment and unwraps the new key; the
   device now shows a 6-digit code.
2. The user **types** the code. A code that does not match the client's own is never sent as
   CONFIRM: the claim is aborted (§ 8.4).
3. The new key is **staged** in the key store, durably, before CONFIRM is sent; a store that
   cannot hold it aborts the claim (§ 8.8 rule 1).
4. CONFIRM. Only a verified ``tag_d`` **promotes** the key; otherwise it stays a candidate.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cryptography.exceptions import InvalidTag

from ..exceptions import OpenDisplayTimeoutError
from . import kdf, wire
from .errors import (
    Sec2ClaimUnconfirmedError,
    Sec2CodeMismatchError,
    Sec2DeviceError,
    Sec2Error,
    Sec2InterferenceError,
    typed_device_error,
)
from .keystore import KeyStore
from .primitives import PrivateKey, ccm_decrypt, ecdh, generate_key_pair, is_valid_point
from .session import Sec2Session, receive, send_fragments

if TYPE_CHECKING:
    from ..transport import Transport

_LOGGER = logging.getLogger(__name__)
_CMD: Final = wire.CMD_SEC_CLAIM & 0xFF


@dataclass(frozen=True)
class ClaimCode:
    """What the device now shows (or would show): ask the user to type it."""

    code: str  # six digits
    display: str  # "123 456"
    has_panel: bool  # False: the device has no screen to show it on (§ 8.6)


class ClaimFlow:  # pylint: disable=too-many-instance-attributes
    """One claim of one device over one connection.

    Args:
        transport: The connected BLE transport (claims are BLE only).
        store: Where the new key is staged and promoted.
        name: The device's name in the store (its advertised name).
        uid: The device's uid, from SEC_STATUS.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        transport: Transport,
        store: KeyStore,
        name: str,
        uid: bytes,
        *,
        frag_max: int = wire.FRAG_MAX_MIN,
        on_session: Callable[[Sec2Session], None] | None = None,
        _key_pair: tuple[PrivateKey, bytes] | None = None,
        _nc: bytes | None = None,
    ) -> None:
        self._on_session = on_session
        if len(uid) != kdf.UID_LEN:
            raise ValueError("uid must be 8 bytes")
        self._transport, self._store, self._name, self._uid = transport, store, name, uid
        self._frag_max = frag_max
        self._private: PrivateKey | None = None
        self._key_pair = _key_pair
        self._nc = _nc
        self._qc = self._qd = self._cd = self._tc = self._prk = b""
        self._dflags = 0
        self._code: ClaimCode | None = None
        self.k_dev: bytes | None = None

    async def _exchange(self, sub: int, body: bytes) -> bytes:
        await send_fragments(self._transport, _CMD, sub, body, self._frag_max)
        try:
            return await receive(self._transport, _CMD, sub, self._frag_max)
        except Sec2DeviceError as err:
            raise typed_device_error(err) from err

    async def begin(self) -> None:
        """BEGIN: send ``Qc``, receive ``dflags``, ``Qd`` and the commitment ``Cd``.

        Raises:
            NotInClaimModeError, AlreadyClaimedError, ClaimAttemptsExhaustedError,
            DeviceLockedError, RateLimitedError, Sec2BusyError: the device refused, and why.
        """
        self._transport.drain_notifications()
        self._private, self._qc = self._key_pair if self._key_pair is not None else generate_key_pair()
        reply = await self._exchange(wire.ClaimSub.BEGIN, bytes([wire.VERSION, self._frag_max]) + self._qc)
        self._dflags, self._qd, self._cd = reply[0], reply[1:65], reply[65:81]
        if not is_valid_point(self._qd):
            await self.abort()
            raise Sec2InterferenceError("The display sent an invalid public key; the claim was aborted.")

    async def exchange_nonces(self) -> ClaimCode:
        """NONCE: reveal ``Nc``, receive ``Nd`` and the wrapped key; the device now shows the code.

        Raises:
            Sec2InterferenceError: The commitment does not open, or the key does not unwrap.
        """
        nc = self._nc if self._nc is not None else secrets.token_bytes(16)
        reply = await self._exchange(wire.ClaimSub.NONCE, nc)
        nd, wrapped = reply[:16], reply[16:44]
        if not hmac.compare_digest(self._cd, kdf.commitment(nd, self._uid, self._qd, self._qc)):
            await self.abort()
            raise Sec2InterferenceError("The display's commitment does not match: someone may be interfering.")
        assert self._private is not None
        z = ecdh(self._private, self._qd)
        self._private = None
        self._tc = kdf.claim_transcript(self._dflags, self._uid, self._qc, self._qd, nc, nd)
        self._prk = kdf.extract(bytes(16), z)
        try:
            self.k_dev = ccm_decrypt(
                kdf.kdf(self._prk, "OD2/wrap", self._tc), kdf.WRAP_NONCE, kdf.wrap_aad(self._uid), wrapped
            )
        except InvalidTag as err:
            await self.abort()
            raise Sec2InterferenceError("Could not unwrap the key: someone may be interfering.") from err
        code = kdf.code_from_raw(kdf.code_raw(nc, self._uid, self._qc, self._qd, nd))
        self._code = ClaimCode(code, kdf.format_code(code), bool(self._dflags & wire.DFLAG_HAS_PANEL))
        return self._code

    def code_matches(self, typed: str) -> bool:
        """Whether ``typed`` ("123 456", "123456", ...) is the client's own code."""
        return self._code is not None and kdf.normalize_typed_code(typed) == self._code.code

    async def confirm(self, typed: str) -> Sec2Session:
        """CONFIRM with the code the user typed; returns the session the claim opens.

        Raises:
            Sec2CodeMismatchError: The code differs; ABORT was sent and nothing is kept.
            Sec2StorageError: The key could not be stored; ABORT was sent.
            Sec2ClaimUnconfirmedError: The device's proof failed; the key stays a candidate.
        """
        if not self.code_matches(typed):
            await self.abort()
            raise Sec2CodeMismatchError()
        return await self._confirm()

    async def confirm_without_code(self, *, acknowledged: bool) -> Sec2Session:
        """CONFIRM without a code check: only for a display without a working screen (§ 8.6).

        The caller must have refused this path when it has any record of a display for this
        device, and must have shown the warning; ``acknowledged`` is the user's explicit yes.
        """
        if not acknowledged:
            raise ValueError("A claim without the code check needs the user to acknowledge the warning")
        return await self._confirm()

    async def _confirm(self) -> Sec2Session:
        assert self.k_dev is not None and self._code is not None
        try:
            self._store.stage(self._name, self.k_dev, "claim")
        except Exception:
            await self.abort()
            raise
        k_conf = kdf.key_hierarchy(self.k_dev, self._uid).k_conf
        reply = await self._exchange(wire.ClaimSub.CONFIRM, kdf.kdf(k_conf, "OD2/claim-c", self._tc))
        if not hmac.compare_digest(reply[:16], kdf.kdf(k_conf, "OD2/claim-d", self._tc)):
            raise Sec2ClaimUnconfirmedError()
        self._store.promote(self._name, self.k_dev, uid=self._uid)
        keys = kdf.session_keys(self._prk, self._tc)
        self._prk = b""
        _LOGGER.info("Claimed %s", self._name)
        session = Sec2Session(
            k_h2d=keys.k_h2d,
            k_d2h=keys.k_d2h,
            sid=keys.sid,
            timeout_s=int.from_bytes(reply[16:18], "little"),
            established_at=time.monotonic(),
        )
        if self._on_session is not None:
            self._on_session(session)
        return session

    async def abort(self) -> None:
        """ABORT: the device discards the pending claim and its key candidate."""
        self._private, self._prk = None, b""
        await send_fragments(self._transport, _CMD, wire.ClaimSub.ABORT, b"", self._frag_max)
        try:  # read the (empty) reply so it is not taken for the next command's answer
            await receive(self._transport, _CMD, wire.ClaimSub.ABORT, self._frag_max)
        except (OpenDisplayTimeoutError, Sec2Error) as err:
            _LOGGER.debug("No clean ABORT reply: %s", err)


async def exit_claim_mode(transport: Transport, *, frag_max: int = wire.FRAG_MAX_MIN) -> None:
    """EXIT: end claim mode and use the device without a key (it reboots into normal mode)."""
    await send_fragments(transport, _CMD, wire.ClaimSub.EXIT, b"", frag_max)
    await receive(transport, _CMD, wire.ClaimSub.EXIT, frag_max)
