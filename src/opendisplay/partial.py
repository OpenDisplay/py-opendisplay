"""Partial rendering support for OpenDisplay BLE devices.

Provides PartialState (caller-owned mutable holder), the DiffStrategy protocol,
Segment dataclass, and built-in diff strategies.

Serialization format: struct header (magic + version + scalar fields) followed
by a 4-byte big-endian length-prefixed ``last_image`` blob.  This avoids the
pickle security surface while remaining compact and version-able.

``last_image`` stores raw palette bytes (1 byte per pixel, from
``PIL.Image.tobytes()`` on the dithered palette image).  The diff operates on
these palette bytes; the library re-encodes segments to wire format before
sending 0x77 packets.  ``bytes_per_pixel`` is always 1 (palette representation)
for images stored by the library.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Wire constants (mirrored from firmware feat/partial-rendering)
# ---------------------------------------------------------------------------

# NACK error codes returned by the device inside {0xFF, opcode, error, 0x00}
ERR_ETAG_MISMATCH = 0x01   # on 0x76: client must fall back to full transfer
ERR_MIXED_DATA    = 0x02   # on 0x71 or 0x77: aborted; etag cleared on device
ERR_SEGMENT_OOB   = 0x03   # on 0x77: aborted; etag cleared on device
ERR_PARTIAL_VERSION = 0x04 # on 0x76: client protocol version unsupported

NACK_PREFIX = 0xFF


def parse_nack(response: bytes) -> tuple[int, int] | None:
    """Return (opcode, error_code) if response is a 4-byte {0xFF, op, err, 0x00} NACK.

    Returns None for any other response shape (caller treats as ACK / handles
    via existing validators).
    """
    if len(response) == 4 and response[0] == NACK_PREFIX and response[3] == 0x00:
        return response[1], response[2]
    return None

# Segment header size in bytes: x(2)+y(2)+w(2)+h(2)+flags(1) = 9
SEGMENT_HEADER_SIZE = 9

# Minimum region size (pixels) below which we stop recursing
_MIN_REGION_PIXELS = 16


# ---------------------------------------------------------------------------
# Segment — geometry + raw palette pixels for one rectangular region
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One rectangular region of pixel data with its location.

    ``pixels`` holds raw palette bytes (1 byte per pixel) before wire encoding.
    ``plane``: 0 = PLANE_0 (new image), 1 = PLANE_1 (old image).
    The library assigns plane values; diff strategies always return plane=0.
    """

    x: int
    y: int
    width: int
    height: int
    pixels: bytes          # raw palette bytes (1 byte per pixel)
    plane: int = 0         # 0 = PLANE_0 (new), 1 = PLANE_1 (old)

    @property
    def pixel_count(self) -> int:
        return self.width * self.height


# ---------------------------------------------------------------------------
# DiffStrategy protocol
# ---------------------------------------------------------------------------

class DiffStrategy(Protocol):
    """Protocol for pluggable diff strategies.

    Implementations receive the old and new raw palette pixel buffers (1 byte
    per pixel) and return a list of Segment objects (plane=0, palette bytes).
    The library duplicates each segment for PLANE_1 with old-image pixels.

    Returning an empty list means "no changes detected; skip transfer".
    """

    def diff(
        self,
        old: bytes,
        new: bytes,
        width: int,
        height: int,
        bytes_per_pixel: int,
        max_segment_bytes: int,
    ) -> list[Segment]: ...


# ---------------------------------------------------------------------------
# Built-in strategy: FullImageStrategy
# ---------------------------------------------------------------------------

class FullImageStrategy:
    """Kill-switch strategy that always forces a full 0x71 upload.

    ``diff()`` always returns an empty list so the library falls back to a
    full-image transfer via the existing 0x71 path.
    """

    def diff(
        self,
        old: bytes,
        new: bytes,
        width: int,
        height: int,
        bytes_per_pixel: int,
        max_segment_bytes: int,
    ) -> list[Segment]:
        return []


