"""Enumerations for OpenDisplay device configuration."""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class RefreshMode(IntEnum):
    """Display refresh modes.

    Only FULL and FAST are supported by the firmware.
    """

    FULL = 0
    FAST = 1


class ICType(IntEnum):
    """Microcontroller IC types."""

    NRF52840 = 1
    ESP32_S3 = 2
    ESP32_C3 = 3
    ESP32_C6 = 4
    NRF52811 = 5


class BoardManufacturer(IntEnum):
    """Board manufacturer identifiers."""

    DIY = 0
    SEEED = 1
    WAVESHARE = 2
    SOLUM = 3
    OPENDISPLAY = 4


class DIYBoardType(IntEnum):
    """DIY board types."""

    CUSTOM = 0


class SeeedBoardType(IntEnum):
    """Seeed board types."""

    EE04 = 0
    EN04 = 1
    ESP32_S3 = 2
    ESP32_C6 = 3
    ESP32_C3 = 4
    NRF52840 = 5
    EE05 = 6
    EN05 = 7
    RETERMINAL_E1003 = 8


class WaveshareBoardType(IntEnum):
    """Waveshare board types."""

    ESP32_S3_PHOTOPAINTER = 0


class SolumBoardType(IntEnum):
    """Solum board types."""

    M3 = 0


class OpenDisplayBoardType(IntEnum):
    """OpenDisplay board types."""

    DEFAULT = 0


class TouchIcType(IntEnum):
    """Touch controller IC types."""

    NONE = 0
    GT911 = 1


class CapacityEstimator(IntEnum):
    """Battery chemistry estimator types."""

    LI_ION = 1
    LIFEPO4 = 2
    SUPERCAP = 3
    LITHIUM_PRIMARY = 4


class LedType(IntEnum):
    """LED configuration types."""

    RGB = 0
    SINGLE = 1
    RY = 2
    FOUR_SEPARATE = 3


class SensorType(IntEnum):
    """Sensor types."""

    TEMPERATURE = 1
    HUMIDITY = 2
    AXP2101_PMIC = 3
    SHT40 = 4


class WifiEncryption(IntEnum):
    """WiFi encryption types."""

    NONE = 0
    WEP = 1
    WPA = 2
    WPA2 = 3
    WPA3 = 4


MANUFACTURER_NAMES: Final[dict[BoardManufacturer, str]] = {
    BoardManufacturer.DIY: "DIY",
    BoardManufacturer.SEEED: "Seeed Studio",
    BoardManufacturer.WAVESHARE: "Waveshare",
    BoardManufacturer.SOLUM: "Solum",
    BoardManufacturer.OPENDISPLAY: "OpenDisplay",
}

_BOARD_TYPE_NAMES_DIY: Final[dict[DIYBoardType, str]] = {
    DIYBoardType.CUSTOM: "Custom",
}

_BOARD_TYPE_NAMES_SEEED: Final[dict[SeeedBoardType, str]] = {
    SeeedBoardType.EE04: "EE04",
    SeeedBoardType.EN04: "EN04",
    SeeedBoardType.ESP32_S3: "ESP32-S3",
    SeeedBoardType.ESP32_C6: "ESP32-C6",
    SeeedBoardType.ESP32_C3: "ESP32-C3",
    SeeedBoardType.NRF52840: "NRF52840",
    SeeedBoardType.EE05: "EE05",
    SeeedBoardType.EN05: "EN05",
    SeeedBoardType.RETERMINAL_E1003: "reTerminal E1003",
}

_BOARD_TYPE_NAMES_WAVESHARE: Final[dict[WaveshareBoardType, str]] = {
    WaveshareBoardType.ESP32_S3_PHOTOPAINTER: "PhotoPainter",
}

_BOARD_TYPE_NAMES_SOLUM: Final[dict[SolumBoardType, str]] = {
    SolumBoardType.M3: "M3",
}

_BOARD_TYPE_NAMES_OPENDISPLAY: Final[dict[OpenDisplayBoardType, str]] = {
    OpenDisplayBoardType.DEFAULT: "Default",
}


def get_manufacturer_name(manufacturer_id: BoardManufacturer | int) -> str | None:
    """Get canonical manufacturer name, if known."""
    try:
        return MANUFACTURER_NAMES[BoardManufacturer(manufacturer_id)]
    except (ValueError, KeyError):
        return None


def get_board_type_name(manufacturer_id: BoardManufacturer | int, board_type: int) -> str | None:
    """Get human-readable board type name for a manufacturer, if known."""
    try:
        manufacturer = BoardManufacturer(manufacturer_id)
    except ValueError:
        return None

    try:
        if manufacturer == BoardManufacturer.DIY:
            return _BOARD_TYPE_NAMES_DIY[DIYBoardType(board_type)]
        if manufacturer == BoardManufacturer.SEEED:
            return _BOARD_TYPE_NAMES_SEEED[SeeedBoardType(board_type)]
        if manufacturer == BoardManufacturer.WAVESHARE:
            return _BOARD_TYPE_NAMES_WAVESHARE[WaveshareBoardType(board_type)]
        if manufacturer == BoardManufacturer.SOLUM:
            return _BOARD_TYPE_NAMES_SOLUM[SolumBoardType(board_type)]
        if manufacturer == BoardManufacturer.OPENDISPLAY:
            return _BOARD_TYPE_NAMES_OPENDISPLAY[OpenDisplayBoardType(board_type)]
    except (ValueError, KeyError):
        return None

    return None


class PowerMode(IntEnum):
    """Power source types."""

    BATTERY = 1
    USB = 2
    SOLAR = 3


class BusType(IntEnum):
    """Data bus types."""

    I2C = 1
    SPI = 2


class Rotation(IntEnum):
    """Display rotation angles in degrees."""

    ROTATE_0 = 0
    ROTATE_90 = 90
    ROTATE_180 = 180
    ROTATE_270 = 270


class FitMode(IntEnum):
    """Image fit strategies for mapping source images to display dimensions.

    Controls how aspect ratio mismatches are handled when the source image
    doesn't match the display's pixel dimensions.
    """

    STRETCH = 0  # Distort to fill exact dimensions (ignores aspect ratio)
    CONTAIN = 1  # Scale to fit within bounds, pad empty space with white
    COVER = 2  # Scale to cover bounds, crop overflow (no distortion)
    CROP = 3  # No scaling, center-crop at native resolution (pad if smaller)
