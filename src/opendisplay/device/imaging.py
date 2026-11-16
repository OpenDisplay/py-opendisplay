"""Image preparation for upload: rotation, fitting and dithering. No I/O."""

from __future__ import annotations

import logging

import numpy as np
from epaper_dithering import DbsParams, DitherMode, DitherOptions, dither_image
from PIL import Image

from ..display_palettes import PANELS_4GRAY, get_bwry_codes, get_gray4_codes, get_palette_for_display
from ..encoding import (
    FIRMWARE_ZLIB_WINDOW_BITS,
    compress_image_data,
    encode_2bpp,
    encode_bitplanes,
    encode_gray4_bitplanes,
    encode_image,
    fit_image,
    normalize_source_image,
)
from ..encoding.multipass import SCHEME_BWRY16, encode_multipass, mode_palette
from ..exceptions import ImageEncodingError
from ..models.capabilities import DeviceCapabilities
from ..models.config import DisplayConfig, GlobalConfig
from ..models.enums import ColorScheme, FitMode, Rotation
from ..models.panel_drive import COLOR_SCHEME_INHERIT, DisplayMode

_LOGGER = logging.getLogger(__name__)


_INDEX_TO_ROTATION: dict[int, Rotation] = {
    0: Rotation.ROTATE_0,
    1: Rotation.ROTATE_90,
    2: Rotation.ROTATE_180,
    3: Rotation.ROTATE_270,
}


def _capabilities_rotation(raw: int) -> Rotation:
    """Convert a DeviceCapabilities.rotation int to a Rotation enum.

    Tolerates both degree values (0/90/180/270) stored by current code and
    raw firmware indices (0/1/2/3) that may exist in older serialized data.
    """
    try:
        return Rotation(raw)
    except ValueError:
        return _INDEX_TO_ROTATION.get(raw, Rotation.ROTATE_0)


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


def _capabilities_from_config(config: GlobalConfig) -> DeviceCapabilities:
    if not config.displays:
        raise RuntimeError("Config has no display information")

    display = config.displays[0]
    rotation = display.rotation_enum
    color_scheme = ColorScheme.from_value(display.color_scheme)
    panel = next((p for p in config.display_panels if p.instance_number == display.instance_number), None)
    return DeviceCapabilities(
        width=display.pixel_width,
        height=display.pixel_height,
        color_scheme=color_scheme,
        rotation=rotation.value if isinstance(rotation, Rotation) else 0,
        bwry_codes=panel.code_map if panel is not None else None,
    )


def _bwry_codes(capabilities: DeviceCapabilities, panel_ic_type: int | None) -> tuple[int, int, int, int]:
    """The device's code_map when it has one (od-v3.js ``wireMapFromCodeMap``), else the built-in table.

    Raises:
        ImageEncodingError: The code_map is not a permutation of 0..3.
    """
    codes = capabilities.bwry_codes
    if codes is None:
        return get_bwry_codes(panel_ic_type)
    if sorted(codes) != [0, 1, 2, 3]:
        raise ImageEncodingError(f"The device's code_map {codes} is not a permutation of 0..3; refusing to guess")
    return codes


# pixels-per-byte for each direct-write color scheme. GRAYSCALE_4 is omitted:
# firmware row-pads its upload, so a non-aligned width is safe there.
_DIRECT_WRITE_PIXELS_PER_BYTE: dict[ColorScheme, int] = {
    ColorScheme.MONO: 8,
    ColorScheme.BWR: 8,
    ColorScheme.BWY: 8,
    ColorScheme.BWRY: 4,
    ColorScheme.BWGBRY: 2,
    ColorScheme.BWGBRY_SPLIT: 2,
    ColorScheme.SEVEN_COLOR: 2,
    ColorScheme.GRAYSCALE_16: 2,
}


def _warn_firmware_upload_limitations(color_scheme: ColorScheme, width: int) -> None:
    """Warn about known device-firmware upload bugs the library can't fix on-device.

    - BWR/BWY direct write drops the red/yellow plane on current firmware (C1).
    - Widths not aligned to the scheme's byte boundary are truncated because the
      firmware sizes the upload from the raw pixel count (C2). GRAYSCALE_4 is
      exempt because firmware row-pads it.
    """
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        _LOGGER.warning(
            "Color scheme %s (BWR/BWY direct write) is not reliably supported by current "
            "firmware: it stores only one plane, so the red/yellow layer is discarded and "
            "the black/white layer renders over stale color RAM. Output may be wrong until "
            "device firmware gains BWR/BWY parity.",
            color_scheme.name,
        )

    ppb = _DIRECT_WRITE_PIXELS_PER_BYTE.get(color_scheme)
    if ppb is not None and width % ppb != 0:
        _LOGGER.warning(
            "Panel width %d is not a multiple of %d for color scheme %s; current firmware "
            "sizes the upload from the raw pixel count and will truncate the last rows on "
            "the device. A byte-aligned width avoids this.",
            width,
            ppb,
            color_scheme.name,
        )


