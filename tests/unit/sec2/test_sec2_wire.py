"""SEC2 wire layer: errors, handshake fragmentation, SEC_STATUS (SECURITY_V2.md § 3, § 5.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opendisplay.sec2 import wire
from opendisplay.sec2.errors import Sec2DeviceError, Sec2FragmentError
from opendisplay.sec2.wire import Reassembler, SecErr, SecState, fragment, parse_sec_error, parse_sec_status

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures" / "sec2_vectors.json").read_text())
_h = bytes.fromhex
_CMD = {"V3_claim": wire.CMD_SEC_CLAIM & 0xFF, "V4_auth": wire.CMD_SEC_AUTH & 0xFF}
_SUB = {
    "BEGIN": wire.ClaimSub.BEGIN,
    "NONCE": wire.ClaimSub.NONCE,
    "CONFIRM": wire.ClaimSub.CONFIRM,
    "ABORT": wire.ClaimSub.ABORT,
    "EXIT": wire.ClaimSub.EXIT,
    "HELLO": wire.AuthSub.HELLO,
    "FINISH": wire.AuthSub.FINISH,
}


def _messages() -> list[tuple[str, int, int, str, dict[str, Any]]]:
    out = []
    for group in ("V3_claim", "V4_auth"):
        for row in VECTORS[group]:
            for name, message in row["messages"].items():
                out.append((group, row["frag_max"], _SUB[name], name, message))
    return out


@pytest.mark.parametrize(("group", "frag_max", "sub", "name", "message"), _messages(), ids=lambda v: str(v)[:12])
def test_fragments_follow_the_deterministic_split(
    group: str, frag_max: int, sub: int, name: str, message: dict[str, Any]
) -> None:
    cmd_lo = _CMD[group]
    for direction in ("h2d", "d2h"):
        body = _h(message.get(f"{direction}_body", ""))
        frames = [_h(f) for f in message[direction]]

        assert fragment(cmd_lo, sub, body, frag_max) == frames
        reassembler = Reassembler(cmd_lo, sub, len(body), frag_max)
        results = [reassembler.feed(frame) for frame in frames]
        assert results[:-1] == [None] * (len(frames) - 1)
        assert results[-1] == body


def test_body_lengths_are_the_spec_tables() -> None:
    assert wire.body_length("h2d", wire.CMD_SEC_AUTH, wire.AuthSub.HELLO) == 82
    assert wire.body_length("d2h", wire.CMD_SEC_CLAIM, wire.ClaimSub.NONCE) == 44
    assert wire.body_length("d2h", wire.CMD_SEC_AUTH, wire.AuthSub.FINISH) == 18


def test_a_body_that_is_an_exact_multiple_ends_with_a_full_last_fragment() -> None:
    frames = fragment(0x56, 1, bytes(32), 20)  # 16 bytes per fragment

    assert [len(f) - 4 for f in frames] == [16, 16]
    assert frames[-1][3] == 0x81


def test_an_empty_body_is_one_empty_last_fragment() -> None:
    assert fragment(0x55, 4, b"", 20) == [bytes([0x00, 0x55, 0x04, 0x80])]


def test_frag_max_is_checked() -> None:
    with pytest.raises(ValueError, match="20..244"):
        fragment(0x55, 1, b"x", 19)


@pytest.mark.parametrize(
    "frames",
    [
        [bytes([0, 0x56, 1, 0x01]) + bytes(16)],  # index 1 first: out of order
        [
            bytes([0, 0x56, 1, 0x00]) + bytes(16),
            bytes([0, 0x56, 1, 0x81]) + bytes(16),
            bytes([0, 0x56, 1, 0x82]) + bytes(16),
        ],
        [bytes([0, 0x56, 1, 0x00]) + bytes(16), bytes([0, 0x56, 1, 0x81]) + bytes(4)],  # short LAST
        [bytes([0, 0x56, 1, 0x00]) + bytes(10)],  # a non-LAST fragment that is not frag_max - 4
        [bytes([0, 0x56, 2, 0x00]) + bytes(16)],  # another sub, otherwise a valid first fragment
    ],
    ids=["out-of-order", "too-long", "short-last", "non-deterministic", "wrong-sub"],
)
def test_malformed_fragments_raise(frames: list[bytes]) -> None:
    reassembler = Reassembler(0x56, 1, 36, 20)  # a 36-byte body: 16 + 16 + 4

    with pytest.raises(Sec2FragmentError):
        for frame in frames:
            reassembler.feed(frame)


def test_a_device_error_during_reassembly_raises_it() -> None:
    reassembler = Reassembler(0x56, 1, 81, 20)

    with pytest.raises(Sec2DeviceError) as err:
        reassembler.feed(bytes([0xFF, 0x56, 0x01, 0x07]))

    assert (err.value.err, err.value.sub) == (SecErr.AUTH_FAILED, 1)


def test_frames_for_other_commands_are_ignored() -> None:
    reassembler = Reassembler(0x56, 1, 16, 20)

    assert reassembler.feed(bytes.fromhex("0071")) is None
    assert reassembler.feed(bytes([0, 0x56, 1, 0x80]) + bytes(16)) == bytes(16)


def test_parse_sec_error() -> None:
    assert parse_sec_error(bytes([0xFF, 0x55, 0x01, 0x04])) == (0x55, 0x01, SecErr.NOT_IN_CLAIM_MODE)
    assert parse_sec_error(bytes([0xFF, 0x70, 0x01, 0x04])) is None  # not a SEC opcode
    assert parse_sec_error(bytes([0xFF, 0x55, 0x01])) is None


def test_sec_status_of_the_e1002_in_fresh_claim_mode() -> None:
    # Real frame: E1002 OD4064C9, Firmware_Unified 3.0.2, no config, fresh claim mode
    # (scratch/captures-OD4064C9-fw3.0.2.md). Decoded by hand against § 5.1.
    status = parse_sec_status(_h("005402002135020000441bf6c96440100e0803"))

    assert status.sec_ver == 2
    assert status.state is SecState.UNCLAIMED
    assert status.claim_mode and status.claim_mode_fresh and not status.session_active
    assert (status.window_s, status.timeout_s, status.renders_left) == (565, 3600, 3)
    assert status.uid == _h("0000441bf6c96440")  # 00 00 + the board's MAC
    assert status.dflags == 0x08 and not status.has_panel


def test_a_short_or_foreign_status_frame_raises() -> None:
    from opendisplay.exceptions import InvalidResponseError

    with pytest.raises(InvalidResponseError):
        parse_sec_status(_h("0054020021"))
    with pytest.raises(InvalidResponseError):
        parse_sec_status(_h("0043") + bytes(17))


def test_record_nonce() -> None:
    assert wire.record_nonce(wire.DIR_H2D, 1) == b"H" + (1).to_bytes(4, "little") + bytes(8)
    assert wire.record_nonce(wire.DIR_D2H, 0x01020304) == b"D" + bytes([4, 3, 2, 1]) + bytes(8)


def test_the_plaintext_set() -> None:
    assert wire.is_plaintext_opcode(0x0054) and wire.is_plaintext_opcode(0x0046)
    assert not wire.is_plaintext_opcode(0x0057) and not wire.is_plaintext_opcode(0x0070)
