"""Tests for image encoding functions."""

import numpy as np
import pytest
from epaper_dithering import ColorScheme, DitherMode
from PIL import Image

from opendisplay.device import prepare_image
from opendisplay.encoding.images import encode_4bpp, encode_image, fit_image
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import FitMode


def _palette_image(pixels: list[list[int]]) -> Image.Image:
    """Create a palette-mode image from a 2D list of palette indices."""
    arr = np.array(pixels, dtype=np.uint8)
    return Image.fromarray(arr, mode="P")


def _p_mode_image(width: int, height: int, index: int = 1) -> Image.Image:
    """Create a P-mode image with a known palette.

    Index 0 = red (255,0,0), index 1 = green (0,255,0).
    All pixels are filled with ``index``.
    """
    img = Image.new("P", (width, height), index)
    palette = [0] * 768
    palette[0:3] = [255, 0, 0]
    palette[3:6] = [0, 255, 0]
    img.putpalette(palette)
    return img


class TestEncodeImageGrayscale16:
    """GRAYSCALE_16 uses 4bpp: 2 pixels per byte, high nibble first."""

    def test_single_pixel_black(self) -> None:
        """Palette index 0 (black) encodes to 0x00 in high nibble."""
        img = _palette_image([[0, 0]])
        result = encode_image(img, ColorScheme.GRAYSCALE_16)
        assert result == bytes([0x00])

    def test_single_pixel_white(self) -> None:
        """Palette index 15 (white) encodes to 0xF0 in high nibble."""
        img = _palette_image([[15, 0]])
        result = encode_image(img, ColorScheme.GRAYSCALE_16)
        assert result == bytes([0xF0])

    def test_two_pixels_nibble_packing(self) -> None:
        """Two pixels pack into one byte: high nibble = pixel 0, low nibble = pixel 1."""
        img = _palette_image([[3, 5]])
        result = encode_image(img, ColorScheme.GRAYSCALE_16)
        assert result == bytes([0x35])

    def test_all_sixteen_levels(self) -> None:
        """All 16 gray levels (0–15) encode without error."""
        img = _palette_image([[i, 15 - i] for i in range(8)])
        result = encode_image(img, ColorScheme.GRAYSCALE_16)
        assert len(result) == 8  # 8 rows × 1 byte/row (2 pixels per byte)

    def test_output_length(self) -> None:
        """Output size is ceil(width/2) × height bytes."""
        img = _palette_image([[0, 1, 2], [3, 4, 5]])
        result = encode_image(img, ColorScheme.GRAYSCALE_16)
        assert len(result) == 4  # ceil(3/2)=2 bytes/row × 2 rows


class TestEncodeBwgbrySplit:
    """BWGBRY_SPLIT (wire scheme 8): left half-plane then right half-plane."""

    def test_half_planes_pack_left_then_right(self) -> None:
        """4×2 image: left 2 cols then right 2 cols, with BWGBRY nibble remap."""
        # Row0: L0=0,L1=1 | R0=2,R1=3  → left 0x01, right 0x23
        # Row1: L0=4,L1=5 | R0=1,R1=0  → left 0x56 (4→5,5→6), right 0x10
        img = _palette_image([[0, 1, 2, 3], [4, 5, 1, 0]])
        row_major = encode_4bpp(img, bwgbry_mapping=True, half_planes=False)
        split = encode_4bpp(img, bwgbry_mapping=True, half_planes=True)
        assert row_major == bytes([0x01, 0x23, 0x56, 0x10])
        assert split == bytes([0x01, 0x56, 0x23, 0x10])
        assert len(split) == len(row_major)

    def test_encode_image_routes_split_packing(self) -> None:
        """encode_image(BWGBRY_SPLIT) applies the bwgbry remap and half-plane split."""
        img = _palette_image([[0, 1, 2, 3], [4, 5, 1, 0]])
        result = encode_image(img, ColorScheme.BWGBRY_SPLIT)
        assert result == bytes([0x01, 0x56, 0x23, 0x10])

    def test_split_matches_bwgbry_length_and_multiset(self) -> None:
        """Split output is a repacking of BWGBRY: same length and same byte multiset."""
        img = _palette_image([[0, 1, 2, 3], [4, 5, 1, 0]])
        split = encode_image(img, ColorScheme.BWGBRY_SPLIT)
        bwgbry = encode_image(img, ColorScheme.BWGBRY)
        assert len(split) == len(bwgbry)
        assert sorted(split) == sorted(bwgbry)

    def test_prepare_image_uses_split_packing(self) -> None:
        """prepare_image(BWGBRY_SPLIT) end-to-end matches encode_4bpp split packing."""
        caps = DeviceCapabilities(width=4, height=2, color_scheme=ColorScheme.BWGBRY_SPLIT)
        img = Image.new("RGB", (4, 2), (0, 0, 0))
        data, _compressed, dithered = prepare_image(
            img,
            capabilities=caps,
            use_measured_palettes=False,
            dither_mode=DitherMode.NONE,
            compress=False,
        )
        assert data == encode_4bpp(dithered, bwgbry_mapping=True, half_planes=True)


class TestEncodeSevenColor:
    """SEVEN_COLOR (wire scheme 7): 4bpp with an identity index map."""

    def test_identity_index_map(self) -> None:
        """Index 4 stays 4 (no BWGBRY remap, which would turn it into 5)."""
        img = _palette_image([[6, 4]])
        result = encode_image(img, ColorScheme.SEVEN_COLOR)
        assert result == bytes([0x64])


