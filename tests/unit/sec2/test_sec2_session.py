"""The SEC2 sealed session and its handshake (SECURITY_V2.md § 9; vectors V4_auth and V5_records)."""

from __future__ import annotations

import json
import secrets
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from opendisplay.device.channel import SecureChannel
from opendisplay.sec2 import kdf, wire
from opendisplay.sec2.errors import (
    Sec2AuthFailedError,
    Sec2MutualAuthError,
    Sec2RecordError,
    Sec2SessionLostError,
)
from opendisplay.sec2.primitives import ecdh, generate_key_pair, key_pair_from_scalar
from opendisplay.sec2.session import Sec2Session, authenticate, open_h2d, seal_d2h

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures" / "sec2_vectors.json").read_text())
V5 = VECTORS["V5_records"]
_h = bytes.fromhex


def _session(send: int = 0, recv: int = 0) -> Sec2Session:
    return Sec2Session(k_h2d=_h(V5["k_h2d"]), k_d2h=_h(V5["k_d2h"]), send_counter=send, recv_counter=recv)


# ── records ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("row", V5["h2d"], ids=lambda r: f"{r['cmd']}-{r['ctr']}")
def test_seal_matches_the_vectors(row: dict[str, Any]) -> None:
    session = _session(send=row["ctr"] - 1)
    cmd = int(row["cmd"], 16)

    assert session.seal(cmd.to_bytes(2, "big") + _h(row["payload"])) == _h(row["frame"])
    assert session.send_counter == row["ctr"]


@pytest.mark.parametrize("row", V5["d2h"], ids=lambda r: r["name"])
def test_open_matches_the_vectors(row: dict[str, Any]) -> None:
    session = _session(recv=row["ctr"] - 1)

    assert session.is_sealed(_h(row["frame"]))
    assert session.open(_h(row["frame"])) == _h(row["plaintext"])
    assert session.recv_counter == row["ctr"]


def test_the_first_frame_uses_counter_1_and_an_empty_payload_seals_to_18_bytes() -> None:
    sealed = _session().seal(b"\x00\x40")

    assert sealed[2:6] == (1).to_bytes(4, "little")
    assert len(sealed) == 18


def test_a_repeated_or_lower_device_counter_is_rejected() -> None:
    row = V5["d2h"][0]
    session = _session(recv=row["ctr"])  # this counter was already accepted

    with pytest.raises(Sec2RecordError, match="counter"):
        session.open(_h(row["frame"]))
    assert session.failures == 0  # not counted as a forgery


def test_three_bad_tags_end_the_session() -> None:  # Review Focus 2
    row = V5["d2h"][0]
    session = _session()
    bad = bytearray(_h(row["frame"]))
    bad[-1] ^= 0x01

    for _ in range(2):
        with pytest.raises(Sec2RecordError, match="integrity"):
            session.open(bytes(bad))
    with pytest.raises(Sec2SessionLostError):
        session.open(bytes(bad))


def test_plaintext_set_opcodes_are_sent_unsealed() -> None:
    session = _session()

    assert session.seal(b"\x00\x54") == b"\x00\x54"
    assert session.send_counter == 0


@pytest.mark.parametrize(
    ("frame", "allowed"),
    [
        (_h("005402002135020000441bf6c96440100e0803"), True),  # SEC_STATUS: plaintext set
        (_h("0043") + bytes(10), True),  # firmware version
        (_h("ff550104"), True),  # a SEC error, advisory
        (_h("0070"), False),  # a direct-write ACK must be sealed inside a session
        (_h("00400000"), False),  # a config reply must be sealed
    ],
)
def test_only_the_plaintext_frames_of_9_5_are_accepted(frame: bytes, allowed: bool) -> None:  # Review Focus 3
    assert _session().allows_plaintext(frame) is allowed


def test_pipe_data_size_and_direct_chunk_size() -> None:
    session = _session()

    assert session.pipe_data_size(244) == 244 - 18 - 1
    assert session.direct_chunk_size == 154


def test_session_keys_are_not_in_the_repr() -> None:
    session = _session()

    assert V5["k_h2d"] not in repr(session) and V5["k_d2h"] not in repr(session)


# ── the handshake against the vectors ─────────────────────────────────────────


class _ScriptedTransport:
    """Answers each LAST fragment with the next scripted reply frames."""

    max_frame = 244
    supports_write_without_response = True

    def __init__(self, replies: list[list[bytes]]) -> None:
        self.written: list[bytes] = []
        self._replies = deque(replies)
        self._queue: deque[bytes] = deque()
        self.drained = 0

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)
        if data[3] & wire.FRAG_LAST:
            self._queue.extend(self._replies.popleft())

    async def read_response(self, timeout: float = 5.0) -> bytes:
        return self._queue.popleft()

    def drain_notifications(self) -> int:
        self.drained += 1
        return 0


