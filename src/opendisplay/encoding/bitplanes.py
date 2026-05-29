"""Bitplane encoding for multi-color e-paper displays."""

from __future__ import annotations

import logging

import numpy as np
from epaper_dithering import ColorScheme
from PIL import Image

_LOGGER = logging.getLogger(__name__)


def encode_bitplanes(
    image: Image.Image,
    color_scheme: ColorScheme,
) -> tuple[bytes, bytes]:
    """Encode image to bitplane format for BWR/BWY displays.

    BWR/BWY displays use two bitplanes:
    - Plane 1 (BW): Black/White layer
    - Plane 2 (R/Y): Red/Yellow accent color layer

    Args:
        image: Dithered palette image
        color_scheme: Must be BWR or BWY

    Returns:
        Tuple of (plane1_bytes, plane2_bytes)

    Raises:
        ValueError: If color_scheme is not BWR or BWY
    """
    if color_scheme not in (ColorScheme.BWR, ColorScheme.BWY):
        raise ValueError(f"Bitplane encoding only supports BWR/BWY, got {color_scheme.name}")

    if image.mode != "P":
        raise ValueError(f"Expected palette image, got {image.mode}")

    pixels = np.array(image)
    height, width = pixels.shape

    # Calculate output size (1bpp, 8 pixels per byte)
    bytes_per_row = (width + 7) // 8
    plane1 = bytearray(bytes_per_row * height)  # BW plane
    plane2 = bytearray(bytes_per_row * height)  # R/Y plane

    # Palette mapping:
    # Index 0 = Black -> BW=0, R/Y=0
    # Index 1 = White -> BW=1, R/Y=0
    # Index 2 = Red/Yellow -> BW=0, R/Y=1

    for y in range(height):
        for x in range(width):
            byte_idx = y * bytes_per_row + x // 8
            bit_idx = 7 - (x % 8)  # MSB first

            palette_idx = pixels[y, x]

            if palette_idx == 1:
                # White - set BW plane
                plane1[byte_idx] |= 1 << bit_idx
            elif palette_idx == 2:
                # Red/Yellow - set R/Y plane
                plane2[byte_idx] |= 1 << bit_idx
            # else: palette_idx == 0 (black) - both planes stay 0

    return bytes(plane1), bytes(plane2)


def encode_gray4_bitplanes(
    image: Image.Image,
    gray_codes: tuple[int, int, int, int],
) -> tuple[bytes, bytes]:
    """Encode a 4-gray palette image to two 1-bit controller planes.

    Each pixel's dither level (palette index 0=black..3=white) is mapped through
    the panel's gray-code table to a 2-bit stored code; plane0 carries the code's
    bit0 and plane1 its bit1, matching the firmware's bbepSetPixel4Gray. The
    firmware streams these planes straight to PLANE_0/PLANE_1, so no on-device
    de-interleave is needed.

    Args:
        image: Dithered palette image (mode "P", indices 0..3)
        gray_codes: level -> stored 2-bit code (see display_palettes.get_gray4_codes)

    Returns:
        Tuple of (plane0_bytes, plane1_bytes)

    Raises:
        ValueError: If image is not a palette image
    """
    if image.mode != "P":
        raise ValueError(f"Expected palette image, got {image.mode}")

    pixels = np.array(image)
    height, width = pixels.shape
    bytes_per_row = (width + 7) // 8
    plane0 = bytearray(bytes_per_row * height)
    plane1 = bytearray(bytes_per_row * height)

    for y in range(height):
        for x in range(width):
            byte_idx = y * bytes_per_row + x // 8
            bit = 1 << (7 - (x % 8))  # MSB first
            code = gray_codes[int(pixels[y, x]) & 0x03]
            if code & 0x01:
                plane0[byte_idx] |= bit
            if code & 0x02:
                plane1[byte_idx] |= bit

    return bytes(plane0), bytes(plane1)
