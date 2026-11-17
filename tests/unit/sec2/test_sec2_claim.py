"""Claiming a firmware 3.0 device: CMD_SEC_CLAIM (SECURITY_V2.md § 8; vectors V3_claim)."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from opendisplay.sec2 import errors, wire
from opendisplay.sec2.claim import ClaimFlow, exit_claim_mode
from opendisplay.sec2.keystore import MemoryKeyStore
from opendisplay.sec2.primitives import key_pair_from_scalar

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures" / "sec2_vectors.json").read_text())
_h = bytes.fromhex
NAME = "OD4064C9"


class _ScriptedTransport:
    """Answers each LAST fragment with the next scripted reply frames."""

    max_frame = 244
    supports_write_without_response = True

    def __init__(self, replies: list[list[bytes]]) -> None:
        self.written: list[bytes] = []
        self._replies = deque(replies)
        self._queue: deque[bytes] = deque()

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)
        if data[3] & wire.FRAG_LAST:
            self._queue.extend(self._replies.popleft() if self._replies else [])

    async def read_response(self, timeout: float = 5.0) -> bytes:
        return self._queue.popleft()

    def drain_notifications(self) -> int:
        return 0


def _flow(row: dict[str, Any], transport: _ScriptedTransport, store: MemoryKeyStore) -> ClaimFlow:
    i = row["inputs"]
    return ClaimFlow(
        transport,  # type: ignore[arg-type]
        store,
        NAME,
        _h(i["uid"]),
        frag_max=row["frag_max"],
        _key_pair=key_pair_from_scalar(_h(i["dc"])),
        _nc=_h(i["Nc"]),
    )


def _replies(row: dict[str, Any], *names: str) -> list[list[bytes]]:
    return [[_h(f) for f in row["messages"][name]["d2h"]] for name in names]


@pytest.mark.parametrize("row", VECTORS["V3_claim"], ids=lambda r: f"frag{r['frag_max']}-{r['inputs']['claim_mode']}")
async def test_a_claim_matches_the_vectors_end_to_end(row: dict[str, Any]) -> None:
    store = MemoryKeyStore()
    transport = _ScriptedTransport(_replies(row, "BEGIN", "NONCE", "CONFIRM"))
    flow = _flow(row, transport, store)

    await flow.begin()
    code = await flow.exchange_nonces()
    assert (code.code, code.display) == (row["code"], row["code_display"])
    assert flow.code_matches(row["code_display"])
    session = await flow.confirm(row["code_display"])

    messages = row["messages"]
    assert transport.written == [
        _h(f) for f in messages["BEGIN"]["h2d"] + messages["NONCE"]["h2d"] + messages["CONFIRM"]["h2d"]
    ]
    assert (session.k_h2d, session.k_d2h, session.sid) == (_h(row["k_h2d"]), _h(row["k_d2h"]), _h(row["sid"]))
    assert store.keys_to_try(NAME) == [_h(row["inputs"]["K_new"])]
    assert store.uid(NAME) == _h(row["inputs"]["uid"])
    assert flow.k_dev == _h(row["inputs"]["K_new"])


async def test_a_wrong_code_is_never_confirmed() -> None:
    row = VECTORS["V3_claim"][0]
    store = MemoryKeyStore()
    transport = _ScriptedTransport([*_replies(row, "BEGIN", "NONCE"), [_h(f) for f in row["messages"]["ABORT"]["d2h"]]])
    flow = _flow(row, transport, store)
    await flow.begin()
    await flow.exchange_nonces()
    wrong = "000000" if row["code"] != "000000" else "111111"

    with pytest.raises(errors.Sec2CodeMismatchError):
        await flow.confirm(wrong)

    assert transport.written[-1] == _h(row["messages"]["ABORT"]["h2d"][0])  # ABORT, not CONFIRM
    assert all(f[2] != wire.ClaimSub.CONFIRM for f in transport.written)
    assert store.keys_to_try(NAME) == []  # a wrong code keeps nothing


async def test_a_broken_commitment_aborts() -> None:
    row = VECTORS["V3_claim"][0]
    nonce_body = bytearray(_h(row["messages"]["NONCE"]["d2h_body"]))
    nonce_body[0] ^= 0x01  # Nd no longer opens Cd
    transport = _ScriptedTransport(
        [
            _replies(row, "BEGIN")[0],
            wire.fragment(0x55, 2, bytes(nonce_body), row["frag_max"]),
            [bytes([0x00, 0x55, 0x04, 0x80])],
        ]
    )
    flow = _flow(row, transport, MemoryKeyStore())
    await flow.begin()

    with pytest.raises(errors.Sec2InterferenceError, match="commitment"):
        await flow.exchange_nonces()
    assert transport.written[-1][2] == wire.ClaimSub.ABORT


async def test_the_key_is_staged_before_confirm_and_a_store_failure_aborts() -> None:
    row = VECTORS["V3_claim"][0]

    class _BrokenStore(MemoryKeyStore):
        def stage(self, name: str, key: bytes, kind: str) -> None:
            raise errors.Sec2StorageError("disk full")

    transport = _ScriptedTransport([*_replies(row, "BEGIN", "NONCE"), [bytes([0x00, 0x55, 0x04, 0x80])]])
    flow = _flow(row, transport, _BrokenStore())
    await flow.begin()
    await flow.exchange_nonces()

    with pytest.raises(errors.Sec2StorageError):
        await flow.confirm(row["code"])
    assert all(f[2] != wire.ClaimSub.CONFIRM for f in transport.written)
    assert transport.written[-1][2] == wire.ClaimSub.ABORT


async def test_an_unverified_device_proof_keeps_the_key_as_a_candidate() -> None:
    row = VECTORS["V3_claim"][0]
    confirm_body = bytearray(_h(row["messages"]["CONFIRM"]["d2h_body"]))
    confirm_body[0] ^= 0x01  # a wrong tag_d
    store = MemoryKeyStore()
    transport = _ScriptedTransport(
        [*_replies(row, "BEGIN", "NONCE"), wire.fragment(0x55, 3, bytes(confirm_body), row["frag_max"])]
    )
    flow = _flow(row, transport, store)
    await flow.begin()
    await flow.exchange_nonces()

    with pytest.raises(errors.Sec2ClaimUnconfirmedError):
        await flow.confirm(row["code"])
    assert store.keys_to_try(NAME) == [_h(row["inputs"]["K_new"])]  # kept: rule 4 of § 8.8
    assert store.uid(NAME) is None  # not confirmed


@pytest.mark.parametrize(
    ("err", "error"),
    [
        (wire.SecErr.NOT_IN_CLAIM_MODE, errors.NotInClaimModeError),
        (wire.SecErr.ALREADY_CLAIMED, errors.AlreadyClaimedError),
        (wire.SecErr.ATTEMPTS_EXHAUSTED, errors.ClaimAttemptsExhaustedError),
        (wire.SecErr.LOCKED, errors.DeviceLockedError),
        (wire.SecErr.RATE_LIMITED, errors.RateLimitedError),
        (wire.SecErr.BUSY, errors.Sec2BusyError),
    ],
)
async def test_each_device_refusal_is_typed_and_says_what_to_do(
    err: wire.SecErr, error: type[Exception]
) -> None:  # Review Focus 6
    row = VECTORS["V3_claim"][0]
    transport = _ScriptedTransport([[bytes([0xFF, 0x55, 0x01, int(err)])]])
    flow = _flow(row, transport, MemoryKeyStore())

    with pytest.raises(error) as raised:
        await flow.begin()
    assert len(str(raised.value)) > 20  # a sentence for the user, not just a code


async def test_no_panel_claim_needs_an_explicit_acknowledgement() -> None:  # § 8.6
    row = next(r for r in VECTORS["V3_claim"])
    store = MemoryKeyStore()
    transport = _ScriptedTransport(_replies(row, "BEGIN", "NONCE", "CONFIRM"))
    flow = _flow(row, transport, store)
    await flow.begin()
    await flow.exchange_nonces()

    with pytest.raises(ValueError, match="acknowledge"):
        await flow.confirm_without_code(acknowledged=False)
    session = await flow.confirm_without_code(acknowledged=True)

    assert session.sid == _h(row["sid"])
    assert store.keys_to_try(NAME) == [_h(row["inputs"]["K_new"])]


async def test_exit_claim_mode_sends_exit() -> None:
    row = VECTORS["V3_claim"][0]
    transport = _ScriptedTransport([[_h(f) for f in row["messages"]["EXIT"]["d2h"]]])

    await exit_claim_mode(transport, frag_max=row["frag_max"])  # type: ignore[arg-type]

    assert transport.written == [_h(f) for f in row["messages"]["EXIT"]["h2d"]]
