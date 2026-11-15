"""Source images are made opaque and sRGB before they are fitted and dithered.

Transparency resolves to white, which is what a panel shows where no ink is laid
down. A wide-gamut colour profile is applied, because palettes and dithering work
in sRGB and would otherwise read the raw values as if they were sRGB.
"""

from __future__ import annotations

import logging
import struct

import pytest
from PIL import Image

from opendisplay import ColorScheme, DeviceCapabilities, prepare_image
from opendisplay.encoding import normalize_source_image

WHITE = (255, 255, 255)

# (120, 160, 100) in Adobe RGB (1998) is (99, 161, 96) in sRGB: gamma 563/256 to
# linear, Adobe RGB -> XYZ -> sRGB matrices, sRGB transfer curve.
ADOBE_RGB_COLOUR = (120, 160, 100)
SAME_COLOUR_IN_SRGB = (99, 161, 96)


def _icc_profile(description: bytes, red: tuple, green: tuple, blue: tuple, gamma: float) -> bytes:
    """Build a minimal ICC v2 matrix/TRC display profile (D50-adapted primaries)."""

    def s15f16(value: float) -> bytes:
        return struct.pack(">i", round(value * 65536))

    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ \x00\x00\x00\x00" + s15f16(x) + s15f16(y) + s15f16(z)

    text = description + b"\x00"
    curve = b"curv\x00\x00\x00\x00" + struct.pack(">IH", 1, round(gamma * 256))
    tags = {
        b"desc": b"desc\x00\x00\x00\x00" + struct.pack(">I", len(text)) + text + bytes(12 + 67),
        b"cprt": b"text\x00\x00\x00\x00none\x00",
        b"wtpt": xyz(0.9642, 1.0, 0.8249),
        b"rXYZ": xyz(*red),
        b"gXYZ": xyz(*green),
        b"bXYZ": xyz(*blue),
        b"rTRC": curve,
        b"gTRC": curve,
        b"bTRC": curve,
    }
    table = struct.pack(">I", len(tags))
    body = b""
    data_start = 128 + 4 + 12 * len(tags)
    for signature, data in tags.items():
        table += signature + struct.pack(">II", data_start + len(body), len(data))
        body += data + bytes(-len(data) % 4)
    header = (
        struct.pack(">I", 128 + len(table) + len(body))
        + b"\x00\x00\x00\x00"  # preferred CMM
        + bytes([2, 0x10, 0, 0])  # version 2.1
        + b"mntrRGB XYZ "  # display class, RGB data, XYZ connection space
        + struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
        + b"acsp"
        + bytes(4 + 4 + 4 + 4 + 8 + 4)  # platform, flags, manufacturer, model, attributes, intent
        + s15f16(0.9642)
        + s15f16(1.0)
        + s15f16(0.8249)  # D50 illuminant
        + bytes(4 + 44)  # creator, reserved
    )
    assert len(header) == 128
    return header + table + body


ADOBE_RGB = _icc_profile(
    b"Adobe RGB (1998)",
    red=(0.60974, 0.31111, 0.01947),
    green=(0.20528, 0.62567, 0.06087),
    blue=(0.14919, 0.06322, 0.74457),
    gamma=563 / 256,
)
SRGB_NAMED = _icc_profile(
    b"sRGB IEC61966-2.1",
    red=(0.43607, 0.22249, 0.01392),
    green=(0.38515, 0.71687, 0.09708),
    blue=(0.14307, 0.06061, 0.71410),
    gamma=2.2,
)


def _with_profile(image: Image.Image, profile: bytes) -> Image.Image:
    image.info["icc_profile"] = profile
    return image


def _close(actual: tuple, expected: tuple, tolerance: int = 1) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


# ─── Transparency ────────────────────────────────────────────────────────────


def _palette_with_transparent_black() -> Image.Image:
    image = Image.new("P", (4, 4), 0)
    image.putpalette([0, 0, 0] + [255, 0, 0] * 255)
    image.info["transparency"] = 0
    return image


@pytest.mark.parametrize(
    "image",
    [
        Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
        Image.new("LA", (4, 4), (0, 0)),
        _palette_with_transparent_black(),
    ],
    ids=["rgba", "greyscale_alpha", "palette_transparent_index"],
)
def test_fully_transparent_pixels_become_white(image: Image.Image) -> None:
    """Whatever colour an encoder left under transparent pixels, the panel shows white."""
    result = normalize_source_image(image)

    assert result.mode == "RGB"
    assert result.getpixel((0, 0)) == WHITE