# ---------------------------------------------------------------------------
# Built-in strategy: RecursiveBoundingBoxStrategy
# ---------------------------------------------------------------------------

class RecursiveBoundingBoxStrategy:
    """Recursive bounding-box diff strategy (default).

    Algorithm (per §3.3 of the partial-rendering plan):
    1. Compute the minimal bounding box of all changed pixels within the
       current region.
    2. If the box's pixel data fits within ``max_segment_bytes``, emit it.
    3. Otherwise split along the longer axis of the bounding box, recompute
       each child's minimal bounding box, and recurse.
    4. Stop recursing when the region is below ``min_region_pixels`` pixels
       and emit as-is to avoid pathological subdivision.

    Input ``old``/``new`` are raw palette bytes (1 byte per pixel).
    ``max_segment_bytes`` is the pixel-data budget per segment (segment header
    not included); the library computes this from the active MTU.
    """

    def __init__(self, min_region_pixels: int = _MIN_REGION_PIXELS) -> None:
        self._min_region_pixels = min_region_pixels

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def diff(
        self,
        old: bytes,
        new: bytes,
        width: int,
        height: int,
        bytes_per_pixel: int,
        max_segment_bytes: int,
    ) -> list[Segment]:
        """Return changed segments (plane=0, raw palette pixels)."""
        if old == new:
            return []

        segments: list[Segment] = []
        self._recurse(
            old, new, width, height, bytes_per_pixel,
            0, 0, width, height,          # initial region = full image
            max_segment_bytes, segments,
        )
        return segments

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bounding_box(
        old: bytes,
        new: bytes,
        img_width: int,
        bytes_per_pixel: int,
        rx: int,
        ry: int,
        rw: int,
        rh: int,
    ) -> tuple[int, int, int, int] | None:
        """Return minimal bounding box (x0, y0, x1_excl, y1_excl) of changed
        pixels within region (rx, ry, rw, rh), or None if no changes."""
        min_x = rw
        max_x = -1
        min_y = rh
        max_y = -1

        bpp = bytes_per_pixel
        for dy in range(rh):
            gy = ry + dy
            row_changed = False
            for dx in range(rw):
                gx = rx + dx
                old_off = (gy * img_width + gx) * bpp
                new_off = old_off
                if old[old_off : old_off + bpp] != new[new_off : new_off + bpp]:
                    if dx < min_x:
                        min_x = dx
                    if dx > max_x:
                        max_x = dx
                    row_changed = True
            if row_changed:
                if dy < min_y:
                    min_y = dy
                if dy > max_y:
                    max_y = dy

        if max_x < 0:
            return None
        return (rx + min_x, ry + min_y, rx + max_x + 1, ry + max_y + 1)

    @staticmethod
    def _extract_region(
        buf: bytes,
        img_width: int,
        bytes_per_pixel: int,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
    ) -> bytes:
        """Extract pixels from buf for rectangle [x0..x1) × [y0..y1)."""
        bpp = bytes_per_pixel
        row_bytes = (x1 - x0) * bpp
        parts: list[bytes] = []
        for y in range(y0, y1):
            off = (y * img_width + x0) * bpp
            parts.append(buf[off : off + row_bytes])
        return b"".join(parts)

    def _recurse(
        self,
        old: bytes,
        new: bytes,
        img_width: int,
        img_height: int,
        bytes_per_pixel: int,
        rx: int,
        ry: int,
        rw: int,
        rh: int,
        max_segment_bytes: int,
        out: list[Segment],
    ) -> None:
        """Recursively find changed regions within (rx, ry, rw, rh)."""
        if rw <= 0 or rh <= 0:
            return

        bb = self._bounding_box(old, new, img_width, bytes_per_pixel, rx, ry, rw, rh)
        if bb is None:
            return  # no changes in this region

        x0, y0, x1, y1 = bb
        bw = x1 - x0
        bh = y1 - y0
        pixel_count = bw * bh
        data_size = pixel_count * bytes_per_pixel

        if data_size <= max_segment_bytes or pixel_count <= self._min_region_pixels:
            # Fits (or too small to split further) — emit as-is
            pixels = self._extract_region(new, img_width, bytes_per_pixel, x0, y0, x1, y1)
            out.append(Segment(x=x0, y=y0, width=bw, height=bh, pixels=pixels, plane=0))
            return

        # Split along the longer axis of the bounding box
        if bw >= bh:
            # Split vertically (along x) at midpoint of bounding box
            mid = x0 + bw // 2
            # Left half: region from rx to mid
            self._recurse(old, new, img_width, img_height, bytes_per_pixel,
                          rx, ry, mid - rx, rh, max_segment_bytes, out)
            # Right half: region from mid to rx+rw
            self._recurse(old, new, img_width, img_height, bytes_per_pixel,
                          mid, ry, rx + rw - mid, rh, max_segment_bytes, out)
        else:
            # Split horizontally (along y) at midpoint of bounding box
            mid = y0 + bh // 2
            # Top half
            self._recurse(old, new, img_width, img_height, bytes_per_pixel,
                          rx, ry, rw, mid - ry, max_segment_bytes, out)
            # Bottom half
            self._recurse(old, new, img_width, img_height, bytes_per_pixel,
                          rx, mid, rw, ry + rh - mid, max_segment_bytes, out)


