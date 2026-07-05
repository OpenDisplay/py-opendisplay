"""Automatic measured palette selection and panel capability data for e-paper displays."""

from epaper_dithering import (
    BWRY_3_97,
    MONO_4_26,
    SOLUM_BWR,
    SPECTRA_7_3_6COLOR,
    ColorPalette,
    ColorScheme,
)

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
# native codes match that order. Panels 0x001D/0x001E use u8Colors_4clr, where
# native code 2=red and 3=yellow, so yellow/red must be swapped on the wire
# (the firmware direct-write path streams the nibble raw).
_BWRY_CODES_DEFAULT: tuple[int, int, int, int] = (0, 1, 2, 3)  # u8Colors_4clr_v2
_BWRY_CODES_SWAPPED: tuple[int, int, int, int] = (0, 1, 3, 2)  # u8Colors_4clr
_BWRY_CODES_BY_PANEL: dict[int, tuple[int, int, int, int]] = {
    0x001D: _BWRY_CODES_SWAPPED,  # EP29YR_128x296
    0x001E: _BWRY_CODES_SWAPPED,  # EP29YR_168x384
}


def get_bwry_codes(panel_ic_type: int | None) -> tuple[int, int, int, int]:
    """Return the BWRY palette-index->stored-nibble table for a panel."""
    if panel_ic_type is None:
        return _BWRY_CODES_DEFAULT
    return _BWRY_CODES_BY_PANEL.get(panel_ic_type, _BWRY_CODES_DEFAULT)


# Map: (panel_ic_type, color_scheme) -> measured ColorPalette
# panel_ic_type identifies the e-paper panel model
# color_scheme identifies the color mode (MONO, BWR, BWGBRY, etc.)
DISPLAY_PALETTE_MAP: dict[tuple[int, ColorScheme], ColorPalette] = {
    # Spectra 7.3" 6-color (ep73_spectra_800x480)
    (35, ColorScheme.BWGBRY): SPECTRA_7_3_6COLOR,
    # 4.26" Monochrome (ep426_800x480)
    (39, ColorScheme.MONO): MONO_4_26,
    # Solum 2.6" BWR (ep26r_152x296)
    (33, ColorScheme.BWR): SOLUM_BWR,
    # 3.97" BWRY (ep397yr_800x480)
    (55, ColorScheme.BWRY): BWRY_3_97,
    # Add more as color calibration becomes available:
    # (?, ColorScheme.BWRY): BWRY_4_2,  # 4.2" BWRY
    # (?, ColorScheme.BWR): HANSHOW_BWR,
    # (?, ColorScheme.BWY): HANSHOW_BWY,
}


def get_palette_for_display(
    panel_ic_type: int | None,
    color_scheme: ColorScheme | int,
    use_measured: bool = True,
) -> ColorScheme | ColorPalette:
    """Get best available palette for display.

    Returns a measured ColorPalette if one exists for the given panel and color
    scheme combination. Otherwise falls back to the theoretical ColorScheme.

    Args:
        panel_ic_type: E-paper panel model ID (from DisplayConfig), or None if not available
        color_scheme: Color scheme enum or integer value
        use_measured: If True, use measured palette when available; if False, always use ColorScheme

    Returns:
        ColorPalette if measured data exists and use_measured=True, otherwise ColorScheme enum
    """
    scheme = color_scheme if isinstance(color_scheme, ColorScheme) else ColorScheme.from_value(color_scheme)

    if use_measured and panel_ic_type is not None:
        key = (panel_ic_type, scheme)
        measured = DISPLAY_PALETTE_MAP.get(key)
        if measured is not None:
            return measured

    return scheme
