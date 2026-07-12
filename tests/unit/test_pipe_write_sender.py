"""Unit tests for the PIPE_WRITE sliding-window sender loop.

Exercises _send_pipe_chunks / _run_pipe_upload / _await_pipe_end_ack directly:
happy path, span-window invariant, seq wrap past 255, selective-repeat loss
recovery, rewind fallback, retransmit pacing, lost-ACK supersession, PTO/RETX
aborts, encryption specifics, and drain_stale enforcement.
"""

from __future__ import annotations

import pytest
from test_pipe_write import (
    END_ACK,
    END_NACK,
    REFRESH_COMPLETE,
    ScriptedConn,
    data_ack,
    data_nack,
    make_device,
    start_ack,
)

from opendisplay.crypto import decrypt_response, derive_session_id, derive_session_key
from opendisplay.exceptions import BLETimeoutError, ProtocolError
from opendisplay.models.enums import RefreshMode
from opendisplay.protocol import PipeParams
from opendisplay.protocol.commands import ENCRYPTED_CHUNK_SIZE


def _params(
    window: int = 4,
    ack_every: int = 2,
    max_frame: int = 244,
    selective: bool = True,
    compressed: bool = True,
    partial: bool = False,
) -> PipeParams:
    """Default compressed=True: the explicit-END contract, where the sender returns
    False after all chunks are acked. Uncompressed full-frame transfers ALWAYS
    auto-complete (firmware sends an unsolicited END_ACK) — tested separately
    below. partial=True also uses the explicit-END contract (partial never
    auto-completes, Part 1 §1.5)."""
    return PipeParams(window, ack_every, max_frame, selective, compressed, partial=partial)


def _data_frames(conn: ScriptedConn) -> list[bytes]:
    return [w for w in conn.written if w[:2] == b"\x00\x81"]


def _seqs(conn: ScriptedConn) -> list[int]:
    return [w[2] for w in _data_frames(conn)]


# ─── Happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_all_within_one_window() -> None:
    dev, conn = make_device([data_ack({0, 1, 2, 3})], max_queue_size=4)
    chunks = [b"a", b"b", b"c", b"d"]
    auto = await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=5.0)
    assert auto is False
    assert _seqs(conn) == [0, 1, 2, 3]
    assert all(r is False for r in conn.write_responses)  # data frames are WNR


@pytest.mark.asyncio
async def test_span_window_invariant_multi_window() -> None:
    """With W=3 and 6 chunks, exactly W frames go out before each ACK refunds."""
    dev, conn = make_device([data_ack({0, 1, 2}), data_ack({0, 1, 2, 3, 4, 5})], max_queue_size=3)
    chunks = [bytes([i]) for i in range(6)]
    await dev._send_pipe_chunks(chunks, _params(window=3), chunk_timeout=5.0)
    # writes_at_read snapshots the cumulative write count at each blocking read.
    assert conn.writes_at_read[0] == 3  # only W sent before the first ACK
    assert conn.writes_at_read[1] == 6  # next window refunded → remaining 3
    assert _seqs(conn) == [0, 1, 2, 3, 4, 5]


def _cumulative_acks(n: int, step: int) -> list[bytes]:
    """Cumulative ACKs advancing by ``step`` (<= 32) up to n chunks."""
    acks = []
    k = step
    while k < n:
        acks.append(data_ack(set(range(k))))
        k += step
    acks.append(data_ack(set(range(n))))
    return acks


@pytest.mark.asyncio
async def test_seq_wraps_past_255() -> None:
    n = 260
    # W=32 (the structural cap); step ACKs by 32 so each 32-bit mask fully
    # describes the in-flight range.
    dev, conn = make_device(_cumulative_acks(n, 32), max_queue_size=32)
    chunks = [bytes([i % 251]) for i in range(n)]
    await dev._send_pipe_chunks(chunks, _params(window=32), chunk_timeout=5.0)
    seqs = _seqs(conn)
    assert len(seqs) == n  # each chunk sent exactly once (no loss)
    assert seqs[255] == 255
    assert seqs[256] == 0  # wrapped
    assert seqs[259] == 3


