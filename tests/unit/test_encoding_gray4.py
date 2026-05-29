"""Tests for 4-gray bitplane encoding (host-side de-interleave for GRAYSCALE_4)."""

import numpy as np
import pytest
from epaper_dithering import ColorScheme
from PIL import Image

from opendisplay import prepare_image
from opendisplay.display_palettes import _GRAY4_CODES_BASE, get_gray4_codes
from opendisplay.encoding.bitplanes import encode_gray4_bitplanes
from opendisplay.encoding.images import encode_2bpp
from opendisplay.models.capabilities import DeviceCapabilities

EP426_PANEL_IC = 0x0028  # uses the v2 gray-code table
BASE_PANEL_IC = 0x0008  # EP295: uses the base gray-code table


def _img(levels: np.ndarray) -> Image.Image:
    return Image.fromarray(levels.astype(np.uint8), mode="P")


def _deinterleave_like_firmware(image: Image.Image, codes: tuple[int, int, int, int]) -> tuple[bytes, bytes]:
    """Reference: how the original (validated) firmware split encode_2bpp output.

    For each pixel it read the 2-bit level, mapped it through the panel's gray
    LUT, then wrote stored-code bit0 -> plane0 and bit1 -> plane1 (PLANE_0/PLANE_1
    in bbepSetPixel4Gray order). Widths here are multiples of 8 so the 2bpp and
    1bpp row paddings line up.
    """
    packed = encode_2bpp(image)
    width, height = image.size
    bpr2 = (width + 3) // 4
    bpr1 = (width + 7) // 8
    plane0 = bytearray(bpr1 * height)
    plane1 = bytearray(bpr1 * height)
    for y in range(height):
        for x in range(width):
            level = (packed[y * bpr2 + x // 4] >> ((3 - (x % 4)) * 2)) & 0x03
            stored = codes[level]
            mask = 0x80 >> (x % 8)
            if stored & 0x01:
                plane0[y * bpr1 + x // 8] |= mask
            if stored & 0x02:
                plane1[y * bpr1 + x // 8] |= mask
    return bytes(plane0), bytes(plane1)


def test_matches_firmware_deinterleave_v2():
    """New host planes are bit-identical to the firmware's de-interleave (EP426/v2)."""
    rng = np.random.default_rng(1)
    levels = rng.integers(0, 4, size=(7, 800), dtype=np.uint8)  # all 4 levels, 800 wide
    img = _img(levels)
    codes = get_gray4_codes(EP426_PANEL_IC)

    p0, p1 = encode_gray4_bitplanes(img, codes)
    e0, e1 = _deinterleave_like_firmware(img, codes)
    assert p0 == e0
    assert p1 == e1


def test_matches_firmware_deinterleave_base():
    """Same equivalence holds for the base gray-code table (non-EP426 panels)."""
    rng = np.random.default_rng(2)
    levels = rng.integers(0, 4, size=(5, 16), dtype=np.uint8)
    img = _img(levels)
    codes = get_gray4_codes(BASE_PANEL_IC)

    p0, p1 = encode_gray4_bitplanes(img, codes)
    e0, e1 = _deinterleave_like_firmware(img, codes)
    assert p0 == e0
    assert p1 == e1


def test_endpoints_black_and_white():
    """Black (level 0) and white (level 3) are table-independent: black->code 3, white->code 0."""
    for panel in (EP426_PANEL_IC, BASE_PANEL_IC):
        codes = get_gray4_codes(panel)
        assert codes[0] == 0b11  # black -> both plane bits set
        assert codes[3] == 0b00  # white -> both plane bits clear

    black = _img(np.zeros((1, 8), dtype=np.uint8))
    p0, p1 = encode_gray4_bitplanes(black, get_gray4_codes(EP426_PANEL_IC))
    assert p0[0] == 0xFF and p1[0] == 0xFF

    white = _img(np.full((1, 8), 3, dtype=np.uint8))
    p0, p1 = encode_gray4_bitplanes(white, get_gray4_codes(EP426_PANEL_IC))
    assert p0[0] == 0x00 and p1[0] == 0x00


def test_plane_lengths():
    """Each plane is one 1bpp frame (row-padded to 8px)."""
    img = _img(np.zeros((10, 12), dtype=np.uint8))
    p0, p1 = encode_gray4_bitplanes(img, get_gray4_codes(EP426_PANEL_IC))
    assert len(p0) == len(p1) == ((12 + 7) // 8) * 10


def test_v2_and_base_differ_on_midgrays():
    """The two tables agree on black/white but swap the mid-grays."""
    v2 = get_gray4_codes(EP426_PANEL_IC)
    base = get_gray4_codes(BASE_PANEL_IC)
    assert v2[0] == base[0] and v2[3] == base[3]
    assert v2[1] != base[1] and v2[2] != base[2]


def test_get_gray4_codes_none_defaults_to_base():
    """An unknown panel (None) falls back to the base table, not a crash."""
    assert get_gray4_codes(None) == _GRAY4_CODES_BASE


def test_encode_gray4_rejects_non_palette_image():
    """Only palette ('P') images carry dither levels; anything else is a caller error."""
    with pytest.raises(ValueError, match="palette image"):
        encode_gray4_bitplanes(Image.new("L", (8, 1)), get_gray4_codes(EP426_PANEL_IC))


def test_prepare_image_gray4_concatenates_planes():
    """prepare_image routes GRAYSCALE_4 through the two-plane encoder and concatenates plane0+plane1."""
    levels = np.tile(np.array([0, 64, 128, 192, 255, 128, 64, 0], dtype=np.uint8), (2, 1))
    src = Image.fromarray(levels, mode="L")
    caps = DeviceCapabilities(width=8, height=2, color_scheme=ColorScheme.GRAYSCALE_4)

    data, _, dithered = prepare_image(src, capabilities=caps, panel_ic_type=EP426_PANEL_IC, compress=False)

    p0, p1 = encode_gray4_bitplanes(dithered, get_gray4_codes(EP426_PANEL_IC))
    assert data == p0 + p1
