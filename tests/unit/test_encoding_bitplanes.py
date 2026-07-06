"""Tests for bitplane encoding (BWR and BWY displays)."""
import numpy as np
import pytest
from PIL import Image

from epaper_dithering import ColorScheme
from opendisplay.encoding.bitplanes import encode_bitplanes


class TestBitplaneEncoding:
    """Test bitplane encoding for BWR and BWY color schemes."""

    def test_bwr_encoding_black_pixel(self):
        """Test BWR encoding: black pixel should be (0,0)."""
        # Create 8x1 image with all black pixels (palette index 0)
        img_array = np.zeros((1, 8), dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWR)

        # Both planes should be all zeros (black = 0,0)
        assert len(plane1) == 1  # 8 pixels = 1 byte
        assert len(plane2) == 1
        assert plane1[0] == 0b00000000
        assert plane2[0] == 0b00000000

    def test_bwr_encoding_white_pixel(self):
        """Test BWR encoding: white pixel should be (1,0)."""
        # Create 8x1 image with all white pixels (palette index 1)
        img_array = np.ones((1, 8), dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWR)

        # Plane1 should be all ones, plane2 should be zeros (white = 1,0)
        assert len(plane1) == 1
        assert len(plane2) == 1
        assert plane1[0] == 0b11111111
        assert plane2[0] == 0b00000000

    def test_bwr_encoding_red_pixel(self):
        """Test BWR encoding: red pixel should be (1,1) - CRITICAL BUG FIX."""
        # Create 8x1 image with all red pixels (palette index 2)
        img_array = np.full((1, 8), 2, dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWR)

        # BOTH planes should be all ones for red (red = 1,1)
        assert len(plane1) == 1
        assert len(plane2) == 1
        assert plane1[0] == 0b11111111, "BWR red pixels must set plane1=1"
        assert plane2[0] == 0b11111111, "BWR red pixels must set plane2=1"

    def test_bwy_encoding_yellow_pixel(self):
        """Test BWY encoding: yellow pixel should be (0,1)."""
        # Create 8x1 image with all yellow pixels (palette index 2)
        img_array = np.full((1, 8), 2, dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWY)

        # Plane1 should be zeros, plane2 should be ones (yellow = 0,1)
        assert len(plane1) == 1
        assert len(plane2) == 1
        assert plane1[0] == 0b00000000, "BWY yellow pixels must set plane1=0"
        assert plane2[0] == 0b11111111, "BWY yellow pixels must set plane2=1"

    def test_bwr_mixed_pixels(self):
        """Test BWR encoding with mixed pixels in specific pattern."""
        # Pattern: [black, white, red, black, white, red, black, white]
        # Indices:  [  0,     1,   2,     0,     1,   2,     0,     1]
        img_array = np.array([[0, 1, 2, 0, 1, 2, 0, 1]], dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWR)

        # Expected plane1: white(1) and red(1) set bits at positions 1,2,4,5,7
        # MSB first: [0,1,1,0,1,1,0,1] = 0b01101101 = 0x6D
        expected_plane1 = 0b01101101

        # Expected plane2: only red(1) sets bits at positions 2,5
        # MSB first: [0,0,1,0,0,1,0,0] = 0b00100100 = 0x24
        expected_plane2 = 0b00100100

        assert plane1[0] == expected_plane1, f"Expected plane1=0x{expected_plane1:02x}, got 0x{plane1[0]:02x}"
        assert plane2[0] == expected_plane2, f"Expected plane2=0x{expected_plane2:02x}, got 0x{plane2[0]:02x}"

    def test_bwy_mixed_pixels(self):
        """Test BWY encoding with mixed pixels in specific pattern."""
        # Pattern: [black, white, yellow, black, white, yellow, black, white]
        # Indices:  [  0,     1,      2,     0,     1,      2,     0,     1]
        img_array = np.array([[0, 1, 2, 0, 1, 2, 0, 1]], dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWY)

        # Expected plane1: only white(1) sets bits at positions 1,4,7
        # MSB first: [0,1,0,0,1,0,0,1] = 0b01001001 = 0x49
        expected_plane1 = 0b01001001

        # Expected plane2: only yellow(1) sets bits at positions 2,5
        # MSB first: [0,0,1,0,0,1,0,0] = 0b00100100 = 0x24
        expected_plane2 = 0b00100100

        assert plane1[0] == expected_plane1, f"Expected plane1=0x{expected_plane1:02x}, got 0x{plane1[0]:02x}"
        assert plane2[0] == expected_plane2, f"Expected plane2=0x{expected_plane2:02x}, got 0x{plane2[0]:02x}"

    def test_multi_row_encoding(self):
        """Test bitplane encoding with multiple rows."""
        # 2x8 image: first row all white, second row all red
        img_array = np.array([
            [1, 1, 1, 1, 1, 1, 1, 1],  # Row 0: white
            [2, 2, 2, 2, 2, 2, 2, 2],  # Row 1: red
        ], dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        plane1, plane2 = encode_bitplanes(img, ColorScheme.BWR)

        # Should be 2 bytes total (1 byte per row)
        assert len(plane1) == 2
        assert len(plane2) == 2

        # Row 0: all white (1,0)
        assert plane1[0] == 0b11111111
        assert plane2[0] == 0b00000000

        # Row 1: all red (1,1) for BWR
        assert plane1[1] == 0b11111111
        assert plane2[1] == 0b11111111

    def test_invalid_color_scheme_raises_error(self):
        """Test that non-bitplane color schemes raise ValueError."""
        img_array = np.zeros((1, 8), dtype=np.uint8)
        img = Image.fromarray(img_array, mode='P')

        # MONO should raise ValueError
        with pytest.raises(ValueError, match="Bitplane encoding only supports BWR/BWY"):
            encode_bitplanes(img, ColorScheme.MONO)

        # BWRY should raise ValueError
        with pytest.raises(ValueError, match="Bitplane encoding only supports BWR/BWY"):
            encode_bitplanes(img, ColorScheme.BWRY)

    def test_non_palette_image_raises_error(self):
        """Test that non-palette images raise ValueError."""
        # Create RGB image instead of palette
        rgb_img = Image.new('RGB', (8, 1), color='white')

        with pytest.raises(ValueError, match="Expected palette image"):
            encode_bitplanes(rgb_img, ColorScheme.BWR)