@pytest.mark.asyncio
async def test_ack_cadence_multiple_acks() -> None:
    # W=4, three ACKs each advancing by two chunks over 6 chunks.
    dev, conn = make_device(
        [data_ack({0, 1}), data_ack({0, 1, 2, 3}), data_ack({0, 1, 2, 3, 4, 5})],
        max_queue_size=4,
    )
    chunks = [bytes([i]) for i in range(6)]
    auto = await dev._send_pipe_chunks(chunks, _params(window=4, ack_every=2), chunk_timeout=5.0)
    assert auto is False
    assert sorted(_seqs(conn)) == [0, 1, 2, 3, 4, 5]


# ─── Auto-complete / NACK ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_complete_end_ack_is_terminal() -> None:
    """An unsolicited END_ACK during send → terminal success, no explicit END."""
    dev, conn = make_device([END_ACK], max_queue_size=8)
    auto = await dev._send_pipe_chunks([b"x", b"y"], _params(window=8), chunk_timeout=5.0)
    assert auto is True
    # Data frames were sent, but the sender returns before emitting any 0x82 END.
    assert _seqs(conn) == [0, 1]


@pytest.mark.asyncio
async def test_fatal_data_nack_aborts() -> None:
    dev, conn = make_device([data_nack(0x03, 1, 0x3)], max_queue_size=4)
    with pytest.raises(ProtocolError, match="NACK"):
        await dev._send_pipe_chunks([b"a", b"b"], _params(window=4), chunk_timeout=5.0)


# ─── Loss recovery (selective repeat, bit0 set) ──────────────────────────────


@pytest.mark.asyncio
async def test_selective_repeat_only_missing_chunk() -> None:
    """A hole at chunk 1 → ONLY chunk 1 retransmitted; window frozen until repair."""
    # 4 chunks, W=4. First ACK: received {0,2,3} (chunk 1 missing). Window_base
    # freezes at 1. Second ACK: {0,1,2,3} all received → done.
    ack1 = data_ack({0, 2, 3})
    ack2 = data_ack({0, 1, 2, 3})
    dev, conn = make_device([ack1, ack2], max_queue_size=4)
    chunks = [b"a", b"b", b"c", b"d"]
    await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=5.0)
    seqs = _seqs(conn)
    # Initial send 0,1,2,3 then exactly ONE retransmit of seq 1.
    assert seqs == [0, 1, 2, 3, 1]
    assert seqs.count(1) == 2  # only chunk 1 resent
    for s in (0, 2, 3):
        assert seqs.count(s) == 1  # nothing else resent


@pytest.mark.asyncio
async def test_window_freezes_at_hole() -> None:
    """A hole must freeze window_base so no chunk beyond span is sent."""
    # 6 chunks, W=3. First window sends 0,1,2. ACK reports {0,2} (hole at 1),
    # window_base stays 0 → span rule blocks sending 3+. Only retransmit of 1.
    ack1 = data_ack({0, 2})
    ack2 = data_ack({0, 1, 2, 3, 4, 5})
    dev, conn = make_device([ack1, ack2], max_queue_size=3)
    chunks = [bytes([i]) for i in range(6)]
    await dev._send_pipe_chunks(chunks, _params(window=3), chunk_timeout=5.0)
    # After first window (0,1,2) and ack {0,2}: window_base=0 (1 missing), so no
    # new chunks; retransmit 1. writes at first read = 3.
    assert conn.writes_at_read[0] == 3
    # The retransmit of chunk 1 happens before the second read.
    assert 1 in _seqs(conn)[3:4]


