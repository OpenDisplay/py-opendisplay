"""Device capabilities model."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import ColorScheme


@dataclass
class DeviceCapabilities:
    """Minimal device information needed for image upload."""

    width: int
    height: int
    color_scheme: ColorScheme
    rotation: int = 0
    # BWRY palette-index -> panel code, from display 0's 0xB1 code_map; None = the built-in table.
    # Firmware 3.0 stores code_map and does not apply it, so the host sends panel codes.
    bwry_codes: tuple[int, int, int, int] | None = None
