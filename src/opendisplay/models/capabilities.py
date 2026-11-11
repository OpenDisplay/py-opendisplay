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
