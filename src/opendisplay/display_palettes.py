"""Palettes and panel capability data for e-paper displays.

epaper-dithering 7 ships no palettes, so py-opendisplay owns them: an idealised palette per
colour scheme, plus measured palettes for panels that have been measured.
"""

from __future__ import annotations

from functools import cache

from epaper_dithering import Palette

from .models.enums import ColorScheme

# Panel IDs that support 4-gray mode (from firmware mapEpd)
PANELS_4GRAY: frozenset[int] = frozenset(
    {
        0x0008,  # EP295_128x296_4GRAY
        0x0015,  # EP75_800x480_4GRAY
        0x0016,  # EP75_800x480_4GRAY_V2
        0x0018,  # EP29_128x296_4GRAY
        0x0028,  # EP426_800x480_4GRAY
        0x002F,  # EP29Z_128x296_4GRAY
        0x0031,  # EP213Z_122x250_4GRAY
        0x003C,  # EP75_800x480_4GRAY_GEN2
        0x0043,  # newer 4-gray panel
        0x0044,  # newer 4-gray panel
        0x0046,  # newer 4-gray panel
        0x0048,  # EP368_792x528_4GRAY
        0x004C,  # newer 4-gray panel
    }
)

# Per-panel 4-gray code tables, mirroring bb_epaper's pColorLookup (bb_ep.inl).
# Maps dither level (0=black .. 3=white) -> the 2-bit code stored in the panel's
# two controller planes; plane0 = code bit0, plane1 = code bit1 (bbepSetPixel4Gray).
# Both endpoints map black->3 and white->0; only EP426 swaps the mid-grays (v2).
_GRAY4_CODES_BASE: tuple[int, int, int, int] = (3, 1, 2, 0)  # u8Colors_4gray
_GRAY4_CODES_V2: tuple[int, int, int, int] = (3, 2, 1, 0)  # u8Colors_4gray_v2
_GRAY4_CODES_BY_PANEL: dict[int, tuple[int, int, int, int]] = {
    0x0028: _GRAY4_CODES_V2,  # EP426_800x480_4GRAY
    0x0048: _GRAY4_CODES_V2,  # EP368_792x528_4GRAY
}


def get_gray4_codes(panel_ic_type: int | None) -> tuple[int, int, int, int]:
    """Return the 4-gray level->stored-code table for a panel (bb_epaper parity)."""
    if panel_ic_type is None:
        return _GRAY4_CODES_BASE
    return _GRAY4_CODES_BY_PANEL.get(panel_ic_type, _GRAY4_CODES_BASE)


# Per-panel BWRY palette-index -> stored-nibble tables, mirroring bb_epaper's
# 4-color swatch tables (bb_ep.inl). The dither palette orders indices as
# black=0, white=1, yellow=2, red=3. Most YR panels use u8Colors_4clr_v2, whose
# native codes match that order. Panel 0x001D uses u8Colors_4clr, where
# native code 2=red and 3=yellow, so yellow/red must be swapped on the wire
# (the firmware direct-write path streams the nibble raw). 0x001E is also listed
# with u8Colors_4clr in bb_epaper, but on hardware (Solum M3 2.7") the identity
# order is correct (#161).
_BWRY_CODES_DEFAULT: tuple[int, int, int, int] = (0, 1, 2, 3)  # u8Colors_4clr_v2
_BWRY_CODES_SWAPPED: tuple[int, int, int, int] = (0, 1, 3, 2)  # u8Colors_4clr
_BWRY_CODES_BY_PANEL: dict[int, tuple[int, int, int, int]] = {
    0x001D: _BWRY_CODES_SWAPPED,  # EP29YR_128x296
}


def get_bwry_codes(panel_ic_type: int | None) -> tuple[int, int, int, int]:
    """Return the BWRY palette-index->stored-nibble table for a panel."""
    if panel_ic_type is None:
        return _BWRY_CODES_DEFAULT
    return _BWRY_CODES_BY_PANEL.get(panel_ic_type, _BWRY_CODES_DEFAULT)


_RGB = tuple[int, int, int]

