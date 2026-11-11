"""Enumerations for OpenDisplay device configuration."""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class ColorScheme(IntEnum):
    """A display's colour scheme, as the firmware's ``color_scheme`` wire value.

    Values match ``enum ColorScheme`` in ``opendisplay-protocol``. The palette each scheme
    dithers to lives in ``opendisplay.display_palettes``.
    """

    MONO = 0
    BWR = 1
    BWY = 2
    BWRY = 3
    BWGBRY = 4
    GRAYSCALE_4 = 5
    GRAYSCALE_16 = 6
    SEVEN_COLOR = 7
    BWGBRY_SPLIT = 8

    @classmethod
    def from_value(cls, value: int) -> ColorScheme:
        """Return the scheme for a wire value, raising ``ValueError`` for an unknown one."""
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"Invalid color scheme value: {value}") from None


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
    RETERMINAL_E1004 = 14


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
    NPM1300 = 6  # Nordic nPM1300 PMIC (default I2C 0x6B)


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


class DisplayTechnology(IntEnum):
    """Display technology (DisplayConfig.display_technology)."""

    UNDEFINED = 0
    E_PAPER = 1
    LCD = 2
    LED_MATRIX = 3


# Panel IC id -> human-readable panel description.
#
# Generated from the canonical protocol schema published by the Config Builder
# (Web repo, httpdocs/firmware/toolbox/config.yaml, ble_proto minor_version 4);
# the firmware's own mapping lives in mapEpd(). Ids absent here are still valid
# on newer firmware -- callers should fall back to the raw hex id.
PANEL_IC_NAMES: Final[dict[int, str]] = {
    0: "undefined / unknown panel",
    1: "WFT0420CZ15",
    2: "DEPG0420BN / GDEY042T81",
    3: 'Waveshare 2.13"',
    4: 'GDEY0213B74 (Inky pHAT 2.13" B/W newer)',
    5: "128x296 panel",
    6: 'Waveshare newer 2.9" 1-bit 128x296',
    7: 'harvested Solum 2.9" BW ESLs',
    8: "4-gray variant",
    9: "GDEY0266T90",
    10: "GDEW0102T4",
    11: "GDEY027T91",
    12: "tricolor panel",
    13: "GDEM0122T61",
    14: '1.54" B/W/R',
    15: "400x300 tricolor",
    16: "GDEQ042Z21",
    17: "GDEY037T03",
    18: 'CROWPANEL 3.7"',
    19: "InkyPHAT 2.13 B/W",
    20: "GDEY075T7 (older version)",
    21: "GDEW075T7 (older version) in 4 gray mode",
    22: "GDEY075T7 older panel, darker grays needed",
    23: "Pimoroni Badger2040",
    24: "Pimoroni Badger2040 4-gray",
    25: "Inky pHAT 2.13 B/W/R",
    26: 'Waveshare 2.0" B/W',
    27: "DEPG01540BN",
    28: "GDEY0266F51",
    29: "GDEY029F51",
    30: "GDEY029F51H",
    31: "DEPG0583BN",
    32: 'Waveshare 2.9" 128x296 B/W V2',
    33: 'Solum 2.6" B/W/R',
    34: "GEDY073D46 (slower, EOL 7-color)",
    35: "Spectra 6/7-color 800x480",
    36: "640x384 panel",
    37: "4-bits per pixel panel",
    38: "Waveshare 800x480 3-color",
    39: 'Waveshare 4.26" B/W 800x480',
    40: '4.26" 2-bit grayscale mode',
    41: 'Adafruit 2.9" Tricolor FeatherWing',
    42: "EInk ED040TC1 SPI UC81xx",
    43: 'Spectra 8.1" 1024x576 6-color',
    44: "ED070EC1",
    45: "UC8151 3-color",
    46: 'SSD1680 (CrowPanel 2.9")',
    47: "SSD1680 4-gray",
    48: 'SSD1680 CrowPanel 2.13"',
    49: 'CrowPanel 2.13" 4-gray mode',
    50: 'CrowPanel 1.54"',
    51: 'CrowPanel 5.79"',
    52: "GDEY0213F52",
    53: "GDEM037F51",
    54: "GDEM035F51",
    55: "GDEM0397F81",
    56: "GDEM0154F51H",
    57: "GDEY0266F52H",
    58: "GDEM042F52",
    59: "GEDY075-D2 (Waveshare/Xiao V2 panels)",
    60: "GDEY075T7-D2 (newer version) in 4 gray mode",
    61: 'Waveshare 2.15" 4 color',
    62: "GDEM1085T51",
    63: "GDEQ031T10 LilyGo T-Deck Pro",
    64: "EP75YR_800x480",
    67: 'Waveshare 1.54"',
    68: "DEPG0420BN / GDEY042T81",
    69: "GDEM0397T81P",
    70: "GDEM0397T81P 4-gray",
    71: 'xteink X3 3.68" B/W',
    72: 'xteink X3 3.68" 4-gray',
    73: 'LilyGo T3S3 2.13"',
    74: 'GDEP040E01 Spectra 6 4" 400x600',
    75: "Badger2350 B/W",
    76: "Badger2350 4-gray",
    1000: 'UC8176 4.2" B/W',
    1001: 'SSD1619 4.2" B/W/R',
    1002: 'UC8176 4.2" B/W/R',
    1003: 'SSD1619 4.2" B/W',
    1004: 'JD79668 4.2" B/W/R/Y',
    1005: 'UC8179 7.5" B/W',
    1006: 'UC8179 7.5" B/W/R',
    1007: 'UC8159 7.5" Low Power B/W',
    1008: 'UC8159 7.5" Low Power B/W/R',
    1009: 'SSD1677 7.5" HD B/W',
    1010: 'SSD1677 7.5" HD B/W/R',
    1011: 'JD79665 7.5" B/W/R/Y',
    1012: 'JD79665 5.83" B/W/R/Y',
    1013: 'UC8151 2.9" 168x384 B/W',
    1014: 'UC8151 2.9" 168x384 B/W/R',
    1015: 'SSD1619 2.9" 168x384 B/W',
    1016: 'SSD1619 2.9" 168x384 B/W/R',
    1017: 'SSD1619 1.6" 200x200 B/W',
    1018: 'SSD1619 1.6" 200x200 B/W/R',
    1019: 'SSD1619 2.2" 240x320 B/W',
    1020: 'SSD1619 2.2" 240x320 B/W/R',
    1021: 'SSD1619 2.6" 296x152 B/W',
    1022: 'SSD1619 2.6" 296x152 B/W/R',
    1023: 'UC8151 2.7" 200x300 B/W',
    1024: 'UC8151 2.7" 200x300 B/W/R',
    1025: 'UC variant 4.3" 152x522 B/W',
    1026: 'UC variant 4.3" 152x522 B/W/R',
    1027: 'SSD1619 1.3" 144x200 B/W',
    1028: 'SSD1619 1.3" 144x200 B/W/R',
    1029: 'SSD1619 M3 Lite 2.2" 250x128 B/W',
    1030: 'SSD1619 M3 Lite 2.2" 250x128 B/W/R',
    3000: 'E Ink ED103TC2 + IT8951 (10.3", 1872x1404, 1bpp)',
    3001: "Same panel as 3000",
    3002: "Soldered Inkplate 5 V2 (ED050WROW, 1280x720)",
    3003: "Soldered Inkplate 10 (ED097TC2, 1200x825)",
}


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
    SeeedBoardType.RETERMINAL_E1004: "reTerminal E1004",
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
