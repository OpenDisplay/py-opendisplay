"""Now-playing layout: album cover on the left, track details on the right.

Pure PIL, no optional dependencies. On a landscape panel the cover fills the
left square and title, artist and album fill the strip beside it. Panels without
room for a readable strip (portrait, square, nearly square) show the cover alone.

The text uses Pillow's built-in scalable font unless a .ttf/.otf file is passed.
That font only covers basic Latin, so accented and non-Latin characters render
as boxes with it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from epaper_dithering import DbsParams, DitherMode
from PIL import Image, ImageDraw, ImageFont

from .device import logical_size, prepare_composite
from .models.config import GlobalConfig
from .models.enums import Rotation

# The strip beside the cover must be at least this fraction of the panel height
# to hold readable text (800x480 gives 320 px = 0.67, 296x128 gives 1.31).
_MIN_TEXT_COLUMN = 0.4
_MARGIN = 0.05  # of panel height
_TITLE_SIZE = 0.085
_ARTIST_SIZE = 0.065
_ALBUM_SIZE = 0.05
_TITLE_LINES = 3
_ARTIST_LINES = 2
_ALBUM_LINES = 2
FontPath = str | os.PathLike[str]
"""A .ttf or .otf file for the track text."""

_ELLIPSIS = "…"
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """What is playing: the cover image and the track details to show."""

    cover: Image.Image | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    year: int | None = None

    @property
    def album_line(self) -> str | None:
        """Album and year as one line, e.g. ``Burlesque · 2010``."""
        parts = [p for p in (self.album, str(self.year) if self.year else None) if p]
        return " · ".join(parts) or None

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to draw."""
        return self.cover is None and not (self.title or self.artist or self.album_line)


def has_text_column(width: int, height: int) -> bool:
    """Whether a panel of this size gets track text beside the cover."""
    return width - height >= _MIN_TEXT_COLUMN * height


@dataclass(frozen=True, slots=True)
class _Layout:
    cover_edge: int
    cover_position: tuple[int, int]
    text: tuple[int, int, int] | None  # left, right, margin of the text column


