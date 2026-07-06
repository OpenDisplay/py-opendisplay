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

import numpy as np
from epaper_dithering import ColorScheme
from PIL import Image

from .encoding import encode_image
from .models.config import DisplayConfig, GlobalConfig
from .models.enums import PartialUpdateSupport

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

    # Firmware only allows partial refresh when getBitsPerPixel() == 1, i.e. MONO,
    # and requires x/width to be multiples of 8 regardless of bpp. Every other
    # scheme is either rejected (ERR_PARTIAL_UNSUPPORTED) or aligns to 4/2 pixels
    # and gets ERR_RECT_ALIGN, so don't attempt a doomed partial round-trip.
    if color_scheme != ColorScheme.MONO:
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

    # MONO packs 8 pixels/byte and firmware requires 8-pixel x/width alignment.
    rx, ry, rw, rh = align_rect(*bbox, width, height, pixels_per_byte=8)
    if rw == 0 or rh == 0:
        return "fallback_full"

    if display.partial_update_support == PartialUpdateSupport.FULL_FRAME:
        # Firmware white-fills the controller RAM at partial start; on these
        # panels the partial waveform erases everything the stream does not
        # cover, so the region must span the whole panel
        # (OpenDisplay/Firmware#80). Still refreshes flicker-free.
        rx, ry, rw, rh = 0, 0, width, height

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
    diff = (np.frombuffer(old, np.uint8) != np.frombuffer(new, np.uint8)).reshape(height, width)
    rows = np.flatnonzero(diff.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(diff.any(axis=0))
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


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

    # Unlike the full-frame encoders, the partial stream packs flat across row
    # boundaries (no per-row byte padding), so the vectorized packing operates
    # on the flat pixel buffer.
    flat = np.frombuffer(pixels, dtype=np.uint8)

    if color_scheme == ColorScheme.MONO:
        return np.packbits(flat > 0).tobytes()

    if color_scheme in (ColorScheme.BWRY, ColorScheme.GRAYSCALE_4):
        p = flat & 0x03
        pad = (-len(p)) % 4
        if pad:
            p = np.concatenate([p, np.zeros(pad, dtype=np.uint8)])
        p = p.reshape(-1, 4)
        packed = (p[:, 0] << 6) | (p[:, 1] << 4) | (p[:, 2] << 2) | p[:, 3]
        return packed.astype(np.uint8).tobytes()

    if color_scheme in (ColorScheme.BWGBRY, ColorScheme.GRAYSCALE_16):
        idx = flat & 0x0F
        if color_scheme == ColorScheme.BWGBRY:
            lut = np.array([0, 1, 2, 3, 5, 6] + [0] * 10, dtype=np.uint8)
            idx = lut[idx]
        pad = len(idx) & 1
        if pad:
            idx = np.concatenate([idx, np.zeros(1, dtype=np.uint8)])
        idx = idx.reshape(-1, 2)
        packed = (idx[:, 0] << 4) | idx[:, 1]
        return packed.astype(np.uint8).tobytes()

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