@pytest.mark.asyncio
async def test_rewind_fallback_when_bit0_clear() -> None:
    """selective=False → rewind: resend from window_base, not just the hole."""
    ack1 = data_ack({0, 2, 3})  # hole at 1
    ack2 = data_ack({0, 1, 2, 3})
    dev, conn = make_device([ack1, ack2], max_queue_size=4)
    chunks = [b"a", b"b", b"c", b"d"]
    await dev._send_pipe_chunks(chunks, _params(window=4, selective=False), chunk_timeout=5.0)
    seqs = _seqs(conn)
    # Rewind sets next_to_send=window_base(=1) → chunks 1,2,3 resent.
    assert seqs[:4] == [0, 1, 2, 3]
    assert seqs[4:] == [1, 2, 3]


@pytest.mark.asyncio
async def test_retransmit_repaced_per_new_ack() -> None:
    """Two successive ACKs still showing the hole → chunk retransmitted each time."""
    ack1 = data_ack({0, 2, 3})
    ack2 = data_ack({0, 2, 3})  # still missing 1
    ack3 = data_ack({0, 1, 2, 3})
    dev, conn = make_device([ack1, ack2, ack3], max_queue_size=4)
    chunks = [b"a", b"b", b"c", b"d"]
    await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=5.0)
    seqs = _seqs(conn)
    # Initial 0,1,2,3 + retransmit on ack1 + retransmit on ack2 = chunk 1 sent 3x.
    assert seqs.count(1) == 3


@pytest.mark.asyncio
async def test_lost_ack_superseded_no_spurious_retransmit() -> None:
    """A dropped ACK followed by a later cumulative ACK → no retransmit, advance."""
    # W=4, 4 chunks. The receiver's first ACK (for {0,1}) is 'lost'; we only
    # deliver the cumulative ACK for all four. No hole exists → no retransmit.
    dev, conn = make_device([data_ack({0, 1, 2, 3})], max_queue_size=4)
    chunks = [b"a", b"b", b"c", b"d"]
    await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=5.0)
    seqs = _seqs(conn)
    assert seqs == [0, 1, 2, 3]  # each sent exactly once


# ─── PTO / RETX aborts ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pto_probe_then_recover() -> None:
    """A read timeout triggers a PTO probe (resend oldest unacked), then recovers."""
    dev, conn = make_device([BLETimeoutError, data_ack({0, 1})], max_queue_size=4)
    chunks = [b"a", b"b"]
    await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=1.0)
    seqs = _seqs(conn)
    # 0,1 sent; timeout → resend oldest unacked (0); then ACK completes.
    assert seqs == [0, 1, 0]


@pytest.mark.asyncio
async def test_max_pto_aborts() -> None:
    dev, conn = make_device([BLETimeoutError, BLETimeoutError, BLETimeoutError], max_queue_size=4)
    with pytest.raises(ProtocolError, match="PTO"):
        await dev._send_pipe_chunks([b"a"], _params(window=4), chunk_timeout=0.5)


@pytest.mark.asyncio
async def test_max_retx_aborts() -> None:
    """ACKs perpetually reporting the same hole → abort after 3*W retransmits."""
    # W=2 → MAX_RETX=6. Deliver many ACKs still missing chunk 1.
    acks = [data_ack({0, 2})] * 20  # chunk 1 always missing (2 received, 3 not sent yet)
    dev, conn = make_device(acks, max_queue_size=2)
    chunks = [b"a", b"b", b"c"]
    with pytest.raises(ProtocolError, match="MAX_RETX"):
        await dev._send_pipe_chunks(chunks, _params(window=2), chunk_timeout=5.0)


# ─── Compressed tail-flush (regression: MAJOR 1) ─────────────────────────────


