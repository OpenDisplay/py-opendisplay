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


def test_get_bwry_codes_ep29yr_168x384_is_identity() -> None:
    # Verified on a Solum M3 2.7" (GDEY029F51H): swapping renders red as yellow (#161).
    assert get_bwry_codes(0x001E) == (0, 1, 2, 3)


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


def test_reorder_dual_interleaved_matches_el058_hardware_order() -> None:
    from opendisplay.encoding.images import reorder_dual_interleaved

    # 16x4 at 2bpp: 4 bytes per row, 2 per half. Byte value = row * 16 + column byte.
    width, height = 16, 4
    data = bytes(r * 16 + c for r in range(height) for c in range(4))
    out = reorder_dual_interleaved(data, width, height)

    right = [r * 16 + c for r in (0, 3, 1, 2) for c in (2, 3)]
    left = [r * 16 + c for r in (0, 3, 1, 2) for c in (0, 1)]
    assert out == bytes(right + left)


def test_reorder_dual_interleaved_matches_reference_el058() -> None:
    import random

    from opendisplay.encoding.images import reorder_dual_interleaved

    def reference(data: bytes, width: int, height: int) -> bytes:
        row = width // 4
        half = row // 2
        out = bytearray()
        for offset in (half, 0):
            for k in range(height // 2):
                for r in (k, height - 1 - k):
                    out += data[r * row + offset : r * row + offset + half]
        return bytes(out)

    data = random.Random(0).randbytes(792 * 272 // 4)
    assert reorder_dual_interleaved(data, 792, 272) == reference(data, 792, 272)


def test_reorder_dual_interleaved_rejects_odd_geometry() -> None:
    import pytest

    from opendisplay.encoding.images import reorder_dual_interleaved

    with pytest.raises(ValueError):
        reorder_dual_interleaved(bytes(3 * 4), 12, 4)  # 3-byte rows cannot split
    with pytest.raises(ValueError):
        reorder_dual_interleaved(bytes(4 * 3), 16, 3)  # odd height


def test_prepare_image_reorders_dual_interleaved_panel() -> None:
    from opendisplay.device import prepare_image
    from opendisplay.encoding.images import reorder_dual_interleaved
    from opendisplay.models.capabilities import DeviceCapabilities
    from opendisplay.models.enums import ColorScheme

    caps = DeviceCapabilities(width=16, height=4, color_scheme=ColorScheme.BWRY)
    img = Image.new("RGB", (16, 4), "white")
    img.putpixel((15, 0), (255, 0, 0))

    plain, _, _ = prepare_image(img, capabilities=caps, panel_ic_type=0x0040, compress=False)
    dual, _, _ = prepare_image(img, capabilities=caps, panel_ic_type=0xF003, compress=False)
    assert dual == reorder_dual_interleaved(plain, 16, 4)
    assert dual != plain
