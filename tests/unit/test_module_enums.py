"""Test model enums and conversions."""

from opendisplay.models.enums import (
    BoardManufacturer,
    BusType,
    DIYBoardType,
    ICType,
    PowerMode,
    RefreshMode,
    Rotation,
    SeeedBoardType,
    WaveshareBoardType,
    get_board_type_name,
    get_manufacturer_name,
)
from opendisplay.models.firmware import firmware_release_repo


class TestRefreshMode:
    """Test RefreshMode enum."""

    def test_refresh_mode_values(self):
        """Test all refresh modes have correct values."""
        assert RefreshMode.FULL == 0
        assert RefreshMode.FAST == 1

    def test_refresh_mode_names(self):
        """Test refresh mode names."""
        assert RefreshMode.FULL.name == "FULL"
        assert RefreshMode.FAST.name == "FAST"


class TestICType:
    """Test IC (microcontroller) type enum."""

    def test_ic_type_values(self):
        """Test IC type values."""
        assert ICType.NRF52840 == 1
        assert ICType.ESP32_S3 == 2
        assert ICType.ESP32_C3 == 3
        assert ICType.ESP32_C6 == 4
        assert ICType.NRF52811 == 5
        assert ICType.EFR32BG22 == 6

    def test_ic_type_names(self):
        """Test IC type names."""
        assert ICType.NRF52840.name == "NRF52840"
        assert ICType.ESP32_S3.name == "ESP32_S3"
        assert ICType.EFR32BG22.name == "EFR32BG22"


class TestPowerMode:
    """Test PowerMode enum."""

    def test_power_mode_values(self):
        """Test power mode values."""
        assert PowerMode.BATTERY == 1
        assert PowerMode.USB == 2
        assert PowerMode.SOLAR == 3

    def test_power_mode_names(self):
        """Test power mode names."""
        assert PowerMode.BATTERY.name == "BATTERY"
        assert PowerMode.USB.name == "USB"
        assert PowerMode.SOLAR.name == "SOLAR"


class TestBoardManufacturer:
    """Test BoardManufacturer enum."""

    def test_board_manufacturer_values(self):
        """Test board manufacturer values from webconfig manufacturer_data enum."""
        assert BoardManufacturer.DIY == 0
        assert BoardManufacturer.SEEED == 1
        assert BoardManufacturer.WAVESHARE == 2

    def test_board_manufacturer_names(self):
        """Test board manufacturer names."""
        assert BoardManufacturer.DIY.name == "DIY"
        assert BoardManufacturer.SEEED.name == "SEEED"
        assert BoardManufacturer.WAVESHARE.name == "WAVESHARE"


class TestBoardTypeEnums:
    """Test manufacturer-specific board type enums."""

    def test_diy_board_type_values(self):
        """Test DIY board type values."""
        assert DIYBoardType.CUSTOM == 0

    def test_seeed_board_type_values(self):
        """Test Seeed board type values."""
        assert SeeedBoardType.EE04 == 0
        assert SeeedBoardType.EN04 == 1
        assert SeeedBoardType.EE05 == 6
        assert SeeedBoardType.EN05 == 7

    def test_waveshare_board_type_values(self):
        """Test Waveshare board type values."""
        assert WaveshareBoardType.ESP32_S3_PHOTOPAINTER == 0

    def test_board_type_name_helpers(self):
        """Test board type and manufacturer name helpers."""
        assert get_manufacturer_name(BoardManufacturer.DIY) == "DIY"
        assert get_manufacturer_name(99) is None

        assert get_board_type_name(BoardManufacturer.DIY, 0) == "Custom"
        assert get_board_type_name(BoardManufacturer.SEEED, 1) == "EN04"
        assert get_board_type_name(BoardManufacturer.WAVESHARE, 0) == "PhotoPainter"
        assert get_board_type_name(BoardManufacturer.SEEED, 99) is None
        assert get_board_type_name(99, 0) is None


class TestBusType:
    """Test BusType enum."""

    def test_bus_type_values(self):
        """Test bus type values."""
        assert BusType.I2C == 1
        assert BusType.SPI == 2

    def test_bus_type_names(self):
        """Test bus type names."""
        assert BusType.I2C.name == "I2C"
        assert BusType.SPI.name == "SPI"


class TestRotation:
    """Test Rotation enum."""

    def test_rotation_values(self):
        """Test rotation degree values."""
        assert Rotation.ROTATE_0 == 0
        assert Rotation.ROTATE_90 == 90
        assert Rotation.ROTATE_180 == 180
        assert Rotation.ROTATE_270 == 270

    def test_rotation_names(self):
        """Test rotation names."""
        assert Rotation.ROTATE_0.name == "ROTATE_0"
        assert Rotation.ROTATE_90.name == "ROTATE_90"
        assert Rotation.ROTATE_180.name == "ROTATE_180"
        assert Rotation.ROTATE_270.name == "ROTATE_270"

    def test_all_rotations_exist(self):
        """Test all 4 rotations are defined."""
        rotations = list(Rotation)
        assert len(rotations) == 4


class TestFirmwareReleaseRepo:
    """Test firmware_release_repo() mapping."""

    def test_nrf52840_maps_to_main_firmware(self):
        assert firmware_release_repo(ICType.NRF52840) == "OpenDisplay/Firmware"

    def test_esp32_variants_map_to_main_firmware(self):
        assert firmware_release_repo(ICType.ESP32_S3) == "OpenDisplay/Firmware"
        assert firmware_release_repo(ICType.ESP32_C3) == "OpenDisplay/Firmware"
        assert firmware_release_repo(ICType.ESP32_C6) == "OpenDisplay/Firmware"

    def test_nrf52811_maps_to_nrf_repo(self):
        assert firmware_release_repo(ICType.NRF52811) == "OpenDisplay/Firmware_NRF"

    def test_efr32bg22_maps_to_silabs_repo(self):
        assert firmware_release_repo(ICType.EFR32BG22) == "OpenDisplay/Firmware_Silabs"

    def test_unknown_ic_type_returns_none(self):
        assert firmware_release_repo(99) is None
        assert firmware_release_repo(0) is None
