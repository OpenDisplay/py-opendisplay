"""Rotating and unclaiming a claimed device: CMD_SEC_KEY 0x0057, sealed only (SECURITY_V2.md § 11).

The device does the work and persists first, then replies. Rotation follows the claim's
pending/confirm pattern on the client too: the new key is staged before ROTATE_CONFIRM, and the
old key is dropped only after a session with the new key has opened (§ 8.8).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

from ..exceptions import InvalidResponseError
from . import kdf, wire
from .errors import Sec2DeviceError, Sec2Error, typed_device_error
from .keystore import KeyStore
from .session import Sec2Session

if TYPE_CHECKING:
    from ..device.channel import SecureChannel

_LOGGER = logging.getLogger(__name__)
_CMD: Final = wire.CMD_SEC_KEY.to_bytes(2, "big")
TIMEOUT_KEY: Final = 10.0  # the device persists before it answers
_KEY_LEN: Final = 16


def _sec2_session(channel: SecureChannel) -> Sec2Session:
    session = channel.session
    if not isinstance(session, Sec2Session):
        raise Sec2Error("Key rotation and unclaim need a SEC2 session; authenticate first")
    return session


async def _command(channel: SecureChannel, sub: int, body: bytes = b"") -> bytes:
    """Send one sealed CMD_SEC_KEY and return the data of its success answer."""
    await channel.write(_CMD + bytes([sub]) + body)
    reply = await channel.read(TIMEOUT_KEY)
    error = wire.parse_sec_error(reply)
    if error is not None:
        raise typed_device_error(Sec2DeviceError(error[0], error[1], int(error[2])))
    if len(reply) < 3 or reply[0] != 0x00 or reply[1] != _CMD[1] or reply[2] != sub:
        raise InvalidResponseError(f"Unexpected answer to SEC_KEY sub {sub}: {reply.hex()}")
    return reply[3:]


async def rotate_key(
    channel: SecureChannel,
    store: KeyStore,
    name: str,
    uid: bytes,
    *,
    reauthenticate: Callable[[bytes], Awaitable[Sec2Session]],
) -> bytes:
    """Replace the device key with a new device-generated one; returns the new key.

    Order: ROTATE (receive K_new) → stage K_new → ROTATE_CONFIRM (tag bound to this session's
    sid) → the device tears the session down → ``reauthenticate(K_new)`` → promote K_new.
    Any failure before the promotion leaves both keys in the store, tried in order next time.

    Raises:
        Sec2Error: There is no SEC2 session.
        Sec2DeviceError: The device refused (typed), the old key is still in force or both are kept.
        Sec2StorageError: K_new could not be stored; ROTATE_CONFIRM was not sent.
    """
    session = _sec2_session(channel)
    sid = session.sid
    new_key = (await _command(channel, wire.KeySub.ROTATE))[:16]
    if len(new_key) != _KEY_LEN:
        raise InvalidResponseError("ROTATE answered without a 16-byte key")
    store.stage(name, new_key, "rotation")
    k_conf_new = kdf.key_hierarchy(new_key, uid).k_conf
    await _command(channel, wire.KeySub.ROTATE_CONFIRM, kdf.kdf(k_conf_new, "OD2/rotate-c", uid + sid))
    channel.reset()  # the device tears the session down after ROTATE_CONFIRM (§ 9.6)
    channel.session = await reauthenticate(new_key)
    store.promote(name, new_key, uid=uid)
    _LOGGER.info("Rotated the device key of %s", name)
    return new_key


async def unclaim(channel: SecureChannel, store: KeyStore, name: str) -> None:
    """Remove the device's key: it becomes UNCLAIMED, in normal mode, and open.

    The key is forgotten only after the device's sealed success (an authenticated answer).

    Raises:
        Sec2Error: There is no SEC2 session.
        Sec2DeviceError: The device refused (for example STORAGE_ERROR); it is still claimed.
    """
    _sec2_session(channel)
    await _command(channel, wire.KeySub.UNCLAIM, wire.UNCLAIM_GUARD)
    channel.reset()
    store.forget(name)
    _LOGGER.info("Unclaimed %s", name)