def _resolve_dbs(dbs: DbsParams | bool | None, display: DisplayConfig | None) -> DbsParams | None:
    """Turn the caller's ``dbs`` argument into DBS parameters for ``dither_image``.

    ``True`` fills in the panel's real pixel density from its physical size, so
    the eye model matches the hardware; explicit ``DbsParams`` are used as-is.
    """
    if dbs is None or dbs is False:
        return None
    if dbs is True:
        ppi = display.ppi if display is not None else None
        return DbsParams(ppi=ppi) if ppi is not None else DbsParams()
    return dbs


def _fit_for_display(
    image: Image.Image, capabilities: DeviceCapabilities, rotate: Rotation, fit: FitMode
) -> Image.Image:
    """Normalise, rotate and fit a source image to the display, as every upload does."""
    target_size = (capabilities.width, capabilities.height)
    base = _capabilities_rotation(capabilities.rotation)
    effective = Rotation((base.value + rotate.value) % 360)
    image = _rotate_source_image(normalize_source_image(image), effective)
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
    return image


def prepare_image(
    image: Image.Image,
    config: GlobalConfig | None = None,
    capabilities: DeviceCapabilities | None = None,
    use_measured_palettes: bool = True,
    panel_ic_type: int | None = None,
    dither_mode: DitherMode = DitherMode.BURKES,
    compress: bool = True,
    serpentine: bool = True,
    exposure: float = 1.0,
    saturation: float = 1.0,
    shadows: float = 0.0,
    highlights: float = 0.0,
    tone: float = 0.0,
    gamut: float = 0.0,
    fit: FitMode = FitMode.CONTAIN,
    rotate: Rotation = Rotation.ROTATE_0,
    dbs: DbsParams | bool | None = None,
) -> tuple[bytes, bytes | None, Image.Image]:
    """Prepare image for display without requiring a BLE connection.

    Standalone function that processes an image (rotate, fit, dither, encode)
    using only the device configuration. No device instance or BLE connection
    needed. Transparency in the source is resolved to white, and an embedded
    colour profile other than sRGB is converted to sRGB first.

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
        serpentine: Alternate scan direction each row to reduce artifacts (default: True)
        exposure: Exposure multiplier, >1.0 brightens (default: 1.0)
        saturation: Saturation multiplier, >1.0 boosts (default: 1.0)
        shadows: Shadow lift in [0.0, 1.0] (default: 0.0)
        highlights: Highlight rolloff in [0.0, 1.0] (default: 0.0)
        tone: Black-point lift, 0.0–1.0 (default: 0.0)
        gamut: Gamut compression, 0.0–1.0 (default: 0.0)
        fit: How to map the image to display dimensions (default: CONTAIN)
        rotate: Source image rotation enum (0/90/180/270)
        dbs: Direct Binary Search refinement after dithering — ``None``/``False``
            = off (default), ``True`` = on with the panel's pixel density taken
            from its physical size, or explicit ``DbsParams``. Slow: seconds on
            a full panel.

    Returns:
        Tuple of (uncompressed_data, compressed_data or None, processed_image)

    Raises:
        RuntimeError: If config has no display information
        ValueError: If ``dither_mode`` is unknown, or ``tone``/``gamut`` is outside 0.0–1.0
        TypeError: If ``tone`` or ``gamut`` is not a number (e.g. the 6.x ``"auto"``)
    """
    if capabilities is None:
        if config is None:
            raise RuntimeError("Config has no display information")
        capabilities = _capabilities_from_config(config)

    if panel_ic_type is None and config is not None and config.displays:
        panel_ic_type = config.displays[0].panel_ic_type

    image = _fit_for_display(image, capabilities, rotate, fit)

    color_scheme = capabilities.color_scheme
    if color_scheme == ColorScheme.GRAYSCALE_4 and panel_ic_type is not None and panel_ic_type not in PANELS_4GRAY:
        _LOGGER.warning(
            "Panel IC 0x%04x is not a known 4-gray panel. GRAYSCALE_4 encoding may not display correctly.",
            panel_ic_type,
        )

    _warn_firmware_upload_limitations(color_scheme, capabilities.width)

    palette = get_palette_for_display(panel_ic_type, color_scheme, use_measured_palettes)
    display_cfg = config.displays[0] if config is not None and config.displays else None
    dithered = dither_image(
        image,
        palette,
        DitherOptions(
            mode=dither_mode,
            serpentine=serpentine,
            exposure=exposure,
            saturation=saturation,
            shadows=shadows,
            highlights=highlights,
            tone=tone,
            gamut=gamut,
            dbs=_resolve_dbs(dbs, display_cfg),
        ),
    )

    # Encode to device format
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        plane1, plane2 = encode_bitplanes(dithered, color_scheme)
        image_data = plane1 + plane2
    elif color_scheme == ColorScheme.GRAYSCALE_4:
        # Two pre-split 1-bit planes concatenated; firmware streams the halves to PLANE_0/PLANE_1.
        image_data = b"".join(encode_gray4_bitplanes(dithered, get_gray4_codes(panel_ic_type)))
    elif color_scheme == ColorScheme.BWRY:
        # Some YR panels (0x001D) use a native 4-color code order with
        # yellow/red swapped relative to the dither palette; apply the per-panel
        # code table so the firmware's raw-nibble direct write shows the right color.
        image_data = encode_2bpp(dithered, codes=_bwry_codes(capabilities, panel_ic_type))
    else:
        image_data = encode_image(dithered, color_scheme)

    # Optionally compress
    compressed_data = None
    if compress:
        # Current firmware compiles uzlib with a 9-bit window and hard-rejects any
        # zlib header advertising more, so always compress with a 9-bit window
        # regardless of transmission_modes: a 9-bit stream decodes fine on any firmware whose
        # window is >= 9 (the firmware check is <=).
        compressed_data = compress_image_data(image_data, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS)

    return image_data, compressed_data, dithered


