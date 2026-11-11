"""Palettes and panel capability data for e-paper displays.

epaper-dithering 7 ships no palettes, so py-opendisplay owns them, as ``epaper-palette`` files
in ``opendisplay/palettes/``: an idealised palette per colour scheme, plus measured palettes
for panels that have been measured. Entry order is the palette-index order the encoders expect.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

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


# Idealised palette per colour scheme.
IDEAL_PALETTE_FILES: dict[ColorScheme, str] = {
    ColorScheme.MONO: "ideal_mono.json",
    ColorScheme.BWR: "ideal_bwr.json",
    ColorScheme.BWY: "ideal_bwy.json",
    ColorScheme.BWRY: "ideal_bwry.json",
    ColorScheme.BWGBRY: "ideal_bwgbry.json",
    ColorScheme.BWGBRY_SPLIT: "ideal_bwgbry.json",  # same inks; only the plane packing differs
    ColorScheme.SEVEN_COLOR: "ideal_seven_color.json",
    ColorScheme.GRAYSCALE_4: "ideal_grayscale_4.json",
    ColorScheme.GRAYSCALE_16: "ideal_grayscale_16.json",
}

# Measured palettes by (panel_ic_type, colour scheme).
MEASURED_PALETTE_FILES: dict[tuple[int, ColorScheme], str] = {
    # Spectra 6 7.3" (ep73_spectra_800x480).
    (35, ColorScheme.BWGBRY): "spectra6_7in3_v2.json",
    # Spectra 6 13.3" dual-controller (T133A01, Seeed reTerminal E1004). Keyed on
    # BWGBRY_SPLIT because firmware requires scheme 8 for split panels; same inks as the
    # 7.3", so it borrows that measurement until the T133A01 is measured.
    (66, ColorScheme.BWGBRY_SPLIT): "spectra6_7in3_v2.json",
    # 3.97" BWRY (ep397yr_800x480).
    (55, ColorScheme.BWRY): "ep397yr_bwry.json",
}


@cache
def load_palette_file(name: str) -> Palette:
    """Load one of the palette files shipped in ``opendisplay/palettes/``."""
    return Palette.from_json(files("opendisplay").joinpath("palettes", name).read_text(encoding="utf-8"))


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
        measured = MEASURED_PALETTE_FILES.get((panel_ic_type, scheme))
        if measured is not None:
            return load_palette_file(measured)
    return load_palette_file(IDEAL_PALETTE_FILES[scheme])
