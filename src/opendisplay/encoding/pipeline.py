"""Stateless image preparation pipeline for OpenDisplay devices.

Contains pure functions that transform a PIL Image into device-ready bytes
(rotate, fit, dither, encode, compress) without any BLE dependency.
"""

from __future__ import annotations

import logging

from epaper_dithering import ColorScheme, DitherMode, dither_image
from PIL import Image

from ..display_palettes import PANELS_4GRAY, get_palette_for_display
from ..models.capabilities import DeviceCapabilities
from ..models.config import GlobalConfig
from ..models.enums import FitMode, Rotation
from .bitplanes import encode_bitplanes
from .compression import compress_image_data
from .images import encode_image, fit_image

_LOGGER = logging.getLogger(__name__)


def _rotate_source_image(image: Image.Image, rotate: Rotation) -> Image.Image:
    """Rotate source image by enum value before fitting.

    Rotation uses clockwise semantics for API ergonomics.
    """
    if not isinstance(rotate, Rotation):
        raise TypeError(f"rotate must be Rotation, got {type(rotate).__name__}")

    if rotate == Rotation.ROTATE_0:
        return image
    if rotate == Rotation.ROTATE_90:
        return image.transpose(Image.Transpose.ROTATE_270)
    if rotate == Rotation.ROTATE_180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if rotate == Rotation.ROTATE_270:
        return image.transpose(Image.Transpose.ROTATE_90)
    return image


def prepare_image(
    image: Image.Image,
    config: GlobalConfig | None = None,
    capabilities: DeviceCapabilities | None = None,
    use_measured_palettes: bool = True,
    panel_ic_type: int | None = None,
    dither_mode: DitherMode = DitherMode.BURKES,
    compress: bool = True,
    tone_compression: float | str = "auto",
    fit: FitMode = FitMode.CONTAIN,
    rotate: Rotation = Rotation.ROTATE_0,
) -> tuple[bytes, bytes | None, Image.Image]:
    """Prepare image for display without requiring a BLE connection.

    Standalone function that processes an image (rotate, fit, dither, encode)
    using only the device configuration. No device instance or BLE connection
    needed.

    Args:
        image: PIL Image to prepare
        config: Device configuration (GlobalConfig from interrogation)
        capabilities: Optional explicit capabilities. If None, extracted
            from config.
        use_measured_palettes: Use measured color palettes when available
        panel_ic_type: Panel IC type for palette lookup. If None, extracted
            from config.
        dither_mode: Dithering algorithm to use (default: BURKES)
        compress: Whether to compress the image data (default: True)
        tone_compression: Dynamic range compression ("auto", or 0.0-1.0)
        fit: How to map the image to display dimensions (default: CONTAIN)
        rotate: Source image rotation enum (0/90/180/270)

    Returns:
        Tuple of (uncompressed_data, compressed_data or None, processed_image)

    Raises:
        RuntimeError: If config has no display information
    """
    if capabilities is None:
        if config is None or not config.displays:
            raise RuntimeError("Config has no display information")
        display = config.displays[0]
        capabilities = DeviceCapabilities(
            width=display.pixel_width,
            height=display.pixel_height,
            color_scheme=ColorScheme.from_value(display.color_scheme),
            rotation=display.rotation,
        )

    if panel_ic_type is None and config is not None and config.displays:
        panel_ic_type = config.displays[0].panel_ic_type

    target_size = (capabilities.width, capabilities.height)
    image = _rotate_source_image(image, rotate)

    if image.size != target_size:
        _LOGGER.info(
            "Fitting image %dx%d -> %dx%d (mode: %s)",
            image.width,
            image.height,
            capabilities.width,
            capabilities.height,
            fit.name,
        )
        image = fit_image(image, target_size, fit)

    color_scheme = capabilities.color_scheme
    if color_scheme == ColorScheme.GRAYSCALE_4 and panel_ic_type is not None and panel_ic_type not in PANELS_4GRAY:
        _LOGGER.warning(
            "Panel IC 0x%04x is not a known 4-gray panel. GRAYSCALE_4 encoding may not display correctly.",
            panel_ic_type,
        )

    palette = get_palette_for_display(panel_ic_type, color_scheme, use_measured_palettes)
    dithered = dither_image(image, palette, mode=dither_mode, tone_compression=tone_compression)

    # Encode to device format
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        plane1, plane2 = encode_bitplanes(dithered, color_scheme)
        image_data = plane1 + plane2
    else:
        image_data = encode_image(dithered, color_scheme)

    # Optionally compress
    compressed_data = None
    if compress:
        compressed_data = compress_image_data(image_data, level=6)

    return image_data, compressed_data, dithered
