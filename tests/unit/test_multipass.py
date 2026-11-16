"""Encoding a multi-pass frame for a named drive mode (piece 3b, Task 5a).

Each pass frame is an ordinary 2 bit-per-pixel frame (4 pixels per byte, first pixel in the high
bits, rows padded to a byte); a pixel's code in pass p is bits [2p+1:2p] of its palette entry's
``codes``, and the pass frames are concatenated (Firmware_Unified f6d3a83 protocol header,
structs 0xB0; design note 2026-11-16-v3-3b-drive-modes-design.md).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from opendisplay.encoding.multipass import encode_multipass, mode_palette
from opendisplay.exceptions import ImageEncodingError
from opendisplay.models.panel_drive import CalibrationEntry, DisplayCalibration


def _entry(p0: int, p1: int, p2: int, lab: tuple[float, float, float]) -> CalibrationEntry:
    return CalibrationEntry(codes=p0 | (p1 << 2) | (p2 << 4), lab_l=lab[0], lab_a=lab[1], lab_b=lab[2])


# A hand-made 3-pass palette of four colours. Entry i uses code i in pass 0, code (3 - i) in
# pass 1 and code 1 in pass 2, so every pass is easy to read off by hand.
CALIBRATION = DisplayCalibration(
    instance_number=0,
    mode_index=1,
    flags=0,
    entries=(
        _entry(0, 3, 1, (10.0, 0.0, 0.0)),  # black
        _entry(1, 2, 1, (95.0, 0.0, 2.0)),  # white
        _entry(2, 1, 1, (85.0, -5.0, 80.0)),  # yellow
        _entry(3, 0, 1, (50.0, 60.0, 40.0)),  # red
    ),
)

# 5x2 indices: the fifth pixel of a row starts a second byte, which is padded with zeros.
INDICES = np.array([[0, 1, 2, 3, 1], [3, 2, 1, 0, 2]], dtype=np.uint8)


def test_exact_bytes_of_a_three_pass_frame() -> None:
    data = encode_multipass(INDICES, CALIBRATION, passes=3)

    pass0 = bytes([0b00_01_10_11, 0b01_00_00_00, 0b11_10_01_00, 0b10_00_00_00])
    pass1 = bytes([0b11_10_01_00, 0b10_00_00_00, 0b00_01_10_11, 0b01_00_00_00])
    pass2 = bytes([0b01_01_01_01, 0b01_00_00_00, 0b01_01_01_01, 0b01_00_00_00])
    assert data == pass0 + pass1 + pass2


def test_length_is_passes_times_rows_times_padded_row_bytes() -> None:
    assert len(encode_multipass(INDICES, CALIBRATION, passes=3)) == 3 * 2 * 2
    assert len(encode_multipass(np.zeros((4, 9), dtype=np.uint8), CALIBRATION, passes=2)) == 2 * 4 * 3


def test_a_pass_whose_codes_are_all_zero_is_an_all_zero_frame() -> None:
    calibration = DisplayCalibration(0, 1, 0, tuple(_entry(i, 0, 0, (50.0, 0.0, 0.0)) for i in range(4)))

    data = encode_multipass(INDICES, calibration, passes=2)

    assert data[4:] == bytes(4)
    assert data[:4] != bytes(4)


def test_codes_are_not_remapped_by_index_order() -> None:
    # Panel codes come from the entry, never from the index: an entry list in another order
    # gives the same codes for the same colours.
    reordered = DisplayCalibration(0, 1, 0, CALIBRATION.entries[::-1])
    flipped = (3 - INDICES).astype(np.uint8)

    assert encode_multipass(flipped, reordered, passes=3) == encode_multipass(INDICES, CALIBRATION, passes=3)


def test_an_index_outside_the_palette_raises() -> None:
    with pytest.raises(ImageEncodingError, match="palette"):
        encode_multipass(np.array([[4]], dtype=np.uint8), CALIBRATION, passes=3)


def test_more_passes_than_the_codes_hold_raises() -> None:
    with pytest.raises(ImageEncodingError, match="passes"):
        encode_multipass(INDICES, CALIBRATION, passes=9)


def test_mode_palette_keeps_the_entry_order_and_takes_white_by_lightness() -> None:
    palette = mode_palette(CALIBRATION)

    assert len(palette.entries) == 4
    lightness = [entry.appearance[0] for entry in palette.entries]
    assert lightness.index(max(lightness)) == 1  # the white entry, normalised to L* 100
    assert lightness[1] == pytest.approx(100.0)
    assert lightness[0] < lightness[3] < lightness[2]  # entry order kept
    assert palette.measured is False


def test_mode_palette_dithers_each_swatch_to_its_entry() -> None:
    from epaper_dithering import DitherMode, DitherOptions, dither_image

    palette = mode_palette(CALIBRATION)
    swatches = Image.new("RGB", (4, 1))
    for x, entry in enumerate(palette.entries):
        swatches.putpixel((x, 0), _lab_to_rgb(entry.appearance))

    indices = np.asarray(dither_image(swatches, palette, DitherOptions(mode=DitherMode.NONE)))

    assert indices.tolist() == [[0, 1, 2, 3]]


def test_an_empty_calibration_has_no_palette() -> None:
    with pytest.raises(ImageEncodingError, match="calibration"):
        mode_palette(DisplayCalibration(0, 1, 0, ()))


def _lab_to_rgb(lab: tuple[float, float, float]) -> tuple[int, int, int]:
    """CIELAB (D65) to 8-bit sRGB, clipped; enough to land a swatch on its own palette entry."""
    l_, a, b = lab
    fy = (l_ + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def f_inv(t: float) -> float:
        return t**3 if t**3 > 0.008856 else (t - 16 / 116) / 7.787

    x, y, z = 0.95047 * f_inv(fx), 1.0 * f_inv(fy), 1.08883 * f_inv(fz)
    rgb = (
        3.2406 * x - 1.5372 * y - 0.4986 * z,
        -0.9689 * x + 1.8758 * y + 0.0415 * z,
        0.0557 * x - 0.2040 * y + 1.0570 * z,
    )

    def gamma(c: float) -> int:
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return round(255 * c)

    return (gamma(rgb[0]), gamma(rgb[1]), gamma(rgb[2]))


# ── prepare_multipass: rotate -> fit -> dither against the mode's palette -> frames ─────────────

from pathlib import Path  # noqa: E402

from opendisplay.device.imaging import prepare_image, prepare_multipass  # noqa: E402
from opendisplay.protocol.config_parser import parse_config_response  # noqa: E402

ALPHA = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
COLOUR16 = next(m for m in ALPHA.display_modes if m.name == "colour16")
FULL = next(m for m in ALPHA.display_modes if m.name == "full")


def _photo(size: tuple[int, int] = (184, 360)) -> Image.Image:
    image = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            image.putpixel((x, y), ((x * 7) % 256, (y * 3) % 256, ((x + y) * 5) % 256))
    return image


def test_colour16_frame_has_three_passes_of_the_display_size() -> None:
    data, preview = prepare_multipass(_photo(), ALPHA, COLOUR16)

    assert COLOUR16.passes == 3
    assert len(data) == 3 * 360 * 46  # 184 px = 46 bytes per row
    assert preview.size == (184, 360)


def test_a_multi_pass_mode_without_its_calibration_raises() -> None:
    config = ALPHA.__class__(
        **{
            **ALPHA.__dict__,
            "length_prefixed_packets": [p for p in ALPHA.length_prefixed_packets if p.packet_type != 0xB0],
        }
    )

    with pytest.raises(ImageEncodingError, match="0xB0"):
        prepare_multipass(_photo(), config, COLOUR16)


def test_a_single_pass_mode_is_the_normal_frame() -> None:
    data, _ = prepare_multipass(_photo(), ALPHA, FULL)
    normal, _, _ = prepare_image(_photo(), config=ALPHA, compress=False)

    assert data == normal


def test_the_source_is_rotated_and_fitted_as_for_a_normal_upload() -> None:
    wide = _photo((360, 184))  # landscape source on a portrait panel: fitted, not rejected

    data, preview = prepare_multipass(wide, ALPHA, COLOUR16)

    assert len(data) == 3 * 360 * 46 and preview.size == (184, 360)


def test_a_calibration_of_another_mode_is_not_used() -> None:
    from opendisplay.models.panel_drive import LengthPrefixedPacket

    packets = []
    for p in ALPHA.length_prefixed_packets:
        if p.packet_type == 0xB0:
            body = bytearray(p.body)
            body[1] = 7  # mode_index of a mode that does not exist
            p = LengthPrefixedPacket(number=p.number, packet_type=0xB0, body=bytes(body))
        packets.append(p)
    config = ALPHA.__class__(**{**ALPHA.__dict__, "length_prefixed_packets": packets})

    with pytest.raises(ImageEncodingError, match="0xB0"):
        prepare_multipass(_photo(), config, COLOUR16)
