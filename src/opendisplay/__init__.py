"""OpenDisplay BLE Protocol Package.

Pure Python package for communicating with OpenDisplay BLE e-paper tags.
"""

from epaper_dithering import ColorScheme, DitherMode

from .battery import voltage_to_percent
from .device import OpenDisplayDevice, prepare_image
from .discovery import discover_devices, discover_devices_with_adv
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
    OTAError,
    OTANotSupportedError,
    ProtocolError,
)
from .models.advertisement import (
    AdvertisementData,
    AdvertisementTracker,
    ButtonChangeEvent,
    ButtonEventData,
    TouchChangeEvent,
    TouchEventData,
    TouchTracker,
    decode_button_event,
    parse_advertisement,
)
from .models.buzzer_activate import BuzzerActivateConfig, BuzzerPattern, BuzzerStep
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
from .models.firmware import firmware_ota_asset, firmware_release_repo
from .models.led_flash import LedFlashConfig, LedFlashStep
from .ota import find_nrf_dfu_device, perform_nrf_dfu, perform_silabs_ota
from .protocol import MANUFACTURER_ID, SERVICE_UUID

__version__ = "0.1.0"

__all__ = [
    # Main API
    "OpenDisplayDevice",
    "discover_devices",
    "discover_devices_with_adv",
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
    "OTAError",
    "OTANotSupportedError",
    "find_nrf_dfu_device",
    "perform_nrf_dfu",
    "perform_silabs_ota",
    # Models - Config
    "GlobalConfig",
    "SystemConfig",
    "ManufacturerData",
    "PowerOption",
    "DisplayConfig",
    "LedConfig",
    "BuzzerActivateConfig",
    "BuzzerPattern",
    "BuzzerStep",
    "LedFlashConfig",
    "LedFlashStep",
    "firmware_ota_asset",
    "firmware_release_repo",
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
    "TouchEventData",
    "TouchChangeEvent",
    "TouchTracker",
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
