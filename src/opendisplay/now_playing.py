"""Now-playing layout: the album cover plus title, artist, album and year.

Pure PIL, no optional dependencies. The layout follows the panel's shape:

* **Side by side**: when the long side is at least 1.4 x the short side and the
  strip beside the cover is at least 80 px, the cover fills the short-side
  square and the text fills the strip: to the right in landscape, below it in
  portrait.
* **Caption**: otherwise the cover shrinks (to no less than 60% of the short
  side) to make room for a title line and, if it fits, an artist line below it.
* **Cover only**: when even a one-line caption would shrink the cover further.

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

_SIDE_RATIO = 1.4  # long side / short side from which text goes beside the cover
_MIN_STRIP = 80  # px; narrower strips beside the cover cannot hold readable text
_MIN_CAPTION_COVER = 0.6  # a caption may shrink the cover to this share of the short side
_MARGIN = 0.05  # of the short side
_GAP = 0.04  # between text blocks, of the short side
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


@dataclass(frozen=True, slots=True)
class _Box:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _Layout:
    mode: str  # "side", "caption" or "cover"
    cover_edge: int
    cover_position: tuple[int, int]
    box: _Box | None = None  # where the text goes
    blocks: tuple[_Block, ...] = ()


def _layout(now: NowPlaying, width: int, height: int, font: FontPath | None = None) -> _Layout:
    """Pick the layout for a panel of *width* x *height* as mounted."""
    short, long = min(width, height), max(width, height)
    margin = round(short * _MARGIN)
    cover_only = _Layout("cover", short, ((width - short) // 2, (height - short) // 2))

    strip = long - short - 2 * margin
    if long >= _SIDE_RATIO * short and strip >= _MIN_STRIP:
        if width >= height:
            box = _Box(short + margin, margin, strip, short - 2 * margin)
        else:
            box = _Box(margin, short + margin, short - 2 * margin, strip)
        blocks = _plan_text(now, base=short, column=box.width, available=box.height, limits=_LINE_LIMITS, font=font)
        return _Layout("side", short, (0, 0), box, tuple(blocks))

    for limits in _CAPTION_LIMITS:
        blocks = _plan_text(now, base=short, column=width - 2 * margin, available=None, limits=(limits,), font=font)
        if not blocks:
            return cover_only  # nothing to write: keep the cover as large as possible
        edge = min(width, height - _text_height(blocks, short) - 2 * margin)
        if edge >= _MIN_CAPTION_COVER * short:
            box = _Box(margin, edge + margin, width - 2 * margin, height - edge - 2 * margin)
            return _Layout("caption", edge, ((width - edge) // 2, 0), box, tuple(blocks))
    return cover_only


def render_now_playing(now: NowPlaying, width: int, height: int, *, font: FontPath | None = None) -> Image.Image:
    """Lay out *now* as an RGB image of the panel's size, e.g. for a preview.

    For the panel itself use :func:`prepare_now_playing`, which keeps the text
    crisp instead of dithering it along with the cover.
    """
    canvas = Image.new("RGB", (width, height), _WHITE)
    layout = _layout(now, width, height, font)
    if now.cover is not None:
        canvas.paste(_square(now.cover, layout.cover_edge), layout.cover_position)
    _draw_text(canvas, layout, fill=_BLACK)
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
    layout = _layout(now, width, height, font)
    ink = Image.new("1", (width, height), 0)
    _draw_text(ink, layout, fill=1)
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


# Caption line limits: title and artist, then title alone.
_CAPTION_LIMITS = ((1, 1, 0), (1, 0, 0))


def _plan_text(  # pylint: disable=too-many-arguments
    now: NowPlaying,
    *,
    base: int,
    column: int,
    available: int | None,
    limits: tuple[tuple[int, int, int], ...],
    font: FontPath | None = None,
) -> list[_Block]:
    """Choose font sizes and wrapped lines, trying *limits* in order until they fit.

    Font sizes scale with *base* (the panel's short side). With *available* None
    the first limits are used as they are.
    """
    texts = (now.title, now.artist, now.album_line)
    blocks: list[_Block] = []
    for line_limits in limits:
        blocks = []
        for text, (relative, minimum), max_lines in zip(texts, _SIZES, line_limits, strict=True):
            if not text or not max_lines:
                continue
            size = max(minimum, round(base * relative))
            face = _font(size, font)
            blocks.append(_Block(size, face, tuple(_wrap(text, face, column, max_lines))))
        if available is None or _text_height(blocks, base) <= available:
            break
    return blocks


def _text_height(blocks: list[_Block] | tuple[_Block, ...], base: int) -> int:
    """Total height of the stacked blocks including the gaps between them."""
    gap = round(base * _GAP)
    return sum(len(b.lines) * _line_height(b.font) for b in blocks) + gap * max(0, len(blocks) - 1)


def _draw_text(canvas: Image.Image, layout: _Layout, *, fill: int | tuple[int, int, int]) -> None:
    """Draw the layout's text blocks, centred vertically in its text box."""
    if layout.box is None or not layout.blocks:
        return
    base = min(canvas.size)
    box = layout.box
    y = box.y + max(0, (box.height - _text_height(layout.blocks, base)) // 2)
    draw = ImageDraw.Draw(canvas)
    if canvas.mode == "1":
        draw.fontmode = "1"  # no anti-aliasing: every pixel is ink or paper
    for block in layout.blocks:
        line_height = _line_height(block.font)
        for i, line in enumerate(block.lines):
            draw.text((box.x, y + i * line_height), line, font=block.font, fill=fill)
        y += len(block.lines) * line_height + round(base * _GAP)


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
