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
PANELS_4GRAY: frozenset[int] = frozenset({
    0x0008,  # EP295_128x296_4GRAY
    0x0015,  # EP75_800x480_4GRAY
    0x0018,  # EP29_128x296_4GRAY
    0x0028,  # EP426_800x480_4GRAY
    0x002F,  # EP29Z_128x296_4GRAY
    0x0031,  # EP213Z_122x250_4GRAY
    0x003C,  # EP75_800x480_4GRAY_GEN2
})

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
    scheme = (
        color_scheme
        if isinstance(color_scheme, ColorScheme)
        else ColorScheme.from_value(color_scheme)
    )

    if use_measured and panel_ic_type is not None:
        key = (panel_ic_type, scheme)
        measured = DISPLAY_PALETTE_MAP.get(key)
        if measured is not None:
            return measured

    return scheme