@pytest.mark.parametrize("row", VECTORS["V4_auth"], ids=lambda r: f"frag{r['frag_max']}-{r['inputs']['origin']}")
async def test_authenticate_matches_the_vectors(row: dict[str, Any]) -> None:
    i, messages = row["inputs"], row["messages"]
    transport = _ScriptedTransport(
        [[_h(f) for f in messages["HELLO"]["d2h"]], [_h(f) for f in messages["FINISH"]["d2h"]]]
    )

    session = await authenticate(
        transport,  # type: ignore[arg-type]
        _h(i["k_dev"]),
        _h(i["uid"]),
        frag_max=row["frag_max"],
        _key_pair=key_pair_from_scalar(_h(i["dc"])),
        _nc=_h(i["Nc"]),
    )

    assert transport.written == [_h(f) for f in messages["HELLO"]["h2d"] + messages["FINISH"]["h2d"]]
    assert (session.k_h2d, session.k_d2h, session.sid) == (_h(row["k_h2d"]), _h(row["k_d2h"]), _h(row["sid"]))
    assert (session.send_counter, session.recv_counter, session.timeout_s) == (0, 0, i["session_timeout_s"])
    assert transport.drained == 1  # pending notifications drained before HELLO (§ 9.7 rule 3)


async def test_a_wrong_key_opens_no_session() -> None:  # Review Focus 1
    row = VECTORS["V4_auth"][0]
    i, messages = row["inputs"], row["messages"]
    # The device rejects our proof: FINISH is answered with AUTH_FAILED.
    transport = _ScriptedTransport([[_h(f) for f in messages["HELLO"]["d2h"]], [bytes([0xFF, 0x56, 0x02, 0x07])]])

    with pytest.raises(Sec2AuthFailedError):
        await authenticate(transport, bytes(16), _h(i["uid"]), frag_max=row["frag_max"])  # type: ignore[arg-type]


async def test_a_device_that_cannot_prove_itself_is_refused() -> None:
    row = VECTORS["V4_auth"][0]
    i, messages = row["inputs"], row["messages"]
    finish = bytearray(_h(messages["FINISH"]["d2h_body"]))
    finish[0] ^= 0x01  # a wrong tag_d
    reply = wire.fragment(0x56, 2, bytes(finish), row["frag_max"])
    transport = _ScriptedTransport([[_h(f) for f in messages["HELLO"]["d2h"]], reply])

    with pytest.raises(Sec2MutualAuthError):
        await authenticate(
            transport,  # type: ignore[arg-type]
            _h(i["k_dev"]),
            _h(i["uid"]),
            frag_max=row["frag_max"],
            _key_pair=key_pair_from_scalar(_h(i["dc"])),
            _nc=_h(i["Nc"]),
        )


# ── a reference device: full handshake with fresh keys, then sealed traffic ─────


class _RefDevice:
    """Enough of a SEC2 device to answer HELLO and FINISH and to seal replies (port of the
    website's tests/helpers/sec2-refdevice.js, the parts this test needs)."""

    max_frame = 244
    supports_write_without_response = True

    def __init__(self, k_dev: bytes, uid: bytes, dflags: int = 0x01) -> None:
        self.k_dev, self.uid, self.dflags = k_dev, uid, dflags
        self._parts: list[bytes] = []
        self._queue: deque[bytes] = deque()
        self.session: Sec2Session | None = None
        self._pending: dict[str, Any] = {}
        self.tx_ctr = 0

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        if self.session is not None and data[:2] not in (b"\x00\x54", b"\x00\x56"):
            plaintext = open_h2d(self.session.k_h2d, data)  # what the device would see
            self.tx_ctr += 1
            self._queue.append(seal_d2h(self.session.k_d2h, self.tx_ctr, b"\x00" + data[1:2] + b"OK" + plaintext))
            return
        self._parts.append(data[4:])
        if not data[3] & wire.FRAG_LAST:
            return
        body, self._parts = b"".join(self._parts), []
        if data[2] == wire.AuthSub.HELLO:
            qc, nc = body[2:66], body[66:82]
            dd, qd = generate_key_pair()
            nd = secrets.token_bytes(16)
            ta = kdf.auth_transcript(self.dflags, self.uid, qc, qd, nc, nd)
            prk = kdf.extract(kdf.key_hierarchy(self.k_dev, self.uid).k_ext, ecdh(dd, qc))
            self._pending = {"ta": ta, "prk": prk}
            self._queue.extend(wire.fragment(0x56, 1, bytes([self.dflags]) + qd + nd, body[1]))
        else:
            ta, prk = self._pending["ta"], self._pending["prk"]
            if body != kdf.kdf(prk, "OD2/auth-c", ta):
                self._queue.append(bytes([0xFF, 0x56, 0x02, 0x07]))
                return
            keys = kdf.session_keys(prk, ta)
            self.session = Sec2Session(k_h2d=keys.k_h2d, k_d2h=keys.k_d2h)
            tag_d = kdf.kdf(prk, "OD2/auth-d", ta)
            self._queue.extend(wire.fragment(0x56, 2, tag_d + (3600).to_bytes(2, "little"), 20))

    async def read_response(self, timeout: float = 5.0) -> bytes:
        return self._queue.popleft()

    def drain_notifications(self) -> int:
        return 0


