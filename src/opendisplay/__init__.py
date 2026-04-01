"""OpenDisplay BLE Protocol Package.

Pure Python package for communicating with OpenDisplay BLE e-paper tags.
"""

from epaper_dithering import ColorScheme, DitherMode

from .battery import voltage_to_percent
from .device import OpenDisplayDevice, prepare_image
from .discovery import discover_devices
from .exceptions import (
    AuthenticationError,
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    ConfigParseError,
    ImageEncodingError,
    InvalidResponseError,
    OpenDisplayError,
    ProtocolError,
)
from .models.advertisement import (
    AdvertisementData,
    AdvertisementTracker,
    ButtonChangeEvent,
    ButtonEventData,
    decode_button_event,
    parse_advertisement,
)
from .models.capabilities import DeviceCapabilities
from .models.config import (
    BinaryInputs,
    DataBus,
    DisplayConfig,
    GlobalConfig,
    LedConfig,
    ManufacturerData,
    PassiveBuzzer,
    PowerOption,
    SecurityConfig,
    SensorData,
    SystemConfig,
    TouchController,
    WifiConfig,
)
from .models.enums import (
    BoardManufacturer,
    BusType,
    DIYBoardType,
    FitMode,
    ICType,
    OpenDisplayBoardType,
    PowerMode,
    RefreshMode,
    Rotation,
    SeeedBoardType,
    SolumBoardType,
    TouchIcType,
    WaveshareBoardType,
    get_board_type_name,
    get_manufacturer_name,
)
from .models.led_flash import LedFlashConfig, LedFlashStep
from .protocol import MANUFACTURER_ID, SERVICE_UUID

__version__ = "0.1.0"

__all__ = [
    # Main API
    "OpenDisplayDevice",
    "discover_devices",
    "prepare_image",
    # Exceptions
    "OpenDisplayError",
    "AuthenticationError",
    "AuthenticationFailedError",
    "AuthenticationRequiredError",
    "BLEConnectionError",
    "BLETimeoutError",
    "ProtocolError",
    "ConfigParseError",
    "InvalidResponseError",
    "ImageEncodingError",
    # Models - Config
    "GlobalConfig",
    "SystemConfig",
    "ManufacturerData",
    "PowerOption",
    "DisplayConfig",
    "LedConfig",
    "LedFlashConfig",
    "LedFlashStep",
    "SensorData",
    "DataBus",
    "BinaryInputs",
    "PassiveBuzzer",
    "SecurityConfig",
    "TouchController",
    "WifiConfig",
    # Models - Other
    "DeviceCapabilities",
    "AdvertisementData",
    "AdvertisementTracker",
    "ButtonEventData",
    "ButtonChangeEvent",
    # Enums
    "ColorScheme",
    "DitherMode",
    "FitMode",
    "BoardManufacturer",
    "DIYBoardType",
    "OpenDisplayBoardType",
    "RefreshMode",
    "ICType",
    "PowerMode",
    "BusType",
    "Rotation",
    "SeeedBoardType",
    "SolumBoardType",
    "TouchIcType",
    "WaveshareBoardType",
    "get_board_type_name",
    "get_manufacturer_name",
    # Utilities
    "parse_advertisement",
    "decode_button_event",
    "voltage_to_percent",
    # Constants
    "SERVICE_UUID",
    "MANUFACTURER_ID",
]
