"""Unit tests for the 0x76 streamed-rectangle partial protocol."""

from __future__ import annotations

import pytest

from opendisplay.partial import (
    ERR_ETAG_MISMATCH,
    ERR_MIXED_DATA,
    ERR_RECT_ALIGN,
    ERR_RECT_OOB,
    PARTIAL_FLAG_COMPRESSED,
    PartialState,
    _generate_etag,
    align_rect,
    build_partial_logical_stream,
    compute_bounding_rect,
    parse_nack,
)
from opendisplay.protocol.commands import (
    MAX_START_PAYLOAD,
    build_direct_write_end_with_etag,
    build_direct_write_partial_start,
)


class TestPartialState:
    def test_roundtrip_empty(self):
        state = PartialState()
        assert PartialState.from_bytes(state.to_bytes()) == state

    def test_roundtrip_populated(self):
        state = PartialState(
            etag=0xDEADBEEF,
            last_image=bytes(range(256)) * 4,
            width=480,
            height=800,
            bytes_per_pixel=1,
        )

        out = PartialState.from_bytes(state.to_bytes())

        assert out.etag == state.etag
        assert out.last_image == state.last_image
        assert out.width == state.width
        assert out.height == state.height
        assert out.bytes_per_pixel == state.bytes_per_pixel

    def test_bad_magic_rejected(self):
        with pytest.raises(ValueError, match="magic"):
            PartialState.from_bytes(b"XXXX" + b"\x00" * 21)

    def test_truncated_rejected(self):
        with pytest.raises(ValueError):
            PartialState.from_bytes(b"PDST")


class TestParseNack:
    def test_known_errors(self):
        assert parse_nack(b"\xff\x76\x01\x00") == (0x76, ERR_ETAG_MISMATCH)
        assert parse_nack(b"\xff\x70\x02\x00") == (0x70, ERR_MIXED_DATA)
        assert parse_nack(b"\xff\x76\x03\x00") == (0x76, ERR_RECT_OOB)
        assert parse_nack(b"\xff\x76\x04\x00") == (0x76, ERR_RECT_ALIGN)

    def test_not_nack_returns_none(self):
        assert parse_nack(b"\x00\x76") is None
        assert parse_nack(b"\xff\x70\x01") is None
        assert parse_nack(b"\xff\x70\x01\x00\x00") is None
        assert parse_nack(b"\xff\x70\x01\x01") is None


class TestGenerateEtag:
    def test_nonzero_uint32(self):
        for _ in range(100):
            value = _generate_etag()
            assert 1 <= value <= 0xFFFFFFFF


class TestBoundingRect:
    def test_no_change_returns_none(self):
        buf = bytes(100)
        assert compute_bounding_rect(buf, buf, 10, 10) is None

    def test_single_pixel_change(self):
        old = bytearray(16 * 8)
        new = bytearray(old)
        new[3 * 16 + 13] = 1

        assert compute_bounding_rect(bytes(old), bytes(new), 16, 8) == (13, 3, 14, 4)

    def test_multiple_changes_one_bounding_box(self):
        old = bytearray(32 * 20)
        new = bytearray(old)
        new[2 * 32 + 1] = 1
        new[15 * 32 + 28] = 1

        assert compute_bounding_rect(bytes(old), bytes(new), 32, 20) == (1, 2, 29, 16)


class TestAlignRect:
    def test_mono_expands_to_8_pixels(self):
        assert align_rect(13, 3, 14, 4, 32, 20, pixels_per_byte=8) == (8, 3, 8, 1)

    def test_2bpp_expands_to_4_pixels(self):
        assert align_rect(5, 1, 7, 3, 32, 20, pixels_per_byte=4) == (4, 1, 4, 2)

    def test_4bpp_expands_to_2_pixels(self):
        assert align_rect(5, 1, 6, 3, 32, 20, pixels_per_byte=2) == (4, 1, 2, 2)

    def test_right_edge_clamp_keeps_width_aligned(self):
        assert align_rect(30, 1, 32, 2, 32, 20, pixels_per_byte=8) == (24, 1, 8, 1)


class TestLogicalStream:
    def test_old_then_new_plane_major_stream(self):
        stream = build_partial_logical_stream(b"abcdef", b"ABCDEF")
        assert stream == b"abcdefABCDEF"

    def test_rejects_mismatched_rect_lengths(self):
        with pytest.raises(AssertionError, match="old/new rect byte lengths"):
            build_partial_logical_stream(b"abcde", b"ABCDEF")

    def test_stream_accounting(self):
        old = bytes(range(16))
        new = bytes(range(16, 32))
        stream = build_partial_logical_stream(old, new)

        assert len(stream) == 32
        assert stream[:16] == old
        assert stream[16:] == new


class TestBuilders:
    def test_partial_start_fixed_fields_and_initial_bytes(self):
        stream = bytes(range(200))
        packet, remaining = build_direct_write_partial_start(
            old_etag=0xDEADBEEF,
            new_etag=0x01020304,
            flags=PARTIAL_FLAG_COMPRESSED,
            x=8,
            y=9,
            width=16,
            height=10,
            stream_bytes=stream,
        )

        assert len(packet) == MAX_START_PAYLOAD
        assert packet[:2] == b"\x00\x76"
        assert packet[2] == PARTIAL_FLAG_COMPRESSED
        assert int.from_bytes(packet[3:7], "big") == 0xDEADBEEF
        assert int.from_bytes(packet[7:11], "big") == 0x01020304
        assert int.from_bytes(packet[11:13], "big") == 8
        assert int.from_bytes(packet[13:15], "big") == 9
        assert int.from_bytes(packet[15:17], "big") == 16
        assert int.from_bytes(packet[17:19], "big") == 10
        assert packet[19:] == stream[:181]
        assert remaining == stream[181:]

    def test_partial_start_allows_zero_etag(self):
        packet, _ = build_direct_write_partial_start(0, 0x01020304, 0, 0, 0, 8, 1)
        assert packet[3:7] == b"\x00\x00\x00\x00"

    def test_end_with_etag(self):
        cmd = build_direct_write_end_with_etag(refresh_mode=2, new_etag=0x01020304)
        assert cmd == b"\x00\x72\x02\x01\x02\x03\x04"