@pytest.mark.asyncio
async def test_compressed_tail_stall_short_timeout_and_dup_probe() -> None:
    """n_chunks % N != 0: the tail never earns a cadence ACK — the sender must
    wait only the short flush timeout, dup-probe, and finish on the elicited
    duplicate-ACK. The scripted firmware ACKs ONLY on cadence and in response to
    the duplicate (no convenient full-coverage ACK is volunteered)."""
    # 3 chunks, N=2: cadence ACK covers {0,1}; chunk 2 is accepted silently.
    dev, conn = make_device(
        [data_ack({0, 1}), BLETimeoutError, data_ack({0, 1, 2})],
        max_queue_size=4,
    )
    chunks = [b"a", b"b", b"c"]
    auto = await dev._send_pipe_chunks(chunks, _params(window=4, ack_every=2, compressed=True), chunk_timeout=5.0)
    assert auto is False  # compressed → caller still sends the explicit END
    # First read (cadence ACK owed: 3 unacked >= N) used the full chunk timeout;
    # the tail waits (< N unacked, no holes) used the short flush timeout.
    assert conn.timeouts[0] == pytest.approx(5.0)
    assert conn.timeouts[1] == pytest.approx(0.5)
    assert conn.timeouts[2] == pytest.approx(0.5)
    # The dup-probe resent exactly the oldest unacked (tail) chunk — this also
    # repairs a genuinely lost tail chunk instead of surfacing a fatal END NACK.
    assert _seqs(conn) == [0, 1, 2, 2]


@pytest.mark.asyncio
async def test_compressed_tail_flush_not_used_while_cadence_ack_owed() -> None:
    """With unacked >= N the firmware still owes a cadence ACK → full timeout."""
    # 4 chunks, N=2: after sending all, 4 unacked >= N → no tail-flush shortcut.
    dev, conn = make_device([data_ack({0, 1, 2, 3})], max_queue_size=4)
    await dev._send_pipe_chunks(
        [b"a", b"b", b"c", b"d"], _params(window=4, ack_every=2, compressed=True), chunk_timeout=5.0
    )
    assert conn.timeouts == [pytest.approx(5.0)]


@pytest.mark.asyncio
async def test_compressed_tail_flush_not_used_with_known_hole() -> None:
    """A known hole keeps the normal loss-recovery timeout, not the tail flush."""
    # 3 chunks, N=2. ACK {0,2}: hole at 1 → next read must use chunk_timeout
    # (retransmit pacing path), not the short tail-flush timeout.
    dev, conn = make_device([data_ack({0, 2}), data_ack({0, 1, 2})], max_queue_size=4)
    await dev._send_pipe_chunks([b"a", b"b", b"c"], _params(window=4, ack_every=2, compressed=True), chunk_timeout=5.0)
    assert conn.timeouts[1] == pytest.approx(5.0)  # hole known → full timeout


# ─── Uncompressed auto-complete contract (regression: MAJOR 2) ───────────────


@pytest.mark.asyncio
async def test_uncompressed_waits_for_unsolicited_end_ack() -> None:
    """Actual firmware ordering: flush-ACK (acks everything) THEN unsolicited
    {0x00,0x82}. The sender must NOT stop at the flush-ACK — it keeps reading
    until the END_ACK and reports auto-complete."""
    dev, conn = make_device([data_ack({0, 1}), END_ACK], max_queue_size=4)
    auto = await dev._send_pipe_chunks([b"a", b"b"], _params(window=4, compressed=False), chunk_timeout=90.0)
    assert auto is True
    # No explicit END is ever written on the uncompressed path.
    assert all(w[:2] != b"\x00\x82" for w in conn.written)


@pytest.mark.asyncio
async def test_uncompressed_missing_end_ack_aborts() -> None:
    """All chunks acked but the auto-complete END_ACK never arrives → abort
    loudly (nothing left to probe), not a spurious END."""
    dev, conn = make_device([data_ack({0}), BLETimeoutError], max_queue_size=8)
    with pytest.raises(ProtocolError, match="auto-complete"):
        await dev._send_pipe_chunks([b"z"], _params(window=8, compressed=False), chunk_timeout=0.5)
    assert all(w[:2] != b"\x00\x82" for w in conn.written)


# ─── Uncompressed partial explicit-END contract (Part 1 §1.5) ────────────────