# ---------------------------------------------------------------------------
# Wire-format segment packing
# ---------------------------------------------------------------------------

def _build_segment_wire(seg: Segment, wire_pixels: bytes) -> bytes:
    """Encode one segment to its 0x77 wire representation.

    Uses *wire_pixels* rather than *seg.pixels* (which are palette bytes);
    the caller is responsible for encoding palette bytes → wire format.

    Wire format per segment: x(2BE) y(2BE) w(2BE) h(2BE) flags(1) pixels(N)
    """
    header = struct.pack(">HHHHB", seg.x, seg.y, seg.width, seg.height, seg.plane & 0x01)
    return header + wire_pixels


def pack_segments_into_packets(
    segments: list[tuple[Segment, bytes]],
    mtu: int,
    cmd_prefix: bytes = b"\x00\x77",
) -> list[bytes]:
    """Pack (segment, wire_pixels) pairs into 0x77 BLE packets.

    Uses space-filling (greedy largest-first) packing:
    - Sort by wire-pixel size descending.
    - For each packet, greedily pick the largest remaining segment that fits,
      then continue with smaller ones until no segment fits.
    - All input segments appear exactly once across all returned packets.

    Args:
        segments:   List of (Segment, wire_pixels) where wire_pixels is the
                    encoded pixel data for that segment.
        mtu:        Maximum total packet size in bytes (including cmd_prefix).
        cmd_prefix: 2-byte opcode prefix (default 0x0077 big-endian).

    Returns:
        List of complete BLE packet bytes.
    """
    if not segments:
        return []

    max_payload = mtu - len(cmd_prefix)

    # Pre-compute wire representation for each (segment, wire_pixels)
    wires: list[bytes] = [_build_segment_wire(seg, wp) for seg, wp in segments]
    sizes: list[int] = [len(w) for w in wires]

    # Sort indices by wire size descending (largest first)
    order = sorted(range(len(wires)), key=lambda i: sizes[i], reverse=True)
    remaining: list[int] = list(order)
    packets: list[bytes] = []

    while remaining:
        packet_parts: list[bytes] = []
        space = max_payload
        still_remaining: list[int] = []

        for idx in remaining:
            w = wires[idx]
            if len(w) <= space:
                packet_parts.append(w)
                space -= len(w)
            else:
                still_remaining.append(idx)

        if not packet_parts:
            # Segment alone is larger than payload — emit it alone to avoid
            # an infinite loop.  Upper layer should not produce such segments.
            idx = remaining[0]
            packets.append(cmd_prefix + wires[idx])
            remaining = remaining[1:]
            continue

        packets.append(cmd_prefix + b"".join(packet_parts))
        remaining = still_remaining

    return packets


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
