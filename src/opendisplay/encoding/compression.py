"""Image compression for BLE transfer."""

from __future__ import annotations

import logging
import zlib

_LOGGER = logging.getLogger(__name__)

DEFAULT_ZLIB_WINDOW_BITS = zlib.MAX_WBITS
ZIPXL_ZLIB_WINDOW_BITS = 9


def compress_image_data(data: bytes, level: int = 6, window_bits: int = DEFAULT_ZLIB_WINDOW_BITS) -> bytes:
    """Compress image data using zlib.

    Args:
        data: Raw image data
        level: Compression level (0-9, default: 6)
            0 = no compression
            1 = fastest
            6 = default balance
            9 = best compression
        window_bits: DEFLATE history window size as log2 bytes (9-15,
            default: 15, the standard zlib default)

    Returns:
        Compressed data
    """
    if not 9 <= window_bits <= zlib.MAX_WBITS:
        raise ValueError(f"window_bits must be in range 9..{zlib.MAX_WBITS}, got {window_bits}")
    if level == 0:
        return data

    compressor = zlib.compressobj(level=level, method=zlib.DEFLATED, wbits=window_bits)
    compressed = compressor.compress(data) + compressor.flush()

    ratio = len(compressed) / len(data) * 100 if data else 0
    _LOGGER.debug(
        "Compressed %d bytes -> %d bytes (%.1f%%, zlib window=%d bits)",
        len(data),
        len(compressed),
        ratio,
        window_bits,
    )

    return compressed


def zlib_window_bits(data: bytes) -> int | None:
    """Return the zlib header's advertised DEFLATE window size, if recognizable."""
    if len(data) < 2:
        return None

    cmf = data[0]
    flg = data[1]
    if (cmf & 0x0F) != 8 or ((cmf << 8) + flg) % 31 != 0 or (flg & 0x20):
        return None
    window_bits = (cmf >> 4) + 8
    if window_bits > zlib.MAX_WBITS:
        return None
    return window_bits


def decompress_image_data(data: bytes) -> bytes:
    """Decompress zlib-compressed image data.

    Args:
        data: Compressed data

    Returns:
        Decompressed data

    Raises:
        zlib.error: If decompression fails
    """
    return zlib.decompress(data)