def preview_image(
    processed: Image.Image,
    config: GlobalConfig | None = None,
    capabilities: DeviceCapabilities | None = None,
    rotate: Rotation = Rotation.ROTATE_0,
) -> Image.Image:
    """Turn ``prepare_image``'s processed image into an RGB preview facing like the source.

    The processed image is in the panel's native orientation, rotated by the display's
    rotation plus ``rotate``; this undoes that rotation. Its colours are each palette
    entry's appearance, so the preview shows what the panel shows. Pass the same
    ``config``/``capabilities`` and ``rotate`` given to ``prepare_image``.

    Raises:
        RuntimeError: If config has no display information
    """
    if capabilities is None:
        if config is None:
            raise RuntimeError("Config has no display information")
        capabilities = _capabilities_from_config(config)
    effective = (_capabilities_rotation(capabilities.rotation).value + rotate.value) % 360
    return _rotate_source_image(processed.convert("RGB"), Rotation((360 - effective) % 360))


def prepare_multipass(  # pylint: disable=too-many-arguments
    image: Image.Image,
    config: GlobalConfig,
    mode: DisplayMode,
    *,
    use_measured_palettes: bool = True,
    dither_mode: DitherMode = DitherMode.BURKES,
    serpentine: bool = True,
    exposure: float = 1.0,
    saturation: float = 1.0,
    fit: FitMode = FitMode.CONTAIN,
    rotate: Rotation = Rotation.ROTATE_0,
) -> tuple[bytes, Image.Image]:
    """Encode an image for a named drive mode: rotate -> fit -> dither -> one frame per pass. No I/O.

    A multi-pass mode is dithered against its calibration palette (0xB0) and encoded pass by pass
    (``encoding.multipass``). A single-pass mode is its scheme's normal frame (``prepare_image``).

    Returns:
        The uncompressed frames, concatenated, and the dithered preview.

    Raises:
        ImageEncodingError: A multi-pass mode without a 0xB0 calibration packet or with a scheme
            other than BWRY16, or a single-pass mode whose scheme is not the display's.
    """
    if mode.passes > 1 and mode.color_scheme != SCHEME_BWRY16:
        raise ImageEncodingError(
            f"Drive mode '{mode.name}' has {mode.passes} passes with scheme {mode.color_scheme}; "
            f"only scheme {SCHEME_BWRY16} (BWRY16) multi-pass frames can be encoded"
        )
    display_scheme = config.displays[0].color_scheme if config.displays else None
    if mode.passes <= 1 and mode.color_scheme not in (COLOR_SCHEME_INHERIT, display_scheme):
        raise ImageEncodingError(
            f"Drive mode '{mode.name}' renders scheme {mode.color_scheme}, not the display's scheme "
            f"{display_scheme}; such a single-pass mode cannot be encoded yet"
        )
    if mode.passes <= 1:
        data, _, preview = prepare_image(
            image,
            config=config,
            use_measured_palettes=use_measured_palettes,
            dither_mode=dither_mode,
            compress=False,
            serpentine=serpentine,
            exposure=exposure,
            saturation=saturation,
            fit=fit,
            rotate=rotate,
        )
        return data, preview
    calibration = next(
        (
            c
            for c in config.display_calibrations
            if c.mode_index == mode.mode_index and c.instance_number == mode.instance_number
        ),
        None,
    )
    if calibration is None:
        raise ImageEncodingError(
            f"Drive mode '{mode.name}' has {mode.passes} passes but the config has no 0xB0 calibration "
            "packet for it, so its colours are unknown"
        )
    image = _fit_for_display(image, _capabilities_from_config(config), rotate, fit)
    dithered = dither_image(
        image,
        mode_palette(calibration),
        DitherOptions(mode=dither_mode, serpentine=serpentine, exposure=exposure, saturation=saturation),
    )
    return encode_multipass(np.asarray(dithered), calibration, mode.passes), dithered
