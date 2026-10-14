"""Tests for prepare_composite: dither only the photo, keep ink crisp."""

from __future__ import annotations

import dataclasses

import pytest
from epaper_dithering import ColorScheme, DitherMode, dither_image
from PIL import Image, ImageDraw
from test_sendspin import _config, _display

from opendisplay import logical_size, prepare_composite
from opendisplay.display_palettes import get_palette_for_display
from opendisplay.models.config import GlobalConfig

SPECTRA = 0x0023


def _spectra(width: int = 800, height: int = 480, rotation: int = 0) -> GlobalConfig:
    display = dataclasses.replace(
        _display(width, height),
        panel_ic_type=SPECTRA,
        color_scheme=ColorScheme.BWGBRY.value,
        rotation=rotation,
    )
    return _config(display)


def _indices(color: tuple[int, int, int]) -> int:
    palette = get_palette_for_display(SPECTRA, ColorScheme.BWGBRY, True)
    pixel = dither_image(Image.new("RGB", (1, 1), color), palette, mode=DitherMode.NONE).getpixel((0, 0))
    assert isinstance(pixel, int)
    return pixel


def _ink_with_text(size: tuple[int, int]) -> Image.Image:
    ink = Image.new("1", size, 0)
    draw = ImageDraw.Draw(ink)
    draw.fontmode = "1"
    draw.text((500, 200), "Walk of Fame", fill=1)
    return ink


def test_text_area_holds_only_pure_black_and_white() -> None:
    config = _spectra()
    photo = Image.new("RGB", (480, 480), (150, 90, 60))  # a mid tone that must dither
    _, _, frame = prepare_composite(config, ink=_ink_with_text((800, 480)), photo=photo, compress=False)

    text_column = set(frame.crop((480, 0, 800, 480)).get_flattened_data())
    assert text_column == {_indices((255, 255, 255)), _indices((0, 0, 0))}
    assert len(set(frame.crop((0, 0, 480, 480)).get_flattened_data())) > 1  # the photo is dithered


def test_without_photo_frame_is_white_with_black_ink() -> None:
    ink = Image.new("1", (800, 480), 0)
    ink.paste(1, (10, 10, 20, 20))
    _, _, frame = prepare_composite(_spectra(), ink=ink, compress=False)
    assert frame.getpixel((15, 15)) == _indices((0, 0, 0))
    assert frame.getpixel((400, 400)) == _indices((255, 255, 255))


def test_rotated_panel_lays_out_in_logical_orientation() -> None:
    config = _spectra(rotation=90)
    assert logical_size(config) == (480, 800)
    ink = Image.new("1", (480, 800), 0)
    ink.paste(1, (0, 0, 10, 10))  # logical top-left corner
    _, _, frame = prepare_composite(config, ink=ink, compress=False)
    assert frame.size == (800, 480)  # rotated back to the panel
    black = _indices((0, 0, 0))
    corners = [frame.getpixel(p) for p in ((0, 0), (799, 0), (0, 479), (799, 479))]
    assert corners.count(black) == 1  # the mark survived the rotation


def test_matches_prepare_image_encoding_for_plain_frames() -> None:
    """With an empty mask and no photo the frame is all white: same bytes as
    prepare_image on a white image, so the encoder path is unchanged."""
    from opendisplay import prepare_image

    config = _spectra()
    composite = prepare_composite(config, ink=Image.new("1", (800, 480), 0), compress=False)
    reference = prepare_image(Image.new("RGB", (800, 480), "white"), config=config, compress=False)
    assert composite[0] == reference[0]


def test_wrong_ink_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="ink must be"):
        prepare_composite(_spectra(), ink=Image.new("1", (10, 10), 0))


def test_now_playing_text_is_not_dithered() -> None:
    from opendisplay.now_playing import NowPlaying, prepare_now_playing

    now = NowPlaying(
        cover=Image.new("RGB", (480, 480), (150, 90, 60)), title="Walk of Fame", artist="Miley Cyrus", year=2025
    )
    _, _, frame = prepare_now_playing(now, _spectra(), dbs=False, compress=False)
    text_column = set(frame.crop((480, 0, 800, 480)).get_flattened_data())
    assert text_column == {_indices((255, 255, 255)), _indices((0, 0, 0))}