@pytest.mark.asyncio
async def test_uncompressed_partial_returns_false_after_all_acked() -> None:
    """Uncompressed *partial* never auto-completes: once every chunk is acked the
    sender breaks and returns False (caller sends the explicit END) — it must NOT
    keep reading for an unsolicited END_ACK the way full-frame uncompressed does."""
    dev, conn = make_device([data_ack({0, 1})], max_queue_size=4)
    auto = await dev._send_pipe_chunks(
        [b"a", b"b"], _params(window=4, compressed=False, partial=True), chunk_timeout=5.0
    )
    assert auto is False  # explicit-END contract → caller sends END
    assert all(w[:2] != b"\x00\x82" for w in conn.written)  # sender never emits END itself


@pytest.mark.asyncio
async def test_uncompressed_partial_tail_flush_dup_probe() -> None:
    """A < N_eff partial tail earns no cadence ACK → short flush timeout + dup-probe,
    exactly like the compressed tail (the explicit_end substitution)."""
    dev, conn = make_device(
        [data_ack({0, 1}), BLETimeoutError, data_ack({0, 1, 2})],
        max_queue_size=4,
    )
    auto = await dev._send_pipe_chunks(
        [b"a", b"b", b"c"], _params(window=4, ack_every=2, compressed=False, partial=True), chunk_timeout=5.0
    )
    assert auto is False
    assert conn.timeouts[0] == pytest.approx(5.0)  # cadence ACK owed (3 unacked >= N)
    assert conn.timeouts[1] == pytest.approx(0.5)  # tail flush (1 unacked < N, no holes)
    assert conn.timeouts[2] == pytest.approx(0.5)
    assert _seqs(conn) == [0, 1, 2, 2]  # dup-probe resent the oldest unacked tail chunk


@pytest.mark.asyncio
async def test_single_chunk_partial_completes() -> None:
    dev, conn = make_device([data_ack({0})], max_queue_size=8)
    auto = await dev._send_pipe_chunks([b"z"], _params(window=8, compressed=False, partial=True), chunk_timeout=5.0)
    assert auto is False
    assert _seqs(conn) == [0]


@pytest.mark.asyncio
async def test_run_pipe_upload_partial_auto_complete_is_contract_violation() -> None:
    """If firmware wrongly auto-completes a partial transfer, _run_pipe_upload must
    raise (an unsolicited END_ACK would have refreshed FULL with no committed etag)."""
    dev, conn = make_device([END_ACK], max_queue_size=8)
    with pytest.raises(ProtocolError, match="auto-completed unexpectedly"):
        await dev._run_pipe_upload(
            b"z",
            _params(window=8, compressed=False, partial=True),
            RefreshMode.PARTIAL,
            progress_callback=None,
            new_etag=0xABCD,
        )


@pytest.mark.asyncio
async def test_run_pipe_upload_partial_sends_explicit_end_selector_2() -> None:
    """Partial upload always sends END = [0x02][new_etag BE]."""
    dev, conn = make_device([data_ack({0}), END_ACK, REFRESH_COMPLETE], max_queue_size=8)
    committed = await dev._run_pipe_upload(
        b"z",
        _params(window=8, compressed=False, partial=True),
        RefreshMode.PARTIAL,
        progress_callback=None,
        new_etag=0xABCD,
    )
    assert committed is True
    end_frames = [w for w in conn.written if w[:2] == b"\x00\x82"]
    assert len(end_frames) == 1
    assert end_frames[0] == b"\x00\x82" + bytes([2]) + (0xABCD).to_bytes(4, "big")


# ─── _run_pipe_upload / _await_pipe_end_ack ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_pipe_upload_compressed_full_flow_with_etag() -> None:
    """Compressed contract unchanged: explicit END (with etag) is still sent."""
    dev, conn = make_device(
        [data_ack({0}), END_ACK, REFRESH_COMPLETE],
        max_queue_size=8,
    )
    committed = await dev._run_pipe_upload(
        b"hello",
        _params(window=8, compressed=True),
        RefreshMode.FULL,
        progress_callback=None,
        new_etag=0xABCD,
    )
    assert committed is True  # END-with-etag was sent
    end_frames = [w for w in conn.written if w[:2] == b"\x00\x82"]
    assert len(end_frames) == 1
    assert end_frames[0] == b"\x00\x82" + bytes([0]) + (0xABCD).to_bytes(4, "big")


