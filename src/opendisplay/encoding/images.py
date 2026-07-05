"""Image encoding and preprocessing for e-paper displays."""

from __future__ import annotations

import logging

import numpy as np
from epaper_dithering import ColorScheme
from PIL import Image, ImageOps

from ..models.enums import FitMode

_LOGGER = logging.getLogger(__name__)

# Fill color for CONTAIN and CROP padding (white, natural for e-paper)
_PAD_COLOR = (255, 255, 255)


def fit_image(
    image: Image.Image,
    target_size: tuple[int, int],
    fit: FitMode,
) -> Image.Image:
    """Fit an image to target dimensions using the specified strategy.

    Args:
        image: Source PIL Image
        target_size: (width, height) of the display
        fit: Fit strategy to apply

    Returns:
        Image with exact target dimensions
    """
    if fit == FitMode.STRETCH:
        return image.resize(target_size, Image.Resampling.LANCZOS)

    if fit == FitMode.CONTAIN:
        return ImageOps.pad(image, target_size, Image.Resampling.LANCZOS, color=_PAD_COLOR)

    if fit == FitMode.COVER:
        return ImageOps.fit(image, target_size, Image.Resampling.LANCZOS)

    if fit == FitMode.CROP:
        tw, th = target_size
        sw, sh = image.size

        # Crop region from source (centered, clamped to target size)
        crop_w, crop_h = min(sw, tw), min(sh, th)
        left = (sw - crop_w) // 2
        top = (sh - crop_h) // 2
        cropped = image.crop((left, top, left + crop_w, top + crop_h))

        # Paste centered onto white canvas if padding needed
        if crop_w == tw and crop_h == th:
            return cropped
        canvas = Image.new("RGB", target_size, _PAD_COLOR)
        paste_x = (tw - crop_w) // 2
        paste_y = (th - crop_h) // 2
        canvas.paste(cropped, (paste_x, paste_y))
        return canvas

    raise ValueError(f"Unknown fit mode: {fit}")


def encode_image(
    image: Image.Image,
    color_scheme: ColorScheme,
) -> bytes:
    """Encode image to display format based on color scheme.

    Args:
        image: Dithered palette image
        color_scheme: Display color scheme

    Returns:
        Encoded image bytes
    """

    if color_scheme == ColorScheme.MONO:
        return encode_1bpp(image)
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        # 3-color displays use bitplane encoding (handled separately)
        raise ValueError(f"Color scheme {color_scheme.name} requires bitplane encoding, use encode_bitplanes() instead")
    if color_scheme == ColorScheme.BWRY:
        return encode_2bpp(image)
    if color_scheme == ColorScheme.BWGBRY:
        # 6-color Spectra 6 display uses 4bpp with special firmware values
        # Palette indices 0-5 map to firmware values 0,1,2,3,5,6 (4 is skipped!)
        return encode_4bpp(image, bwgbry_mapping=True)
    if color_scheme == ColorScheme.GRAYSCALE_4:
        # 4-gray needs two 1-bit controller planes, not packed 2bpp; the packed
        # form is not accepted by any firmware path. prepare_image routes 4-gray
        # through encode_gray4_bitplanes() instead.
        raise ValueError(
            f"Color scheme {color_scheme.name} requires two 1-bit planes, use encode_gray4_bitplanes() instead"
        )
    if color_scheme == ColorScheme.GRAYSCALE_16:
        # 16-level grayscale uses 4bpp; palette indices 0-15 map directly (0=black, 15=white)
        return encode_4bpp(image)
    raise ValueError(f"Unsupported color scheme: {color_scheme}")


def encode_1bpp(image: Image.Image) -> bytes:
    """Encode image to 1-bit-per-pixel format (monochrome).

    Format: 8 pixels per byte, MSB first
    Palette index 0 = black (0), index 1 = white (1)

    Args:
        image: Palette image (mode 'P')

    Returns:
        Encoded bytes
    """
    if image.mode != "P":
        raise ValueError(f"Expected palette image, got {image.mode}")

    pixels = np.asarray(image)

    # Any non-zero palette index = white (bit set). packbits(axis=1) zero-pads
    # each row to a byte boundary, matching the per-row layout above.
    return np.packbits(pixels > 0, axis=1).tobytes()


def encode_2bpp(image: Image.Image, codes: tuple[int, int, int, int] | None = None) -> bytes:
    """Encode image to 2-bits-per-pixel format (4 colors).

    Format: 4 pixels per byte, MSB first
    Each 2-bit value maps to palette index (0-3)

    Args:
        image: Palette image (mode 'P')
        codes: Optional palette-index -> stored-nibble table. Used by BWRY panels
            whose native 4-color code order differs from the dither palette order
            (e.g. yellow/red swapped). Defaults to identity.

    Returns:
        Encoded bytes
    """
    if image.mode != "P":
        raise ValueError(f"Expected palette image, got {image.mode}")

    pixels = np.asarray(image)
    height, width = pixels.shape

    # Mask to 2 bits, remap through the panel code table if given, zero-pad the
    # width to a multiple of 4 (matches the per-row byte boundary), then pack
    # 4 pixels per byte MSB-first.
    p = (pixels & 0x03).astype(np.uint8)
    if codes is not None:
        p = np.asarray(codes, dtype=np.uint8)[p]
    pad = (-width) % 4
    if pad:
        p = np.pad(p, ((0, 0), (0, pad)))
    p = p.reshape(height, -1, 4)
    packed = (p[:, :, 0] << 6) | (p[:, :, 1] << 4) | (p[:, :, 2] << 2) | p[:, :, 3]
    return packed.astype(np.uint8).tobytes()


def encode_4bpp(image: Image.Image, bwgbry_mapping: bool = False) -> bytes:
    """Encode image to 4-bits-per-pixel format (16 colors).

    Format: 2 pixels per byte, MSB first
    Each 4-bit value maps to palette index (0-15)

    Used for BWGBRY (6-color Spectra 6) and GRAYSCALE_16 (16-level grayscale).
    For GRAYSCALE_16, palette indices 0-15 map directly to firmware values
    (0=black, 15=white), so no remapping is needed.

    Args:
        image: Palette image (mode 'P')
        bwgbry_mapping: If True, remap palette indices for BWGBRY firmware
                        (0→0, 1→1, 2→2, 3→3, 4→5, 5→6)

    Returns:
        Encoded bytes
    """
    if image.mode != "P":
        raise ValueError(f"Expected palette image, got {image.mode}")

    pixels = np.asarray(image)
    height, width = pixels.shape

    idx = (pixels & 0x0F).astype(np.uint8)

    # BWGBRY firmware color mapping (Spectra 6 display): palette indices to
    # firmware values 0→0, 1→1, 2→2, 3→3, 4→5, 5→6, everything else → 0
    # (preserving the previous dict.get(..., 0) default).
    if bwgbry_mapping:
        lut = np.array([0, 1, 2, 3, 5, 6] + [0] * 10, dtype=np.uint8)
        idx = lut[idx]

    # Zero-pad width to an even number (matches the per-row byte boundary),
    # then pack 2 pixels per byte, high nibble first.
    if width & 1:
        idx = np.pad(idx, ((0, 0), (0, 1)))
    idx = idx.reshape(height, -1, 2)
    packed = (idx[:, :, 0] << 4) | idx[:, :, 1]
    return packed.astype(np.uint8).tobytes()
