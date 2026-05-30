"""Tests for zlib compression settings used by BLE uploads."""

from __future__ import annotations

import pytest

from opendisplay.encoding import (
    DEFAULT_ZLIB_WINDOW_BITS,
    ZIPXL_ZLIB_WINDOW_BITS,
    compress_image_data,
    decompress_image_data,
    zlib_window_bits,
)


def test_compress_image_data_defaults_to_standard_zlib_window() -> None:
    data = b"abc123" * 100

    compressed = compress_image_data(data)

    assert zlib_window_bits(compressed) == DEFAULT_ZLIB_WINDOW_BITS
    assert decompress_image_data(compressed) == data


def test_compress_image_data_supports_zipxl_512_byte_window() -> None:
    data = b"abc123" * 100

    compressed = compress_image_data(data, window_bits=ZIPXL_ZLIB_WINDOW_BITS)

    assert zlib_window_bits(compressed) == ZIPXL_ZLIB_WINDOW_BITS
    assert decompress_image_data(compressed) == data


def test_compress_image_data_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="window_bits"):
        compress_image_data(b"data", window_bits=8)


def test_zlib_window_bits_returns_none_for_non_zlib_data() -> None:
    assert zlib_window_bits(b"\xff\xff") is None
