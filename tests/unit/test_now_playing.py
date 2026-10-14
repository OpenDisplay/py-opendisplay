"""Tests for the now-playing layout: text beside, caption below, or cover only."""

from __future__ import annotations

import pytest
from PIL import Image, ImageFont

from opendisplay.now_playing import _LINE_LIMITS, NowPlaying, _layout, _plan_text, _text_height, render_now_playing

TRACK = NowPlaying(
    cover=Image.new("RGB", (480, 480), "red"),
    title="You Haven't Seen the Last of Me",
    artist="Cher",
    album="Burlesque",
    year=2010,
)
RED = (255, 0, 0)


def _ink(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """True if anything other than white was drawn inside *box*."""
    region = image.crop(box).convert("L")
    return region.getextrema()[0] < 255


# Sizes are bb_epaper panels as mounted (landscape unless noted).
@pytest.mark.parametrize(
    ("width", "height", "mode"),
    [
        (800, 480, "side"),
        (296, 128, "side"),
        (250, 122, "side"),
        (792, 272, "side"),
        (1360, 480, "side"),
        (480, 800, "side"),  # 800x480 mounted portrait: text below the cover
        (128, 296, "side"),  # 296x128 mounted portrait
        (400, 300, "caption"),
        (300, 400, "caption"),
        (1872, 1404, "caption"),
        (200, 200, "caption"),
        (152, 152, "caption"),
        (128, 80, "caption"),  # wide, but the strip beside the cover is too narrow
        (100, 60, "cover"),  # even a one-line caption would shrink the cover too far
    ],
)
def test_layout_mode_by_panel_size(width: int, height: int, mode: str) -> None:
    assert _layout(TRACK, width, height).mode == mode


def test_landscape_side_layout_puts_text_right_of_cover() -> None:
    image = render_now_playing(TRACK, 800, 480)
    assert image.size == (800, 480)
    assert image.getpixel((10, 10)) == RED  # cover fills the left square
    assert image.getpixel((470, 470)) == RED
    assert _ink(image, (480, 0, 800, 480))


def test_portrait_side_layout_puts_text_below_cover() -> None:
    image = render_now_playing(TRACK, 480, 800)
    assert image.getpixel((10, 10)) == RED  # cover fills the top square
    assert image.getpixel((470, 470)) == RED
    assert _ink(image, (0, 480, 480, 800))
    margin = round(480 * 0.05)
    assert not _ink(image, (480 - margin + 1, 480, 480, 800))  # right margin untouched


def test_caption_layout_shrinks_cover_and_writes_below() -> None:
    layout = _layout(TRACK, 400, 300)
    image = render_now_playing(TRACK, 400, 300)
    edge = layout.cover_edge
    assert 0.6 * 300 <= edge < 300
    assert image.getpixel((200, edge // 2)) == RED  # cover centred at the top
    assert not _ink(image, (0, 0, (400 - edge) // 2 - 1, edge))  # white beside the cover
    assert _ink(image, (0, edge, 400, 300))  # caption under it
    margin = round(300 * 0.05)
    assert not _ink(image, (0, 300 - margin + 1, 400, 300))  # bottom margin untouched


def test_caption_drops_artist_before_shrinking_cover_too_far() -> None:
    layout = _layout(TRACK, 128, 80)
    assert layout.mode == "caption"
    assert [len(block.lines) for block in layout.blocks] == [1]  # title only
    assert layout.cover_edge >= 0.6 * 80


def test_cover_only_when_nothing_else_fits() -> None:
    image = render_now_playing(TRACK, 100, 60)
    assert image.getpixel((50, 30)) == RED  # cover centred
    assert not _ink(image, (0, 0, 19, 60))  # white either side, no text
    assert not _ink(image, (81, 0, 100, 60))


def test_cover_without_text_keeps_full_size_cover() -> None:
    layout = _layout(NowPlaying(cover=TRACK.cover), 400, 300)
    assert layout.mode == "cover"
    assert layout.cover_edge == 300


def test_text_only_when_cover_missing() -> None:
    image = render_now_playing(NowPlaying(title="Radio", artist="Station"), 800, 480)
    assert not _ink(image, (0, 0, 480, 480))  # no cover, left stays white
    assert _ink(image, (480, 0, 800, 480))


def test_long_title_stays_inside_text_column() -> None:
    title = "A remarkably long track title that cannot possibly fit on a single line of the panel " * 3
    image = render_now_playing(NowPlaying(cover=TRACK.cover, title=title, artist="Someone"), 800, 480)
    margin = round(480 * 0.05)
    assert not _ink(image, (800 - margin + 1, 0, 800, 480))  # right margin untouched


def test_unbreakable_word_is_truncated_not_overflowing() -> None:
    image = render_now_playing(NowPlaying(cover=TRACK.cover, title="X" * 200), 800, 480)
    margin = round(480 * 0.05)
    assert not _ink(image, (800 - margin + 1, 0, 800, 480))


def test_small_panel_text_is_readable_and_fits() -> None:
    now = NowPlaying(
        cover=TRACK.cover,
        title="You Haven't Seen the Last of Me (Extended Version)",
        artist="Cher and a Very Long Featured Artist List",
        album="Burlesque (Original Motion Picture Soundtrack)",
        year=2010,
    )
    margin = round(128 * 0.05)
    available = 128 - 2 * margin
    blocks = _plan_text(now, base=128, column=296 - 128 - 2 * margin, available=available, limits=_LINE_LIMITS)
    assert blocks[0].size >= 16  # title
    assert all(block.size >= 11 for block in blocks)
    assert _text_height(blocks, 128) <= available

    image = render_now_playing(now, 296, 128)
    assert not _ink(image, (128, 0, 296, margin))  # top margin untouched
    assert not _ink(image, (128, 128 - margin + 1, 296, 128))  # bottom margin untouched


def test_large_panel_keeps_all_three_blocks() -> None:
    now = NowPlaying(title="Title", artist="Artist", album="Album", year=2010)
    blocks = _plan_text(now, base=480, column=800 - 480 - 48, available=480 - 48, limits=_LINE_LIMITS)
    assert [len(b.lines) for b in blocks] == [1, 1, 1]
    assert blocks[0].size > blocks[1].size > blocks[2].size


def test_custom_font_is_used_for_every_block(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[tuple[str, int]] = []
    original = ImageFont.truetype

    def _truetype(font: object, size: float = 10, **kwargs: object) -> ImageFont.FreeTypeFont:
        if isinstance(font, str):  # our path; Pillow's own default font comes in as bytes
            loaded.append((font, int(size)))
            return ImageFont.load_default(size=size)  # type: ignore[return-value]
        return original(font, size, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ImageFont, "truetype", _truetype)
    now = NowPlaying(title="Beyoncé", artist="Sigur Rós", album="Björk")
    render_now_playing(now, 800, 480, font="/fonts/NotoSans-Regular.ttf")
    assert {path for path, _ in loaded} == {"/fonts/NotoSans-Regular.ttf"}
    assert len({size for _, size in loaded}) == 3  # title, artist and album sizes


def test_missing_font_file_raises() -> None:
    with pytest.raises(OSError):
        render_now_playing(NowPlaying(title="Song"), 800, 480, font="/nonexistent/font.ttf")
