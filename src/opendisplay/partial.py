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

from epaper_dithering import ColorScheme
from PIL import Image

from .encoding import encode_image
from .models.config import DisplayConfig, GlobalConfig

# ---------------------------------------------------------------------------
# Wire constants mirrored from the firmware partial-rendering protocol.
# ---------------------------------------------------------------------------

# NACK error codes returned by the device inside {0xFF, opcode, error, 0x00}
ERR_ETAG_MISMATCH = 0x01  # on 0x76: client must fall back to full transfer
ERR_MIXED_DATA = 0x02  # aborted; etag cleared on device
ERR_RECT_OOB = 0x03  # on 0x76: rectangle out of display bounds
ERR_RECT_ALIGN = 0x04  # on 0x76: x or width not aligned to byte boundary
ERR_PARTIAL_FLAGS = 0x05  # on 0x76: unsupported or reserved flags set
ERR_PARTIAL_STREAM = 0x06  # on 0x71/0x72: stream byte count or content error
ERR_PARTIAL_UNSUPPORTED = 0x07  # on 0x76: partial update unsupported for panel mode

NACK_PREFIX = 0xFF

# 0x76 flag bits
PARTIAL_FLAG_COMPRESSED = 0x01  # bit 0: stream is zlib-compressed

# pixels_per_byte for each bits_per_pixel value
_PIXELS_PER_BYTE: dict[int, int] = {1: 8, 2: 4, 4: 2, 8: 1}


def parse_nack(response: bytes) -> tuple[int, int] | None:
    """Return (opcode, error_code) if response is a 4-byte {0xFF, op, err, 0x00} NACK.

    Returns None for any other response shape.
    """
    if len(response) == 4 and response[0] == NACK_PREFIX and response[3] == 0x00:
        return response[1], response[2]
    return None


@dataclass
class PartialRegion:
    """Container for validated partial-diff metadata before upload."""

    display: DisplayConfig
    color_scheme: ColorScheme
    width: int
    height: int
    palette_image: Image.Image
    new_palette: bytes
    old_palette: bytes
    rx: int
    ry: int
    rw: int
    rh: int


def compute_partial_region(
    processed_image: Image.Image,
    state: PartialState,
    config: GlobalConfig | None,
    color_scheme: ColorScheme,
) -> str | PartialRegion:
    """Build partial-region metadata for upload diffing."""
    if config is None or not getattr(config, "displays", None):
        return "fallback_full"

    display = config.displays[0]
    if not display.partial_update_support:
        return "fallback_full"

    # Firmware only supports partial refresh on 1bpp panels; 4-gray (and the
    # 3-color schemes) are rejected, so don't attempt a doomed partial round-trip.
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY, ColorScheme.GRAYSCALE_4):
        return "fallback_full"

    width, height = processed_image.size
    if state.etag == 0 or state.last_image is None or state.width != width or state.height != height:
        return "fallback_full"

    palette_image = processed_image.convert("P") if processed_image.mode != "P" else processed_image
    new_palette = palette_image.tobytes()
    old_palette = state.last_image
    if len(old_palette) != len(new_palette):
        return "fallback_full"

    bbox = compute_bounding_rect(old_palette, new_palette, width, height)
    if bbox is None:
        return "no_change"

    bpp = {
        ColorScheme.MONO: 1,
        ColorScheme.BWRY: 2,
        ColorScheme.GRAYSCALE_4: 2,
        ColorScheme.BWGBRY: 4,
        ColorScheme.GRAYSCALE_16: 4,
    }.get(color_scheme, 1)
    pixels_per_byte = _PIXELS_PER_BYTE.get(bpp, 8)

    rx, ry, rw, rh = align_rect(*bbox, width, height, pixels_per_byte)
    if rw == 0 or rh == 0:
        return "fallback_full"

    return PartialRegion(
        display=display,
        color_scheme=color_scheme,
        width=width,
        height=height,
        palette_image=palette_image,
        new_palette=new_palette,
        old_palette=old_palette,
        rx=rx,
        ry=ry,
        rw=rw,
        rh=rh,
    )


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
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                row_changed = True
        if row_changed:
            min_y = min(min_y, y)
            max_y = max(max_y, y)
    if max_x < 0:
        return None
    return (min_x, min_y, max_x + 1, max_y + 1)


def align_rect(
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    display_width: int,
    _display_height: int,
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


def encode_segment_wire(
    palette_image: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    color_scheme: ColorScheme,
) -> bytes:
    """Encode a palette rectangle to protocol wire bytes for partial updates."""
    cropped = palette_image.crop((x, y, x + w, y + h))
    pixels = cropped.tobytes()

    if color_scheme == ColorScheme.MONO:
        output = bytearray((len(pixels) + 7) // 8)
        for i, palette_idx in enumerate(pixels):
            if palette_idx > 0:
                output[i // 8] |= 1 << (7 - (i % 8))
        return bytes(output)

    if color_scheme in (ColorScheme.BWRY, ColorScheme.GRAYSCALE_4):
        output = bytearray((len(pixels) + 3) // 4)
        for i, palette_idx in enumerate(pixels):
            shift = (3 - (i % 4)) * 2
            output[i // 4] |= (palette_idx & 0x03) << shift
        return bytes(output)

    if color_scheme in (ColorScheme.BWGBRY, ColorScheme.GRAYSCALE_16):
        output = bytearray((len(pixels) + 1) // 2)
        bwgbry_map = {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 5: 6}
        for i, palette_idx in enumerate(pixels):
            value = palette_idx & 0x0F
            if color_scheme == ColorScheme.BWGBRY:
                value = bwgbry_map.get(value, 0)
            if i % 2 == 0:
                output[i // 2] |= value << 4
            else:
                output[i // 2] |= value
        return bytes(output)

    return encode_image(cropped, color_scheme)


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
_HEADER_FMT = ">4sBIIII"  # magic(4s), version(B), etag(I BE), width(I), height(I), bpp(I)
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 4+1+4+4+4+4 = 21 bytes


@dataclass
class PartialState:
    """Mutable, opaque state tracked by the caller across partial updates.

    Treat fields as opaque; persist only via ``to_bytes`` / ``from_bytes``.

    ``last_image`` holds raw palette bytes (1 byte per pixel) from
    ``PIL.Image.tobytes()`` on the dithered palette image.
    ``bytes_per_pixel`` is always 1 for library-populated instances.
    """

    etag: int = 0  # last new_etag successfully sent (0 = unknown)
    last_image: bytes | None = None  # raw palette pixel buffer matching etag
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