async def test_full_handshake_and_sealed_traffic_with_a_reference_device() -> None:
    k_dev, uid = secrets.token_bytes(16), secrets.token_bytes(8)
    device = _RefDevice(k_dev, uid)

    session = await authenticate(device, k_dev, uid, frag_max=20)  # type: ignore[arg-type]
    channel = SecureChannel(device)  # type: ignore[arg-type]
    channel.session = session
    await channel.write(b"\x00\x40")
    reply = await channel.read(5.0)

    assert reply == b"\x00\x40OK"
    assert session.send_counter == 1 and session.recv_counter == 1


async def test_a_wrong_key_against_the_reference_device() -> None:
    device = _RefDevice(secrets.token_bytes(16), bytes(8))

    with pytest.raises(Sec2AuthFailedError):
        await authenticate(device, secrets.token_bytes(16), bytes(8), frag_max=20)  # type: ignore[arg-type]
    assert device.session is None


async def test_the_channel_drops_plaintext_a_sec2_session_does_not_allow() -> None:
    class _Transport:
        max_frame = 244
        frames = deque([b"\x00\x70", b"\x00\x43\x02\x00\x00"])  # a bare ACK (dropped), then firmware version

        async def read_response(self, timeout: float = 5.0) -> bytes:
            return self.frames.popleft()

    channel = SecureChannel(_Transport())  # type: ignore[arg-type]
    channel.session = _session()

    assert await channel.read(5.0) == b"\x00\x43\x02\x00\x00"


async def test_a_legacy_session_still_accepts_every_plaintext_reply() -> None:
    from opendisplay.device.channel import LegacySession

    class _Transport:
        max_frame = 244

        async def read_response(self, timeout: float = 5.0) -> bytes:
            return b"\x00\x70"

    channel = SecureChannel(_Transport())  # type: ignore[arg-type]
    channel.session = LegacySession(session_key=bytes(16), session_id=bytes(8))

    assert await channel.read(5.0) == b"\x00\x70"


def test_the_start_payload_limit_comes_from_the_session() -> None:
    class _Transport:
        max_frame = 244

    channel = SecureChannel(_Transport())  # type: ignore[arg-type]
    assert channel.start_payload_limit() == 200  # MAX_START_PAYLOAD without a session
    session = _session()
    session.direct_chunk_size = 120
    channel.session = session
    assert channel.start_payload_limit() == 120


def test_a_legacy_session_key_is_not_in_the_repr() -> None:
    from opendisplay.device.channel import LegacySession

    session = LegacySession(session_key=bytes(range(16)), session_id=bytes(range(8)))

    assert bytes(range(16)).hex() not in repr(session) and "session_key" not in repr(session)


async def test_a_sec2_session_renews_through_its_hook_at_90_percent_of_its_lifetime() -> None:
    import time

    class _Transport:
        max_frame = 244
        written: list[bytes] = []

        async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
            self.written.append(data)

    channel = SecureChannel(_Transport(), reauth_key=bytes(16))  # type: ignore[arg-type]
    session = _session()
    session.timeout_s, session.established_at = 100, time.monotonic() - 91
    channel.session = session
    renewed: list[bool] = []

    async def renew() -> None:
        renewed.append(True)

    channel.renew = renew
    await channel.write(b"\x00\x40")
    assert renewed == [True]  # not the 2.x handshake, although a 2.x reauth key is set

    session.established_at = time.monotonic()
    await channel.write(b"\x00\x40")
    assert renewed == [True]  # young session: no renewal