_BLACK, _WHITE = ("black", "#000000"), ("white", "#ffffff")
_YELLOW, _RED = ("yellow", "#ffff00"), ("red", "#ff0000")
_BLUE, _GREEN, _ORANGE = ("blue", "#0000ff"), ("green", "#00ff00"), ("orange", "#ff8000")
_SPECTRA6 = (_BLACK, _WHITE, _YELLOW, _RED, _BLUE, _GREEN)

# Ink names and nominal colours per scheme, in the palette-index order the encoders expect
# (the order of epaper-dithering 6.x's built-in schemes).
_SCHEME_INKS: dict[ColorScheme, tuple[tuple[str, str], ...]] = {
    ColorScheme.MONO: (_BLACK, _WHITE),
    ColorScheme.BWR: (_BLACK, _WHITE, _RED),
    ColorScheme.BWY: (_BLACK, _WHITE, _YELLOW),
    ColorScheme.BWRY: (_BLACK, _WHITE, _YELLOW, _RED),
    ColorScheme.BWGBRY: _SPECTRA6,
    ColorScheme.BWGBRY_SPLIT: _SPECTRA6,
    ColorScheme.SEVEN_COLOR: (*_SPECTRA6, _ORANGE),
    ColorScheme.GRAYSCALE_4: (_BLACK, ("gray1", "#555555"), ("gray2", "#aaaaaa"), _WHITE),
    ColorScheme.GRAYSCALE_16: (
        _BLACK,
        *((f"gray{i}", "#" + f"{i * 0x11:02x}" * 3) for i in range(1, 15)),
        _WHITE,
    ),
}

# Measured sRGB per ink, in the scheme's ink order, carried over from epaper-dithering 6.x.
_SPECTRA6_V2: tuple[str, tuple[_RGB, ...]] = (
    "Waveshare 7.3in Spectra 6, measured 2026-03-15 (6.x SPECTRA_7_3_6COLOR_V2)",
    ((31, 24, 41), (168, 180, 182), (180, 173, 0), (113, 24, 19), (36, 70, 139), (50, 84, 60)),
)
_MEASURED: dict[tuple[int, ColorScheme], tuple[str, tuple[_RGB, ...]]] = {
    # Spectra 7.3" 6-colour (ep73_spectra_800x480).
    (35, ColorScheme.BWGBRY): _SPECTRA6_V2,
    # Spectra 6 13.3" dual-controller (T133A01, Seeed reTerminal E1004). Keyed on
    # BWGBRY_SPLIT because firmware requires scheme 8 for split panels; same inks as the
    # 7.3", so it borrows that measurement until the T133A01 is measured.
    (66, ColorScheme.BWGBRY_SPLIT): _SPECTRA6_V2,
    # 3.97" BWRY (ep397yr_800x480).
    (55, ColorScheme.BWRY): (
        "EP397YR 3.97in BWRY (6.x BWRY_3_97)",
        ((10, 7, 14), (173, 178, 174), (172, 128, 0), (85, 24, 14)),
    ),
}


def _hex_rgb(value: str) -> _RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


@cache
def _ideal_palette(scheme: ColorScheme) -> Palette:
    return Palette.from_srgb([(_hex_rgb(nominal), name, nominal) for name, nominal in _SCHEME_INKS[scheme]])


@cache
def _measured_palette(panel_ic_type: int, scheme: ColorScheme) -> Palette | None:
    measured = _MEASURED.get((panel_ic_type, scheme))
    if measured is None:
        return None
    source, colors = measured
    inks = _SCHEME_INKS[scheme]
    return Palette.from_srgb(
        [(rgb, name, nominal) for rgb, (name, nominal) in zip(colors, inks, strict=True)],
        measured=True,
    ).with_source(source)


def get_palette_for_display(
    panel_ic_type: int | None,
    color_scheme: ColorScheme | int,
    use_measured: bool = True,
) -> Palette:
    """Return the palette to dither to for a display.

    A measured palette for the panel and scheme when one exists (and ``use_measured``),
    otherwise the scheme's idealised palette. Either way, the scheme's pure inks are named,
    so colours that request them come out solid.

    Raises:
        ValueError: ``color_scheme`` is not a known wire value.
    """
    scheme = ColorScheme.from_value(int(color_scheme))
    if use_measured and panel_ic_type is not None:
        measured = _measured_palette(panel_ic_type, scheme)
        if measured is not None:
            return measured
    return _ideal_palette(scheme)
