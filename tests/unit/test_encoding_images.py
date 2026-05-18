"""Tests for image encoding functions."""

import numpy as np
from epaper_dithering import ColorScheme
from PIL import Image

from opendisplay.encoding.images import encode_image


def _palette_image(pixels: list[list[int]]) -> Image.Image:
    """Create a palette-mode image from a 2D list of palette indices."""
    arr = np.array(pixels, dtype=np.uint8)
    return Image.fromarray(arr, mode="P")


class TestEncodeImageGrayscale8:
    """GRAYSCALE_8 uses 4bpp: 2 pixels per byte, high nibble first."""

    def test_single_pixel_black(self) -> None:
        """Palette index 0 (black) encodes to 0x00 in high nibble."""
        img = _palette_image([[0, 0]])
        result = encode_image(img, ColorScheme.GRAYSCALE_8)
        assert result == bytes([0x00])

    def test_single_pixel_white(self) -> None:
        """Palette index 7 (white) encodes to 0x70 in high nibble."""
        img = _palette_image([[7, 0]])
        result = encode_image(img, ColorScheme.GRAYSCALE_8)
        assert result == bytes([0x70])

    def test_two_pixels_nibble_packing(self) -> None:
        """Two pixels pack into one byte: high nibble = pixel 0, low nibble = pixel 1."""
        img = _palette_image([[3, 5]])
        result = encode_image(img, ColorScheme.GRAYSCALE_8)
        assert result == bytes([0x35])

    def test_all_eight_levels(self) -> None:
        """All 8 gray levels (0–7) encode without error."""
        img = _palette_image([[0, 1], [2, 3], [4, 5], [6, 7]])
        result = encode_image(img, ColorScheme.GRAYSCALE_8)
        assert len(result) == 4  # 4 rows × 1 byte/row (2 pixels per byte)

    def test_output_length(self) -> None:
        """Output size is ceil(width/2) × height bytes."""
        img = _palette_image([[0, 1, 2], [3, 4, 5]])
        result = encode_image(img, ColorScheme.GRAYSCALE_8)
        assert len(result) == 4  # ceil(3/2)=2 bytes/row × 2 rows
