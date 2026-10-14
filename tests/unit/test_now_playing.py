"""Tests for the now-playing layout (cover left, track text right)."""

from __future__ import annotations

from PIL import Image

from opendisplay.now_playing import NowPlaying, _plan_text, _text_height, has_text_column, render_now_playing


def _cover(color: str = "red", size: int = 480) -> Image.Image:
    return Image.new("RGB", (size, size), color)


def _ink(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """True if anything other than white was drawn inside *box*."""
    region = image.crop(box).convert("L")
    return region.getextrema()[0] < 255


def test_landscape_panel_has_text_column() -> None:
    assert has_text_column(800, 480)
    assert has_text_column(296, 128)


def test_portrait_and_square_panels_have_no_text_column() -> None:
    assert not has_text_column(480, 800)
    assert not has_text_column(400, 400)
    assert not has_text_column(500, 480)  # 20 px strip is too narrow to read


def test_cover_left_text_right_on_landscape() -> None:
    now = NowPlaying(
        cover=_cover(), title="You Haven't Seen the Last of Me", artist="Cher", album="Burlesque", year=2010
    )
    image = render_now_playing(now, 800, 480)

    assert image.size == (800, 480)
    assert image.getpixel((10, 10)) == (255, 0, 0)  # cover fills the left square
    assert image.getpixel((470, 470)) == (255, 0, 0)
    assert _ink(image, (480, 0, 800, 480))  # text drawn to the right


def test_text_only_when_cover_missing() -> None:
    image = render_now_playing(NowPlaying(title="Radio", artist="Station"), 800, 480)
    assert not _ink(image, (0, 0, 480, 480))  # no cover, left stays white
    assert _ink(image, (480, 0, 800, 480))


def test_portrait_panel_shows_cover_only() -> None:
    image = render_now_playing(NowPlaying(cover=_cover(), title="Song"), 480, 800)
    assert image.size == (480, 800)
    assert image.getpixel((240, 400)) == (255, 0, 0)  # cover centred
    assert not _ink(image, (0, 0, 480, 150))  # white above the cover, no text
    assert not _ink(image, (0, 650, 480, 800))


def test_long_title_stays_inside_text_column() -> None:
    title = "A remarkably long track title that cannot possibly fit on a single line of the panel " * 3
    image = render_now_playing(NowPlaying(cover=_cover(), title=title, artist="Someone"), 800, 480)
    margin = round(480 * 0.05)
    assert not _ink(image, (800 - margin + 1, 0, 800, 480))  # right margin untouched


def test_unbreakable_word_is_truncated_not_overflowing() -> None:
    image = render_now_playing(NowPlaying(cover=_cover(), title="X" * 200), 800, 480)
    margin = round(480 * 0.05)
    assert not _ink(image, (800 - margin + 1, 0, 800, 480))


def test_small_panel_text_is_readable_and_fits() -> None:
    now = NowPlaying(
        cover=_cover(size=128),
        title="You Haven't Seen the Last of Me (Extended Version)",
        artist="Cher and a Very Long Featured Artist List",
        album="Burlesque (Original Motion Picture Soundtrack)",
        year=2010,
    )
    margin = round(128 * 0.05)
    blocks = _plan_text(now, height=128, column=296 - 128 - 2 * margin)
    assert blocks[0].size >= 16  # title
    assert all(block.size >= 11 for block in blocks)
    assert _text_height(blocks, 128) <= 128 - 2 * margin

    image = render_now_playing(now, 296, 128)
    assert not _ink(image, (128, 0, 296, margin))  # top margin untouched
    assert not _ink(image, (128, 128 - margin + 1, 296, 128))  # bottom margin untouched


def test_large_panel_keeps_all_three_blocks() -> None:
    now = NowPlaying(title="Title", artist="Artist", album="Album", year=2010)
    blocks = _plan_text(now, height=480, column=800 - 480 - 48)
    assert [len(b.lines) for b in blocks] == [1, 1, 1]
    assert blocks[0].size > blocks[1].size > blocks[2].size