@pytest.mark.asyncio
async def test_run_pipe_upload_uncompressed_auto_completes_no_end_no_etag() -> None:
    """Uncompressed: firmware flush-ACKs then auto-completes with an unsolicited
    END_ACK, resetting pipe state — the client must not send END, and must NOT
    report the etag as committed (firmware auto-complete stores no etag; a
    recorded phantom etag would poison every subsequent partial update)."""
    dev, conn = make_device([data_ack({0}), END_ACK, REFRESH_COMPLETE], max_queue_size=8)
    committed = await dev._run_pipe_upload(
        b"z",
        _params(window=8, compressed=False),
        RefreshMode.FULL,
        progress_callback=None,
        new_etag=0xABCD,
    )
    assert committed is False  # etag never committed on auto-complete
    assert all(w[:2] != b"\x00\x82" for w in conn.written)  # no spurious END


@pytest.mark.asyncio
async def test_run_pipe_upload_no_etag_returns_false() -> None:
    dev, conn = make_device([data_ack({0}), END_ACK, REFRESH_COMPLETE], max_queue_size=8)
    committed = await dev._run_pipe_upload(
        b"z", _params(window=8), RefreshMode.FULL, progress_callback=None, new_etag=None
    )
    assert committed is False  # no etag committed


@pytest.mark.asyncio
async def test_run_pipe_upload_auto_complete_skips_end() -> None:
    """Firmware auto-completes (END_ACK mid-send) → no explicit 0x82 END written."""
    dev, conn = make_device([END_ACK, REFRESH_COMPLETE], max_queue_size=8)
    committed = await dev._run_pipe_upload(
        b"z", _params(window=8), RefreshMode.FULL, progress_callback=None, new_etag=0xAB
    )
    assert committed is False  # auto-completed → etag never committed
    assert all(w[:2] != b"\x00\x82" for w in conn.written)


@pytest.mark.asyncio
async def test_await_end_ack_skips_tail_flush_ack() -> None:
    # A trailing PIPE_ACK precedes the END_ACK — must be ignored.
    dev, conn = make_device([data_ack({0, 1}), data_ack({0, 1}), END_ACK, REFRESH_COMPLETE], max_queue_size=8)
    await dev._run_pipe_upload(b"ab", _params(window=8), RefreshMode.FULL, progress_callback=None, new_etag=None)
    # Completed without error.
    assert any(w[:2] == b"\x00\x82" for w in conn.written)


@pytest.mark.asyncio
async def test_await_end_nack_aborts() -> None:
    dev, conn = make_device([data_ack({0}), END_NACK], max_queue_size=8)
    with pytest.raises(ProtocolError, match="END NACK"):
        await dev._run_pipe_upload(b"z", _params(window=8), RefreshMode.FULL, progress_callback=None, new_etag=None)


@pytest.mark.asyncio
async def test_refresh_timeout_raises() -> None:
    dev, conn = make_device([data_ack({0}), END_ACK, b"\x00\x74"], max_queue_size=8)
    with pytest.raises(ProtocolError, match="refresh timed out"):
        await dev._run_pipe_upload(b"z", _params(window=8), RefreshMode.FULL, progress_callback=None, new_etag=None)


# ─── drain_stale enforcement ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_in_stream_writes_pass_drain_stale_false() -> None:
    dev, conn = make_device([data_ack({0, 1}), END_ACK, REFRESH_COMPLETE], max_queue_size=8)
    await dev._run_pipe_upload(b"ab", _params(window=8), RefreshMode.FULL, progress_callback=None, new_etag=None)
    # Data (0x81) and END (0x82) writes → drain_stale False.
    for frame, drain in zip(conn.written, conn.drain_flags):
        if frame[:2] in (b"\x00\x81", b"\x00\x82"):
            assert drain is False, f"{frame[:2].hex()} must not drain"