def _layout(width: int, height: int) -> _Layout:
    if not has_text_column(width, height):
        edge = min(width, height)
        return _Layout(edge, ((width - edge) // 2, (height - edge) // 2), None)
    margin = round(height * _MARGIN)
    return _Layout(height, (0, 0), (height + margin, width - margin, margin))


def render_now_playing(now: NowPlaying, width: int, height: int, *, font: FontPath | None = None) -> Image.Image:
    """Lay out *now* as an RGB image of the panel's size, e.g. for a preview.

    For the panel itself use :func:`prepare_now_playing`, which keeps the text
    crisp instead of dithering it along with the cover.
    """
    canvas = Image.new("RGB", (width, height), _WHITE)
    layout = _layout(width, height)
    if now.cover is not None:
        canvas.paste(_square(now.cover, layout.cover_edge), layout.cover_position)
    if layout.text is not None:
        left, right, margin = layout.text
        _draw_text_column(canvas, now, left=left, right=right, margin=margin, fill=_BLACK, font=font)
    return canvas


def prepare_now_playing(  # pylint: disable=too-many-arguments
    now: NowPlaying,
    config: GlobalConfig,
    *,
    rotate: Rotation = Rotation.ROTATE_0,
    use_measured_palettes: bool = True,
    dither_mode: DitherMode = DitherMode.BURKES,
    dbs: DbsParams | bool | None = True,
    compress: bool = True,
    font: FontPath | None = None,
) -> tuple[bytes, bytes | None, Image.Image]:
    """Prepare *now* for upload with ``upload_prepared_image``.

    Only the cover is dithered (with refinement by default); the text is drawn
    without anti-aliasing in the panel's own black, so it stays sharp. CPU-bound:
    run it off the event loop.

    *font* is a .ttf or .otf file for the text. Without it Pillow's built-in font
    is used, which only covers basic Latin: accented and non-Latin characters
    show as boxes, so pass a font that covers the music library.
    """
    width, height = logical_size(config, rotate)
    layout = _layout(width, height)
    ink = Image.new("1", (width, height), 0)
    if layout.text is not None:
        left, right, margin = layout.text
        _draw_text_column(ink, now, left=left, right=right, margin=margin, fill=1, font=font)
    return prepare_composite(
        config,
        ink=ink,
        photo=_square(now.cover, layout.cover_edge) if now.cover is not None else None,
        photo_position=layout.cover_position,
        rotate=rotate,
        use_measured_palettes=use_measured_palettes,
        dither_mode=dither_mode,
        dbs=dbs,
        compress=compress,
    )


def _square(cover: Image.Image, edge: int) -> Image.Image:
    """Scale the cover into an edge x edge square, padding non-square art with white."""
    cover = cover.convert("RGB")
    if cover.size == (edge, edge):
        return cover
    scale = min(edge / cover.width, edge / cover.height)
    resized = cover.resize(
        (max(1, round(cover.width * scale)), max(1, round(cover.height * scale))),
        Image.Resampling.LANCZOS,
    )
    square = Image.new("RGB", (edge, edge), _WHITE)
    square.paste(resized, ((edge - resized.width) // 2, (edge - resized.height) // 2))
    return square


@dataclass(frozen=True, slots=True)
class _Block:
    """One wrapped text block (title, artist or album) and its font."""

    size: int
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    lines: tuple[str, ...]


# (relative size, minimum px) for title, artist and album. The minimum keeps small
# panels readable; when the text then no longer fits, lines are dropped instead.
_SIZES = ((_TITLE_SIZE, 16), (_ARTIST_SIZE, 13), (_ALBUM_SIZE, 11))
# Line limits for title, artist and album, tried in order until the text fits.
_LINE_LIMITS = (
    (_TITLE_LINES, _ARTIST_LINES, _ALBUM_LINES),
    (_TITLE_LINES, _ARTIST_LINES, 0),
    (2, _ARTIST_LINES, 0),
    (2, 1, 0),
    (1, 1, 0),
)


def _plan_text(now: NowPlaying, *, height: int, column: int, font: FontPath | None = None) -> list[_Block]:
    """Choose font sizes and wrapped lines that fit beside the cover."""
    available = height - 2 * round(height * _MARGIN)
    texts = (now.title, now.artist, now.album_line)
    blocks: list[_Block] = []
    for limits in _LINE_LIMITS:
        blocks = []
        for text, (relative, minimum), max_lines in zip(texts, _SIZES, limits, strict=True):
            if not text or not max_lines:
                continue
            size = max(minimum, round(height * relative))
            face = _font(size, font)
            blocks.append(_Block(size, face, tuple(_wrap(text, face, column, max_lines))))
        if _text_height(blocks, height) <= available:
            break
    return blocks


def _text_height(blocks: list[_Block], height: int) -> int:
    """Total height of the stacked blocks including the gaps between them."""
    gap = round(height * 0.04)
    return sum(len(b.lines) * _line_height(b.font) for b in blocks) + gap * max(0, len(blocks) - 1)


def _draw_text_column(  # pylint: disable=too-many-arguments
    canvas: Image.Image,
    now: NowPlaying,
    *,
    left: int,
    right: int,
    margin: int,
    fill: int | tuple[int, int, int],
    font: FontPath | None = None,
) -> None:
    height = canvas.height
    blocks = _plan_text(now, height=height, column=right - left, font=font)
    if not blocks:
        return
    gap = round(height * 0.04)
    y = max(margin, (height - _text_height(blocks, height)) // 2)
    draw = ImageDraw.Draw(canvas)
    if canvas.mode == "1":
        draw.fontmode = "1"  # no anti-aliasing: every pixel is ink or paper
    for block in blocks:
        line_height = _line_height(block.font)
        for i, line in enumerate(block.lines):
            draw.text((left, y + i * line_height), line, font=block.font, fill=fill)
        y += len(block.lines) * line_height + gap


def _font(size: int, path: FontPath | None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path is not None:
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    top, bottom = font.getbbox("Hgjy")[1::2]
    return round((bottom - top) * 1.25)


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int, max_lines: int) -> list[str]:
    """Greedy word wrap into at most *max_lines*, ending in an ellipsis when cut."""

    def fits(candidate: str) -> bool:
        return font.getlength(candidate) <= width

    lines: list[str] = []
    current = ""
    words = text.split()
    for index, word in enumerate(words):
        candidate = f"{current} {word}" if current else word
        if fits(candidate):
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
        while not fits(current):  # a single word wider than the column
            cut = _fit_prefix(current, font, width)
            lines.append(current[:cut])
            current = current[cut:]
            if len(lines) == max_lines:
                break
        if len(lines) == max_lines:
            current = " ".join([current, *words[index + 1 :]])
            break
    else:
        if current:
            lines.append(current)
        return lines[:max_lines]

    # Ran out of lines with text left over: end the last line in an ellipsis.
    last = lines[max_lines - 1]
    while last and not fits(last + _ELLIPSIS):
        last = last[:-1].rstrip()
    lines[max_lines - 1] = last + _ELLIPSIS
    return lines[:max_lines]


def _fit_prefix(word: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int) -> int:
    """Length of the longest prefix of *word* that fits, at least one character."""
    cut = len(word)
    while cut > 1 and font.getlength(word[:cut]) > width:
        cut -= 1
    return cut
