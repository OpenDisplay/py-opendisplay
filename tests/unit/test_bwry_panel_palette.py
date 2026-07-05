"""Tests for per-panel BWRY code tables and 4-gray table additions (M3)."""

from __future__ import annotations

from PIL import Image

from opendisplay.display_palettes import (
    PANELS_4GRAY,
    get_bwry_codes,
    get_gray4_codes,
)
from opendisplay.encoding.images import encode_2bpp


def test_get_bwry_codes_swaps_yellow_red_on_u8colors_4clr_panels() -> None:
    assert get_bwry_codes(0x001D) == (0, 1, 3, 2)
    assert get_bwry_codes(0x001E) == (0, 1, 3, 2)


def test_get_bwry_codes_default_is_identity() -> None:
    assert get_bwry_codes(55) == (0, 1, 2, 3)
    assert get_bwry_codes(None) == (0, 1, 2, 3)


def test_encode_2bpp_applies_code_table() -> None:
    img = Image.new("P", (4, 1))
    img.putdata([0, 1, 2, 3])  # black, white, yellow, red

    # identity: 0b00_01_10_11
    assert encode_2bpp(img) == bytes([0b00011011])
    # yellow/red swapped: 0b00_01_11_10
    assert encode_2bpp(img, codes=(0, 1, 3, 2)) == bytes([0b00011110])


def test_gray4_v2_table_includes_ep368() -> None:
    assert get_gray4_codes(0x0048) == (3, 2, 1, 0)


def test_panels_4gray_includes_new_ids() -> None:
    assert {0x0043, 0x0044, 0x0046, 0x0048, 0x004C} <= PANELS_4GRAY
