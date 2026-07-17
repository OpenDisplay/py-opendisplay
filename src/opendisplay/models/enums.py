"""Enumerations for OpenDisplay device configuration."""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class RefreshMode(IntEnum):
    """Display refresh modes.

    FULL is the normal full-screen update.
    FAST is a panel-specific reduced-flash refresh.
    PARTIAL requests the panel's true partial-update mode when supported.
    """

    FULL = 0
    FAST = 1
    PARTIAL = 2


class PartialUpdateSupport(IntEnum):
    """display.partial_update_support values (config tool enum).

    FULL_FRAME panels (e.g. EP426 / Seeed EN05) support the 0x76 partial
    protocol but require the stream to cover the whole panel: firmware
    white-fills the controller RAM at partial start, so content outside the
    region would be erased (OpenDisplay/Firmware#80).
    """

    NONE = 0  # only full updates supported
    PARTIAL = 1  # partial region updates supported
    FULL_FRAME = 2  # partial supported, full-frame stream required


class ICType(IntEnum):
    """Microcontroller IC types."""

    NRF52840 = 1
    ESP32_S3 = 2
    ESP32_C3 = 3
    ESP32_C6 = 4
    NRF52811 = 5
    EFR32BG22 = 6  # EFR32BG22C222F352GM40 — Silabs-based boards (e.g. Solum M3)


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
    """Seeed board types.

    Mirrors the ``board_type`` ``conditional_enum`` for ``manufacturer_id: 1``
    in the Web config tool's ``config.yaml`` (source of truth).
    """

    EE04 = 0
    EN04 = 1
    ESP32_S3 = 2
    ESP32_C6 = 3
    ESP32_C3 = 4
    NRF52840 = 5
    EE05 = 6
    EN05 = 7
    RETERMINAL_E1003 = 8
    RETERMINAL_STICKY = 9
    OPENDISPLAY_426_MONO_KIT = 10
    OPENDISPLAY_73_COLOR_KIT = 11
    RETERMINAL_E1001 = 12
    RETERMINAL_E1002 = 13


class WaveshareBoardType(IntEnum):
    """Waveshare board types."""

    ESP32_S3_PHOTOPAINTER = 0


class SolumBoardType(IntEnum):
    """Solum board types.

    Mirrors the ``board_type`` ``conditional_enum`` for ``manufacturer_id: 3``
    in the Web config tool's ``config.yaml`` (source of truth).
    """

    M3_NRF_LITE = 0
    M3_NRF = 1
    M3_SILABS = 2
    M3_SILABS_CORE = 3
    M3_SILABS_PRO = 4
    M3_SILABS_LITE = 5
    M3_SILABS_PEGHOOK = 6

    # Backwards-compat alias for the original single Solum entry (board_type 0).
    M3 = 0


class OpenDisplayBoardType(IntEnum):
    """OpenDisplay board types."""

    OD01 = 0

    # Backwards-compat alias for the original name of board_type 0.
    DEFAULT = 0


class TouchIcType(IntEnum):
    """Touch controller IC types."""

    NONE = 0
    GT911 = 1


class NfcIcType(IntEnum):
    """NFC controller IC types (config packet 0x2a)."""

    AUTO = 0  # Currently resolves to the TNB132M flow in firmware
    TNB132M = 1


class NfcRecordType(IntEnum):
    """NDEF record types for the NFC write endpoint (command 0x0083)."""

    TEXT = 0
    URI = 1
    WELL_KNOWN_RAW = 2
    MIME = 3
    RAW_NDEF = 4


class FlashIcType(IntEnum):
    """External flash IC types (config packet 0x2b)."""

    AUTO = 0  # Generic SPI flash deep-sleep command flow


class NfcFieldDetectMode(IntEnum):
    """NFC field-detect sampling modes (config packet 0x2a)."""

    DISABLED = 0
    GPIO_LEVEL = 1
    IRQ_LATCHED = 2


class ActiveLevel(IntEnum):
    """Active polarity for a GPIO line (shared by NFC/flash power and field-detect)."""

    ACTIVE_LOW = 0
    ACTIVE_HIGH = 1


class CapacityEstimator(IntEnum):
    """Battery chemistry estimator types."""

    LI_ION = 1
    LIFEPO4 = 2
    SUPERCAP = 3
    LITHIUM_PRIMARY = 4
    SEEED_LI_ION = 5


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
    BQ27220 = 5


class WifiEncryption(IntEnum):
    """WiFi encryption types."""

    NONE = 0
    WEP = 1
    WPA = 2
    WPA2 = 3
    WPA3 = 4


class BinaryInputType(IntEnum):
    """Binary input acquisition methods (BinaryInputs.input_type)."""

    DIGITAL = 1  # one GPIO per button, digitalRead + edge interrupt
    SWITCH = 2  # reserved for the host-side switch feature
    ADC_LADDER = 3  # buttons share one ADC pin, distinguished by voltage (polled)


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
    SeeedBoardType.RETERMINAL_STICKY: "reTerminal Sticky",
    SeeedBoardType.OPENDISPLAY_426_MONO_KIT: 'OpenDisplay 4.26" Mono Kit',
    SeeedBoardType.OPENDISPLAY_73_COLOR_KIT: 'OpenDisplay 7.3" Color Kit',
    SeeedBoardType.RETERMINAL_E1001: "reTerminal E1001",
    SeeedBoardType.RETERMINAL_E1002: "reTerminal E1002",
}

_BOARD_TYPE_NAMES_WAVESHARE: Final[dict[WaveshareBoardType, str]] = {
    WaveshareBoardType.ESP32_S3_PHOTOPAINTER: "PhotoPainter",
}

_BOARD_TYPE_NAMES_SOLUM: Final[dict[SolumBoardType, str]] = {
    SolumBoardType.M3_NRF_LITE: "M3 NRF Lite",
    SolumBoardType.M3_NRF: "M3 NRF",
    SolumBoardType.M3_SILABS: "M3 Silabs",
    SolumBoardType.M3_SILABS_CORE: "M3 Silabs Core",
    SolumBoardType.M3_SILABS_PRO: "M3 Silabs Pro",
    SolumBoardType.M3_SILABS_LITE: "M3 Silabs Lite",
    SolumBoardType.M3_SILABS_PEGHOOK: "M3 Silabs Peghook",
}

_BOARD_TYPE_NAMES_OPENDISPLAY: Final[dict[OpenDisplayBoardType, str]] = {
    OpenDisplayBoardType.OD01: "OD01",
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