class TestFitImage:
    """Tests for fit_image covering mode preservation and P-mode conversion."""

    def test_stretch_returns_exact_target_size(self) -> None:
        img = Image.new("RGB", (20, 10), (128, 128, 128))
        result = fit_image(img, (10, 10), FitMode.STRETCH)
        assert result.size == (10, 10)

    def test_contain_returns_exact_target_size(self) -> None:
        img = Image.new("RGB", (20, 10), (128, 128, 128))
        result = fit_image(img, (10, 10), FitMode.CONTAIN)
        assert result.size == (10, 10)

    def test_cover_returns_exact_target_size(self) -> None:
        img = Image.new("RGB", (20, 10), (128, 128, 128))
        result = fit_image(img, (10, 10), FitMode.COVER)
        assert result.size == (10, 10)

    def test_crop_returns_exact_target_size(self) -> None:
        img = Image.new("RGB", (20, 10), (128, 128, 128))
        result = fit_image(img, (10, 10), FitMode.CROP)
        assert result.size == (10, 10)

    def test_stretch_preserves_l_mode(self) -> None:
        img = Image.new("L", (20, 10), 128)
        result = fit_image(img, (10, 10), FitMode.STRETCH)
        assert result.mode == "L"

    def test_contain_preserves_l_mode(self) -> None:
        img = Image.new("L", (20, 10), 128)
        result = fit_image(img, (10, 10), FitMode.CONTAIN)
        assert result.mode == "L"

    def test_cover_preserves_l_mode(self) -> None:
        img = Image.new("L", (20, 10), 128)
        result = fit_image(img, (10, 10), FitMode.COVER)
        assert result.mode == "L"

    def test_crop_preserves_l_mode(self) -> None:
        img = Image.new("L", (8, 8), 128)
        result = fit_image(img, (10, 10), FitMode.CROP)
        assert result.mode == "L"

    def test_crop_l_mode_pads_white(self) -> None:
        """Smaller-than-target L image gets white (255) padding."""
        img = Image.new("L", (4, 4), 100)
        result = fit_image(img, (10, 10), FitMode.CROP)
        assert result.mode == "L"
        assert result.size == (10, 10)
        assert result.getpixel((0, 0)) == 255
        assert result.getpixel((5, 5)) == 100

    def test_contain_l_mode_pads_white(self) -> None:
        """Aspect-ratio-preserving L-mode pad uses white fill."""
        img = Image.new("L", (5, 10), 100)
        result = fit_image(img, (10, 10), FitMode.CONTAIN)
        assert result.mode == "L"
        assert result.size == (10, 10)
        assert result.getpixel((0, 5)) == 255

    def test_stretch_converts_p_to_rgb(self) -> None:
        img = _p_mode_image(20, 10, index=1)
        result = fit_image(img, (10, 10), FitMode.STRETCH)
        assert result.mode == "RGB"

    def test_contain_converts_p_to_rgb(self) -> None:
        img = _p_mode_image(20, 10, index=1)
        result = fit_image(img, (10, 10), FitMode.CONTAIN)
        assert result.mode == "RGB"

    def test_cover_converts_p_to_rgb(self) -> None:
        img = _p_mode_image(20, 10, index=1)
        result = fit_image(img, (10, 10), FitMode.COVER)
        assert result.mode == "RGB"

    def test_crop_converts_p_to_rgb(self) -> None:
        img = _p_mode_image(8, 8, index=1)
        result = fit_image(img, (10, 10), FitMode.CROP)
        assert result.mode == "RGB"

    def test_crop_p_mode_preserves_colors(self) -> None:
        """P-mode CROP: palette colors survive, padding is white."""
        img = _p_mode_image(4, 4, index=1)  # green
        result = fit_image(img, (10, 10), FitMode.CROP)
        assert result.mode == "RGB"
        assert result.getpixel((0, 0)) == (255, 255, 255)
        assert result.getpixel((5, 5)) == (0, 255, 0)

    def test_contain_p_mode_preserves_colors(self) -> None:
        """P-mode CONTAIN: palette colors survive, padding is white."""
        img = _p_mode_image(5, 10, index=1)  # green, tall
        result = fit_image(img, (10, 10), FitMode.CONTAIN)
        assert result.mode == "RGB"
        assert result.getpixel((0, 5)) == (255, 255, 255)

    def test_stretch_p_mode_preserves_colors(self) -> None:
        """P-mode STRETCH: uniform green image stays green after resize."""
        img = _p_mode_image(20, 10, index=1)
        result = fit_image(img, (10, 10), FitMode.STRETCH)
        assert result.mode == "RGB"
        assert result.getpixel((5, 5)) == (0, 255, 0)

    def test_crop_larger_image_no_padding(self) -> None:
        """Image larger than target in both dimensions: pure center crop, no padding."""
        img = Image.new("RGB", (20, 20), (0, 100, 200))
        result = fit_image(img, (10, 10), FitMode.CROP)
        assert result.size == (10, 10)
        assert result.getpixel((5, 5)) == (0, 100, 200)

    def test_unknown_fit_mode_raises(self) -> None:
        img = Image.new("RGB", (10, 10))
        with pytest.raises(ValueError, match="Unknown fit mode"):
            fit_image(img, (10, 10), 99)  # type: ignore[arg-type]
