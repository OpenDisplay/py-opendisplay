"""Partial rendering support for OpenDisplay BLE devices.

Provides PartialState (caller-owned mutable holder), bounding-rect helpers,
and the logical stream builder for the 0x76 single-rectangle protocol.

Serialization format: struct header (magic + version + scalar fields) followed
by a 4-byte big-endian length-prefixed ``last_image`` blob.

``last_image`` stores raw palette bytes (1 byte per pixel, from
``PIL.Image.tobytes()`` on the dithered palette image).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Wire constants mirrored from the firmware partial-rendering protocol.
# ---------------------------------------------------------------------------

# NACK error codes returned by the device inside {0xFF, opcode, error, 0x00}
ERR_ETAG_MISMATCH   = 0x01   # on 0x76: client must fall back to full transfer
ERR_MIXED_DATA      = 0x02   # aborted; etag cleared on device
ERR_RECT_OOB        = 0x03   # on 0x76: rectangle out of display bounds
ERR_RECT_ALIGN      = 0x04   # on 0x76: x or width not aligned to byte boundary
ERR_PARTIAL_FLAGS   = 0x05   # on 0x76: unsupported or reserved flags set
ERR_PARTIAL_STREAM  = 0x06   # on 0x71/0x72: stream byte count or content error
ERR_PARTIAL_UNSUPPORTED = 0x07   # on 0x76: partial update unsupported for panel mode

NACK_PREFIX = 0xFF

# 0x76 flag bits
PARTIAL_FLAG_COMPRESSED = 0x01   # bit 0: stream is zlib-compressed

# pixels_per_byte for each bits_per_pixel value
_PIXELS_PER_BYTE: dict[int, int] = {1: 8, 2: 4, 4: 2, 8: 1}


def parse_nack(response: bytes) -> tuple[int, int] | None:
    """Return (opcode, error_code) if response is a 4-byte {0xFF, op, err, 0x00} NACK.

    Returns None for any other response shape.
    """
    if len(response) == 4 and response[0] == NACK_PREFIX and response[3] == 0x00:
        return response[1], response[2]
    return None


# ---------------------------------------------------------------------------
# Bounding-rect helpers
# ---------------------------------------------------------------------------

def compute_bounding_rect(
    old: bytes,
    new: bytes,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """Return (x0, y0, x1_excl, y1_excl) of changed pixels, or None if identical."""
    if old == new:
        return None
    min_x, max_x, min_y, max_y = width, -1, height, -1
    for y in range(height):
        row_off = y * width
        row_changed = False
        for x in range(width):
            if old[row_off + x] != new[row_off + x]:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                row_changed = True
        if row_changed:
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
    if max_x < 0:
        return None
    return (min_x, min_y, max_x + 1, max_y + 1)


def align_rect(
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    display_width: int,
    display_height: int,
    pixels_per_byte: int,
) -> tuple[int, int, int, int]:
    """Expand (x0, y0, x1, y1) to packed-byte boundaries.

    Returns (x, y, width, height) ready to send in 0x76.
    """
    aligned_x0 = (x0 // pixels_per_byte) * pixels_per_byte
    aligned_x1 = x1
    if aligned_x1 % pixels_per_byte:
        aligned_x1 += pixels_per_byte - (aligned_x1 % pixels_per_byte)
    if aligned_x1 > display_width:
        aligned_x1 = display_width
        misalign = (aligned_x1 - aligned_x0) % pixels_per_byte
        if misalign:
            aligned_x0 = max(0, aligned_x0 - (pixels_per_byte - misalign))
    return (aligned_x0, y0, aligned_x1 - aligned_x0, y1 - y0)


def build_partial_logical_stream(
    old_rect_bytes: bytes,
    new_rect_bytes: bytes,
) -> bytes:
    """Build a plane-major old-then-new partial stream.

    Produces: old_rect + new_rect.
    """
    assert len(old_rect_bytes) == len(new_rect_bytes), "old/new rect byte lengths must match"
    return old_rect_bytes + new_rect_bytes


# ---------------------------------------------------------------------------
# Etag helpers
# ---------------------------------------------------------------------------

def _generate_etag() -> int:
    """Generate a random non-zero 32-bit etag."""
    while True:
        value = int.from_bytes(os.urandom(4), "big")
        if value != 0:
            return value


# ---------------------------------------------------------------------------
# PartialState
# ---------------------------------------------------------------------------

# Serialization header: b"PDST" magic(4) + version(B) + etag(4BE) + width(4LE)
#                       + height(4LE) + bpp(4LE)
# Followed by: img_len(4BE) + img_bytes(img_len)
_MAGIC = b"PDST"
_VERSION = 1
_HEADER_FMT = ">4sBIIII"   # magic(4s), version(B), etag(I BE), width(I), height(I), bpp(I)
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 4+1+4+4+4+4 = 21 bytes


@dataclass
class PartialState:
    """Mutable, opaque state tracked by the caller across partial updates.

    Treat fields as opaque; persist only via ``to_bytes`` / ``from_bytes``.

    ``last_image`` holds raw palette bytes (1 byte per pixel) from
    ``PIL.Image.tobytes()`` on the dithered palette image.
    ``bytes_per_pixel`` is always 1 for library-populated instances.
    """

    etag: int = 0                     # last new_etag successfully sent (0 = unknown)
    last_image: bytes | None = None   # raw palette pixel buffer matching etag
    width: int = 0
    height: int = 0
    bytes_per_pixel: int = 0

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Serialize state to bytes (struct header + length-prefixed image)."""
        img = self.last_image or b""
        header = struct.pack(
            _HEADER_FMT,
            _MAGIC,
            _VERSION,
            self.etag,
            self.width,
            self.height,
            self.bytes_per_pixel,
        )
        img_len_bytes = struct.pack(">I", len(img))
        return header + img_len_bytes + img

    @classmethod
    def from_bytes(cls, data: bytes) -> "PartialState":
        """Deserialize state from bytes produced by ``to_bytes``."""
        min_len = _HEADER_SIZE + 4  # header + img_len field
        if len(data) < min_len:
            raise ValueError(f"PartialState data too short: {len(data)} bytes (need {min_len})")
        magic, version, etag, width, height, bpp = struct.unpack_from(_HEADER_FMT, data, 0)
        if magic != _MAGIC:
            raise ValueError(f"PartialState magic mismatch: {magic!r}")
        if version != _VERSION:
            raise ValueError(f"PartialState version unsupported: {version}")
        (img_len,) = struct.unpack_from(">I", data, _HEADER_SIZE)
        offset = _HEADER_SIZE + 4
        if len(data) < offset + img_len:
            raise ValueError("PartialState image data truncated")
        img: bytes | None = data[offset : offset + img_len] if img_len > 0 else None
        return cls(etag=etag, last_image=img, width=width, height=height, bytes_per_pixel=bpp)