@pytest.mark.asyncio
async def test_probe_write_uses_default_drain_stale_true() -> None:
    dev, conn = make_device(
        [BLETimeoutError, b"\x00\x70", b"\x00\x71", b"\x00\x72", REFRESH_COMPLETE], max_queue_size=8
    )
    await dev._execute_upload(b"AB", RefreshMode.FULL, use_compression=False)
    # The 0x0080 probe went through _write → default drain_stale True.
    probe_idx = next(i for i, w in enumerate(conn.written) if w[:2] == b"\x00\x80")
    assert conn.drain_flags[probe_idx] is True


# ─── Encryption specifics ────────────────────────────────────────────────────


def _make_session(dev) -> None:
    key = b"\x11" * 16
    cn, sn, did = b"\x01" * 16, b"\x02" * 16, b"\x00\x00\x00\x01"
    dev._session_key = derive_session_key(key, cn, sn, did)
    dev._session_id = derive_session_id(dev._session_key, cn, sn)
    dev._nonce_counter = 0


@pytest.mark.asyncio
async def test_encrypted_frames_and_fresh_nonce_on_retransmit() -> None:
    dev, conn = make_device([data_ack({0, 2}), data_ack({0, 1, 2})], max_queue_size=4)
    _make_session(dev)
    chunks = [b"a", b"b", b"c"]
    start_nonce = dev._nonce_counter
    await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=5.0)
    frames = _data_frames(conn)
    # Every data frame is a full CCM envelope (>= 31 bytes).
    for f in frames:
        assert len(f) >= 31
    # 3 initial sends + 1 retransmit of chunk 1 = 4 encryptions → nonce advanced 4x.
    assert dev._nonce_counter == start_nonce + 4
    # The retransmit carries a higher nonce than the original send of chunk 1.
    seq1_frames = [f for f in frames if decrypt_response(dev._session_key, f)[1][0] == 1]
    assert len(seq1_frames) == 2
    n0 = int.from_bytes(seq1_frames[0][2:18], "big")
    n1 = int.from_bytes(seq1_frames[1][2:18], "big")
    assert n1 > n0  # fresh, higher nonce on retransmit


@pytest.mark.asyncio
async def test_encrypted_seq_is_first_plaintext_byte() -> None:
    dev, conn = make_device([data_ack({0, 1, 2, 3})], max_queue_size=4)
    _make_session(dev)
    chunks = [b"AA", b"BB", b"CC", b"DD"]
    await dev._send_pipe_chunks(chunks, _params(window=4), chunk_timeout=5.0)
    for expected_seq, f in enumerate(_data_frames(conn)):
        _cmd, payload = decrypt_response(dev._session_key, f)
        assert payload[0] == expected_seq  # seq is the first plaintext byte
        assert payload[1:] == chunks[expected_seq]  # then the chunk


@pytest.mark.asyncio
async def test_encrypted_data_size_212_at_244() -> None:
    dev, _ = make_device([], max_queue_size=4)
    _make_session(dev)
    assert dev._pipe_data_size(244) == 212  # 244 - 31 - 1


@pytest.mark.asyncio
async def test_plaintext_data_size_241_at_244() -> None:
    dev, _ = make_device([], max_queue_size=4)
    assert dev._pipe_data_size(244) == 241  # 244 - 3


