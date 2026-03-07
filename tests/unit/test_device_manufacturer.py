"""Test typed board manufacturer access on OpenDisplayDevice."""

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.models.config import (
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.models.enums import BoardManufacturer


def _system_packet() -> SystemConfig:
    return SystemConfig(
        ic_type=1,
        communication_modes=1,
        device_flags=0,
        pwr_pin=0xFF,
        reserved=b"\x00" * 17,
    )


def _manufacturer_packet(manufacturer_id: int) -> ManufacturerData:
    return ManufacturerData(
        manufacturer_id=manufacturer_id,
        board_type=0,
        board_revision=1,
        reserved=b"\x00" * 18,
    )


def _power_packet() -> PowerOption:
    return PowerOption(
        power_mode=1,
        battery_capacity_mah=(1000).to_bytes(3, "little"),
        sleep_timeout_ms=1000,
        tx_power=0,
        sleep_flags=0,
        battery_sense_pin=0xFF,
        battery_sense_enable_pin=0xFF,
        battery_sense_flags=0,
        capacity_estimator=1,
        voltage_scaling_factor=100,
        deep_sleep_current_ua=0,
        deep_sleep_time_seconds=0,
        reserved=b"\x00" * 10,
    )


def _display_packet() -> DisplayConfig:
    return DisplayConfig(
        instance_number=0,
        display_technology=0,
        panel_ic_type=0,
        pixel_width=296,
        pixel_height=128,
        active_width_mm=66,
        active_height_mm=29,
        tag_type=0,
        rotation=0,
        reset_pin=0xFF,
        busy_pin=0xFF,
        dc_pin=0xFF,
        cs_pin=0xFF,
        data_pin=0,
        partial_update_support=0,
        color_scheme=0,
        transmission_modes=0,
        clk_pin=0,
        reserved_pins=b"\x00" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )


def _config_with_manufacturer(manufacturer_id: int) -> GlobalConfig:
    return GlobalConfig(
        system=_system_packet(),
        manufacturer=_manufacturer_packet(manufacturer_id),
        power=_power_packet(),
        displays=[_display_packet()],
    )


class TestBoardManufacturerAccess:
    """Test OpenDisplayDevice.get_board_manufacturer()."""

    def test_returns_typed_enum_for_known_manufacturer(self):
        config = _config_with_manufacturer(1)
        device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config)

        assert device.get_board_manufacturer() == BoardManufacturer.SEEED

    def test_returns_raw_int_for_unknown_manufacturer(self):
        config = _config_with_manufacturer(99)
        device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config)

        result = device.get_board_manufacturer()
        assert isinstance(result, int)
        assert result == 99

    def test_raises_when_config_missing(self):
        device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")

        with pytest.raises(RuntimeError, match="config unknown"):
            device.get_board_manufacturer()

    def test_get_board_type_returns_raw_id(self):
        config = _config_with_manufacturer(1)
        config.manufacturer.board_type = 6
        device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config)

        assert device.get_board_type() == 6

    def test_get_board_type_name_returns_known_name(self):
        config = _config_with_manufacturer(1)
        config.manufacturer.board_type = 1
        device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config)

        assert device.get_board_type_name() == "EN04"

    def test_get_board_type_name_returns_none_for_unknown(self):
        config = _config_with_manufacturer(1)
        config.manufacturer.board_type = 99
        device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config)

        assert device.get_board_type_name() is None
