"""Key rotation and unclaim: CMD_SEC_KEY, sealed only (SECURITY_V2.md § 11; vectors V6_rotation)."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from opendisplay.device.channel import SecureChannel
from opendisplay.exceptions import OpenDisplayTimeoutError
from opendisplay.sec2 import errors, kdf
from opendisplay.sec2.keys import rotate_key, unclaim
from opendisplay.sec2.keystore import MemoryKeyStore
from opendisplay.sec2.session import Sec2Session, seal_d2h

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures" / "sec2_vectors.json").read_text())
V6 = VECTORS["V6_rotation"]
_h = bytes.fromhex
NAME = "OD4064C9"
OLD, NEW, UID = _h(V6["k_dev_old"]), _h(V6["K_new"]), _h(V6["uid"])


class _Transport:
    max_frame = 244

    def __init__(self, replies: list[bytes | Exception]) -> None:
        self.written: list[bytes] = []
        self._replies = deque(replies)

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)

    async def read_response(self, timeout: float = 5.0) -> bytes:
        item = self._replies.popleft()
        if isinstance(item, Exception):
            raise item
        return item


def _setup(replies: list[bytes | Exception], send: int = 0) -> tuple[SecureChannel, _Transport, MemoryKeyStore]:
    sealed = V6["sealed"]
    transport = _Transport(replies)
    channel = SecureChannel(transport)  # type: ignore[arg-type]
    channel.session = Sec2Session(
        k_h2d=_h(sealed["k_h2d"]), k_d2h=_h(sealed["k_d2h"]), sid=_h(V6["sid"]), send_counter=send
    )
    store = MemoryKeyStore()
    store.promote(NAME, OLD, uid=UID)
    return channel, transport, store


def test_the_rotation_tag_matches_the_vector() -> None:
    k_conf_new = kdf.key_hierarchy(NEW, UID).k_conf

    assert k_conf_new == _h(V6["K_conf_new"])
    assert kdf.kdf(k_conf_new, "OD2/rotate-c", UID + _h(V6["sid"])) == _h(V6["rotate_tag"])


async def test_rotate_key_matches_the_vectors_and_promotes_only_after_a_session_with_the_new_key() -> None:
    sealed = V6["sealed"]
    channel, transport, store = _setup([_h(sealed["ROTATE_reply_ctr1"]), _h(sealed["ROTATE_CONFIRM_reply_ctr2"])])
    reauthenticated: list[bytes] = []

    async def reauth(key: bytes) -> Sec2Session:
        assert store.keys_to_try(NAME) == [OLD, NEW]  # both held while the new key is unproven
        reauthenticated.append(key)
        return Sec2Session(k_h2d=bytes(16), k_d2h=bytes(16))

    new_key = await rotate_key(channel, store, NAME, UID, reauthenticate=reauth)

    assert transport.written == [_h(sealed["ROTATE_request_ctr1"]), _h(sealed["ROTATE_CONFIRM_request_ctr2"])]
    assert new_key == NEW and reauthenticated == [NEW]
    assert store.keys_to_try(NAME) == [NEW]
    assert channel.session is not None and channel.session.k_h2d == bytes(16)  # the new session


async def test_a_lost_rotate_reply_leaves_the_old_key() -> None:  # Review Focus 5
    channel, _, store = _setup([OpenDisplayTimeoutError("lost")])

    with pytest.raises(OpenDisplayTimeoutError):
        await rotate_key(channel, store, NAME, UID, reauthenticate=pytest.fail)  # type: ignore[arg-type]
    assert store.keys_to_try(NAME) == [OLD]


async def test_a_lost_rotate_confirm_reply_keeps_both_keys() -> None:  # Review Focus 5
    sealed = V6["sealed"]
    channel, _, store = _setup([_h(sealed["ROTATE_reply_ctr1"]), OpenDisplayTimeoutError("lost")])

    with pytest.raises(OpenDisplayTimeoutError):
        await rotate_key(channel, store, NAME, UID, reauthenticate=pytest.fail)  # type: ignore[arg-type]
    assert store.keys_to_try(NAME) == [OLD, NEW]


async def test_a_failed_handshake_with_the_new_key_keeps_both_keys() -> None:  # Review Focus 5
    sealed = V6["sealed"]
    channel, _, store = _setup([_h(sealed["ROTATE_reply_ctr1"]), _h(sealed["ROTATE_CONFIRM_reply_ctr2"])])

    async def reauth(key: bytes) -> Sec2Session:
        raise errors.Sec2AuthFailedError(0x56, 2, 0x07)

    with pytest.raises(errors.Sec2AuthFailedError):
        await rotate_key(channel, store, NAME, UID, reauthenticate=reauth)
    assert store.keys_to_try(NAME) == [OLD, NEW]
    assert channel.session is None  # the device tore the old session down after ROTATE_CONFIRM


async def test_a_refused_rotate_confirm_is_typed_and_keeps_both_keys() -> None:
    sealed = V6["sealed"]
    refusal = seal_d2h(_h(sealed["k_d2h"]), 2, bytes([0xFF, 0x57, 0x02, 0x07]))
    channel, _, store = _setup([_h(sealed["ROTATE_reply_ctr1"]), refusal])

    with pytest.raises(errors.Sec2AuthFailedError):
        await rotate_key(channel, store, NAME, UID, reauthenticate=pytest.fail)  # type: ignore[arg-type]
    assert store.keys_to_try(NAME) == [OLD, NEW]


async def test_rotation_needs_a_sec2_session() -> None:
    channel, _, store = _setup([])
    channel.session = None

    with pytest.raises(errors.Sec2Error, match="session"):
        await rotate_key(channel, store, NAME, UID, reauthenticate=pytest.fail)  # type: ignore[arg-type]


async def test_unclaim_matches_the_vector_and_forgets_the_key_after_the_sealed_success() -> None:
    sealed = V6["sealed"]
    success = seal_d2h(_h(sealed["k_d2h"]), 3, bytes([0x00, 0x57, 0x03]))
    channel, transport, store = _setup([success], send=2)

    await unclaim(channel, store, NAME)

    assert transport.written == [_h(sealed["UNCLAIM_request_ctr3"])]
    assert store.keys_to_try(NAME) == []
    assert channel.session is None


async def test_a_refused_unclaim_keeps_the_key() -> None:
    sealed = V6["sealed"]
    refusal = seal_d2h(_h(sealed["k_d2h"]), 3, bytes([0xFF, 0x57, 0x03, 0x0B]))  # STORAGE_ERROR
    channel, _, store = _setup([refusal], send=2)

    with pytest.raises(errors.Sec2DeviceError):
        await unclaim(channel, store, NAME)
    assert store.keys_to_try(NAME) == [OLD]