@pytest.mark.asyncio
async def test_encrypted_ack_decrypts_and_classifies() -> None:
    """An encrypted 7-byte ACK survives _read decryption and classifies as PIPE_ACK."""
    from opendisplay.crypto import encrypt_command
    from opendisplay.protocol.responses import PIPE_FRAME_ACK, classify_pipe_frame

    dev, conn = make_device([], max_queue_size=4)
    _make_session(dev)
    # Firmware would encrypt {0x00,0x81,hs,mask} via sendResponse: cmd=0x0081,
    # payload=[hs,mask:4]. Reproduce that envelope and confirm _read yields the ACK.
    ack_plain = data_ack({0, 1, 2})
    env = encrypt_command(dev._session_key, dev._session_id, 0, ack_plain[:2], ack_plain[2:])
    conn._responses = [env]
    decoded = await dev._read(timeout=5.0)
    assert classify_pipe_frame(decoded) == PIPE_FRAME_ACK
    assert decoded == ack_plain


@pytest.mark.asyncio
async def test_encrypted_nack_decrypts_and_aborts() -> None:
    """Encrypted pipe NACKs (byte[2]=err != 0xFF) are decrypted by _read, then
    classified on plaintext and abort the send (firmware clarification #1)."""
    from opendisplay.crypto import encrypt_command

    dev, conn = make_device([], max_queue_size=4)
    _make_session(dev)
    nack_plain = data_nack(0x03, 1, 0x1)  # {0xFF,0x81,err,hs,mask}
    env = encrypt_command(dev._session_key, dev._session_id, 0, nack_plain[:2], nack_plain[2:])
    assert len(env) >= 31  # genuinely encrypted envelope
    conn._responses = [env]
    with pytest.raises(ProtocolError, match="NACK"):
        await dev._send_pipe_chunks([b"a", b"b"], _params(window=4), chunk_timeout=5.0)


def test_encrypted_chunk_capacity_beats_legacy() -> None:
    # Sanity: 212 > legacy encrypted 154.
    assert 212 > ENCRYPTED_CHUNK_SIZE


@pytest.mark.asyncio
async def test_encrypted_full_pipe_end_to_end_through_execute_upload() -> None:
    """Encrypted session, FULL-frame pipe, all the way through _execute_upload:
    encrypted 0x0080 negotiation, encrypted 0x0081 data, firmware auto-complete
    (encrypted unsolicited END_ACK at the 31-byte decrypt floor), encrypted
    0x73. Every host frame must be a valid CCM envelope; no plaintext leak and
    no spurious explicit END."""
    from opendisplay.crypto import encrypt_command

    dev, conn = make_device([], max_queue_size=16)
    _make_session(dev)

    def enc(plain: bytes, ctr: int) -> bytes:
        # Device→host responses use the same CCM envelope as commands.
        return encrypt_command(dev._session_key, dev._session_id, ctr, plain[:2], plain[2:])

    # start_ack (8 B), data ack for chunk 0, unsolicited auto-complete END_ACK
    # (2-byte plaintext → 31-byte envelope, the exact decrypt-floor boundary),
    # then refresh-complete.
    conn._responses = [enc(start_ack(), 0), enc(data_ack({0}), 1), enc(END_ACK, 2), enc(REFRESH_COMPLETE, 3)]
    assert len(conn._responses[2]) == 31  # END_ACK rides the >=31 decrypt boundary

    committed = await dev._execute_upload(b"ABCD", RefreshMode.FULL, use_compression=False, new_etag=0x1234)

    assert committed is False  # auto-complete never commits an etag
    # Every write is encrypted: valid envelope, no plaintext opcode on the wire.
    plaintexts = []
    for w in conn.written:
        assert len(w) >= 31
        cmd_code, payload = decrypt_response(dev._session_key, w)
        plaintexts.append(cmd_code.to_bytes(2, "big") + payload)
    assert plaintexts[0][:2] == b"\x00\x80"
    assert len(plaintexts[0]) == 12  # full-frame START: no partial extension
    assert any(p[:2] == b"\x00\x81" and p[3:] == b"ABCD" for p in plaintexts)
    assert all(p[:2] != b"\x00\x82" for p in plaintexts)  # auto-complete: no explicit END
    assert all(p[:2] != b"\x00\x70" for p in plaintexts)  # never fell back to legacy
