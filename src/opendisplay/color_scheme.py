"""Firmware color-scheme helpers beyond epaper-dithering's enum.

``COLOR_SCHEME_BWGBRY_SPLIT`` (8) uses the same Spectra 6 dither palette as
``BWGBRY`` (4), but packs left-half then right-half planes for dual-CS panels
(e.g. reTerminal E1004). The Rust dither core does not know value 8, so we
resolve it to ``ColorScheme.BWGBRY`` for dithering and keep the wire value
separately for encoding.
"""

from __future__ import annotations

from epaper_dithering import ColorScheme

# Mirrors Firmware COLOR_SCHEME_BWGBRY_SPLIT / config.yaml color_scheme 8.
COLOR_SCHEME_BWGBRY_SPLIT: int = 8


def resolve_firmware_color_scheme(value: int) -> tuple[ColorScheme, int]:
    """Map a firmware color_scheme byte to (dither palette scheme, wire value).

    Raises:
        ValueError: If the value is not a known firmware color scheme.
    """
    if value == COLOR_SCHEME_BWGBRY_SPLIT:
        return ColorScheme.BWGBRY, COLOR_SCHEME_BWGBRY_SPLIT
    return ColorScheme.from_value(value), value


def color_scheme_display_name(scheme: ColorScheme, wire_value: int | None = None) -> str:
    """Human-readable name, including BWGBRY_SPLIT when the wire value is 8."""
    if wire_value == COLOR_SCHEME_BWGBRY_SPLIT:
        return "BWGBRY_SPLIT"
    return scheme.name
