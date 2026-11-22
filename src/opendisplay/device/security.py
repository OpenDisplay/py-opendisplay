"""Device security on the new surface: ``Device.security`` and the scheme choice at connect.

On entry, ``establish`` reads the firmware version and, on firmware 3.0, READ_IDENT and SEC_STATUS
**before any handshake**, then picks the scheme (pieces 2b/3 findings):

- firmware 2.x with a key: the 2.x handshake, as ``OpenDisplayDevice`` always did. Firmware_Silabs
  2.x with encryption on answers even the version read only inside a session, so there the
  handshake runs first (``open_legacy_session``);
- firmware 3.0 without the SEC2 module (READ_IDENT SEC_V2 = 0, § 13.6): no SEC_STATUS, which such
  a build does not know; the device is open;
- firmware 3.0, claimed: SEC2 with the given key and the store's keys, at most three (§ 8.8);
- firmware 3.0, unclaimed: no handshake; the device is open.

A migrated 2.x key (``dflags.KEY_LEGACY``) is rotated to a device-generated key right after the
first session when a key store can keep the new key, unless the caller opts out (§ 10.5).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from .. import redraw
from ..credentials import DeviceKey, LegacyKey
from ..sec2 import wire
from ..sec2.claim import ClaimFlow, exit_claim_mode
from ..sec2.errors import DeviceLockedError, Sec2AuthFailedError, Sec2KeyRequiredError, SecErr
from ..sec2.keys import rotate_key, unclaim
from ..sec2.keystore import MAX_TRY, KeyStore
from ..sec2.session import Sec2Session, authenticate
from ..sec2.wire import SecState, SecStatus
from ..transport import TcpTransport

if TYPE_CHECKING:
    from ..models.ident import DeviceIdent
    from ..transport import Transport
    from .api import ConfigView
    from .channel import SecureChannel
    from .facade import OpenDisplayDevice

_LOGGER = logging.getLogger(__name__)
_SEC2_MIN_MAJOR: Final = 3


class SecurityView:
    """The device's security state and the operations on it (claim, rotate, unclaim)."""

    def __init__(
        self,
        legacy: OpenDisplayDevice,
        *,
        credential: bytes | None = None,
        key_store: KeyStore | None = None,
        rotate_legacy_key: bool = True,
    ) -> None:
        self._legacy = legacy
        self._credential = credential
        self._store = key_store
        self._rotate_legacy_key = rotate_legacy_key
        self._status: SecStatus | None = None
        self._key: bytes | None = None
        self._uid: bytes | None = None
        self._legacy_session_open = False
        #: The device's ``ConfigView`` (set by ``Device``): whether the panel shows the key.
        self.config_view: ConfigView | None = None

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def status(self) -> SecStatus | None:
        """The last SEC_STATUS read (firmware 3.0), ``None`` on 2.x. Advisory: unauthenticated."""
        return self._status

    @property
    def sec2_session_open(self) -> bool:
        """Whether a SEC2 session is open on this connection."""
        return isinstance(self._channel().session, Sec2Session)

    async def refresh_status(self) -> SecStatus:
        """Read SEC_STATUS again."""
        self._status = await self._legacy.read_sec_status()
        return self._status

    # Device wraps OpenDisplayDevice until piece 7 inverts the two; these are its internals.
    def _channel(self) -> SecureChannel:
        return self._legacy._channel  # pylint: disable=protected-access

    def _transport(self) -> Transport:
        return self._legacy._conn  # pylint: disable=protected-access

    def _name(self) -> str:
        return self._legacy.device_name or self._legacy.mac_address

    async def _authenticate(self, key: bytes, uid: bytes) -> Sec2Session:
        """HELLO/FINISH, retried after a display pass that answers BUSY."""
        keys = self._legacy._redraw_keys()  # pylint: disable=protected-access
        return await redraw.retry_busy(
            lambda: authenticate(self._transport(), key, uid, frag_max=self._frag_max()), *keys
        )

    def _note_redraw(self) -> None:
        redraw.mark_redrawing(*self._legacy._redraw_keys())  # pylint: disable=protected-access

    def _key_shown(self) -> bool:
        config = self.config_view.cached if self.config_view is not None else None
        security = getattr(config, "security_config", None)
        return bool(security is not None and security.show_key_on_screen)

    def _frag_max(self) -> int:
        return max(wire.FRAG_MAX_MIN, min(wire.FRAG_MAX_MAX, self._transport().max_frame))

    # ── connect ──────────────────────────────────────────────────────────────

    async def open_legacy_session(self) -> bool:
        """Run the 2.x handshake with the given key; ``False`` when there is no 2.x key to use.

        Never over LAN: the device trusts frames there by origin (TLS or plaintext), and the
        handshake would encrypt them a second time.
        """
        if self._credential is None or isinstance(self._credential, DeviceKey):
            return False
        if isinstance(self._transport(), TcpTransport):
            return False
        await self._legacy.authenticate(bytes(self._credential))
        self._legacy_session_open = True
        return True

    async def establish(self, firmware_major: int, ident: DeviceIdent | None = None) -> None:
        """Pick and open the security scheme for this connection (called on entry).

        ``ident`` is the READ_IDENT answer on firmware 3.0, ``None`` when the device sent none.
        """
        if firmware_major < _SEC2_MIN_MAJOR:
            if not self._legacy_session_open:
                await self.open_legacy_session()
            return
        if ident is not None and ident.sec2 is False:
            if self._credential is not None:
                _LOGGER.warning("The display has no SEC2 module and is open; the key passed to connect() is not used")
            return
        status = await self.refresh_status()
        if status.state == SecState.LOCKED:
            raise DeviceLockedError(
                0x54, 0, int(SecErr.LOCKED), "The display is locked; factory-reset it (five quick power cycles)."
            )
        if status.state != SecState.CLAIMED:
            if self._credential is not None:
                _LOGGER.warning("The display is unclaimed and open; the key passed to connect() is not used")
            return
        if isinstance(self._transport(), TcpTransport):
            # § 12: SEC_* is BLE-only; a claimed display serves LAN over TLS with K_lan, which
            # this library does not derive yet (piece 4 follow-up).
            _LOGGER.warning(
                "SEC2 sessions are BLE only, and TLS with a firmware 3.0 key is not supported yet; "
                "over LAN this claimed display answers only plaintext commands"
            )
            return
        await self._open_session(status)
        if status.key_legacy and self._rotate_legacy_key:
            if self._store is None:
                _LOGGER.warning(
                    "The display's key was migrated from firmware 2.x; not rotating it without a key store "
                    "that can keep the new key"
                )
            else:
                await self.rotate_key()

    def _keys_to_try(self) -> list[bytes]:
        keys: list[bytes] = []
        if self._credential is not None and not isinstance(self._credential, LegacyKey):
            keys.append(bytes(self._credential))
        if self._store is not None:
            keys += [k for k in self._store.keys_to_try(self._name()) if k not in keys]
        return keys[:MAX_TRY]

    async def _open_session(self, status: SecStatus) -> None:
        stored_uid = self._store.uid(self._name()) if self._store is not None else None
        uid = stored_uid or status.uid  # the stored uid binds the key to this device (§ 4.3)
        keys = self._keys_to_try()
        for key in keys:
            try:
                session = await self._authenticate(key, uid)
            except Sec2AuthFailedError:
                continue
            self._install(session, key, uid)
            if self._store is not None:
                self._store.promote(self._name(), key, uid=uid)
            return
        raise Sec2KeyRequiredError(len(keys))

    def _install(self, session: Sec2Session, key: bytes, uid: bytes) -> None:
        channel = self._channel()
        channel.session = session
        self._key, self._uid = key, uid

        async def renew() -> None:
            channel.session = await self._authenticate(key, uid)

        channel.renew = renew

    # ── operations ───────────────────────────────────────────────────────────

    async def claim(self) -> ClaimFlow:
        """Start claiming this unclaimed device (it must be in claim mode); returns the flow.

        Run ``begin()``, ``exchange_nonces()``, then ``confirm(typed_code)``. The confirmed claim's
        session is installed on this connection, and the key promoted in the key store.

        Raises:
            ValueError: No key store: the new key would have nowhere to live.
        """
        if self._store is None:
            raise ValueError("Claiming needs a key store, or the new device key would be lost")
        status = await self.refresh_status()

        def on_session(session: Sec2Session) -> None:
            assert flow.k_dev is not None
            self._install(session, flow.k_dev, status.uid)

        flow = ClaimFlow(
            self._transport(),
            self._store,
            self._name(),
            status.uid,
            frag_max=self._frag_max(),
            on_session=on_session,
            on_end=self._note_redraw,
            redraw_keys=self._legacy._redraw_keys(),  # pylint: disable=protected-access
        )
        return flow

    async def rotate_key(self) -> bytes:
        """Replace the device key with a new device-generated one (needs a SEC2 session and a store)."""
        if self._store is None or self._key is None or self._uid is None:
            raise ValueError("Rotating a key needs an open SEC2 session and a key store")
        uid = self._uid

        async def reauthenticate(key: bytes) -> Sec2Session:
            session = await self._authenticate(key, uid)
            self._install(session, key, uid)
            return session

        new_key = await rotate_key(self._channel(), self._store, self._name(), uid, reauthenticate=reauthenticate)
        if self._key_shown():
            self._note_redraw()  # the boot screen is redrawn with the new key (SECURITY_V2 § 10.7)
        _LOGGER.warning("The key of %s was rotated; other clients that use this display need the new key", self._name())
        return new_key

    async def unclaim(self) -> None:
        """Remove the device's key: it becomes unclaimed and open (needs a SEC2 session)."""
        if self._store is None:
            raise ValueError("Unclaiming needs the key store that holds the key")
        await unclaim(self._channel(), self._store, self._name())
        self._key = self._uid = None

    async def use_without_key(self) -> None:
        """End claim mode and use the display without a key (it reboots into normal mode)."""
        await exit_claim_mode(self._transport(), frag_max=self._frag_max())
        self._note_redraw()  # claim mode ends: the device reboots into normal mode