def test_partial_transparency_blends_with_white() -> None:
    half_black = Image.new("RGBA", (1, 1), (0, 0, 0, 128))

    assert _close(normalize_source_image(half_black).getpixel((0, 0)), (127, 127, 127))


@pytest.mark.parametrize("mode", ["RGB", "L", "P", "1"])
def test_opaque_image_without_a_profile_is_returned_unchanged(mode: str) -> None:
    image = Image.new(mode, (4, 4))

    assert normalize_source_image(image) is image


@pytest.mark.parametrize(
    "image",
    [Image.new("LA", (16, 8), (0, 0)), _palette_with_transparent_black().resize((16, 8))],
    ids=["greyscale_alpha", "palette_transparent_index"],
)
def test_transparent_image_reaches_the_panel_as_white(image: Image.Image) -> None:
    capabilities = DeviceCapabilities(16, 8, ColorScheme.MONO)

    data, _, _ = prepare_image(image, capabilities=capabilities, compress=False)

    assert data == b"\xff" * 16  # 1 bpp, every pixel white


# ─── Colour profiles ─────────────────────────────────────────────────────────


def test_wide_gamut_profile_is_converted_to_srgb() -> None:
    image = _with_profile(Image.new("RGB", (2, 2), ADOBE_RGB_COLOUR), ADOBE_RGB)

    result = normalize_source_image(image)

    assert _close(result.getpixel((0, 0)), SAME_COLOUR_IN_SRGB)
    assert "icc_profile" not in result.info  # the pixels are sRGB now


def test_srgb_profile_is_left_alone() -> None:
    image = _with_profile(Image.new("RGB", (2, 2), ADOBE_RGB_COLOUR), SRGB_NAMED)

    assert normalize_source_image(image) is image


def test_profile_is_applied_to_a_palette_image() -> None:
    image = Image.new("P", (2, 2), 0)
    image.putpalette(list(ADOBE_RGB_COLOUR) * 256)

    result = normalize_source_image(_with_profile(image, ADOBE_RGB))

    assert _close(result.getpixel((0, 0)), SAME_COLOUR_IN_SRGB)


def test_transparent_wide_gamut_image_is_flattened_and_converted() -> None:
    image = Image.new("RGBA", (2, 1), (*ADOBE_RGB_COLOUR, 255))
    image.putpixel((1, 0), (0, 0, 0, 0))

    result = normalize_source_image(_with_profile(image, ADOBE_RGB))

    assert _close(result.getpixel((0, 0)), SAME_COLOUR_IN_SRGB)
    assert result.getpixel((1, 0)) == WHITE


def test_unreadable_profile_is_logged_and_the_image_still_used(caplog: pytest.LogCaptureFixture) -> None:
    image = _with_profile(Image.new("RGB", (2, 2), ADOBE_RGB_COLOUR), b"not an icc profile")

    with caplog.at_level(logging.WARNING):
        result = normalize_source_image(image)

    assert result.getpixel((0, 0)) == ADOBE_RGB_COLOUR
    assert "colour profile" in caplog.text


def test_rgb_profile_on_a_greyscale_image_is_ignored() -> None:
    image = _with_profile(Image.new("L", (2, 2), 100), ADOBE_RGB)

    assert normalize_source_image(image) is image


def test_prepare_image_dithers_the_converted_colours() -> None:
    """A tagged image must come out like the same image converted to sRGB beforehand."""
    capabilities = DeviceCapabilities(32, 32, ColorScheme.BWGBRY)
    tagged = _with_profile(Image.new("RGB", (32, 32), ADOBE_RGB_COLOUR), ADOBE_RGB)
    converted = normalize_source_image(tagged.copy())
    untagged = Image.new("RGB", (32, 32), ADOBE_RGB_COLOUR)

    from_tagged, _, _ = prepare_image(tagged, capabilities=capabilities, compress=False)
    from_converted, _, _ = prepare_image(converted, capabilities=capabilities, compress=False)
    from_untagged, _, _ = prepare_image(untagged, capabilities=capabilities, compress=False)

    assert from_tagged == from_converted
    assert from_tagged != from_untagged
