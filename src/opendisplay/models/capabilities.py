"""Device capabilities model."""

from __future__ import annotations

from dataclasses import dataclass

from epaper_dithering import ColorScheme


@dataclass
class DeviceCapabilities:
    """Minimal device information needed for image upload."""

    width: int
    height: int
    color_scheme: ColorScheme
    rotation: int = 0
    # Raw firmware color_scheme byte. None means color_scheme.value.
    # Set to 8 (BWGBRY_SPLIT) for dual-CS Spectra panels that need half-plane packing.
    wire_color_scheme: int | None = None

    @property
    def wire_scheme(self) -> int:
        """Firmware color_scheme value used on the wire / for packing."""
        return self.color_scheme.value if self.wire_color_scheme is None else self.wire_color_scheme
