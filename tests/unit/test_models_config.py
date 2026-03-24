"""Test config model computed properties."""

import pytest

from opendisplay.models.config import DisplayConfig, ManufacturerData
from opendisplay.models.enums import (
    BoardManufacturer,
    DIYBoardType,
    SeeedBoardType,
    WaveshareBoardType,
)


def _display_config(active_width_mm: int, active_height_mm: int) -> DisplayConfig:
    return DisplayConfig(
        instance_number=0,
        display_technology=1,
        panel_ic_type=0,
        pixel_width=296,
        pixel_height=128,
        active_width_mm=active_width_mm,
        active_height_mm=active_height_mm,
        tag_type=0,
        rotation=0,
        reset_pin=0xFF,
        busy_pin=0xFF,
        dc_pin=0xFF,
        cs_pin=0xFF,
        data_pin=0,
        partial_update_support=1,
        color_scheme=0,
        transmission_modes=0,
        clk_pin=0,
        reserved_pins=b"\x00" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )


class TestDisplayConfigScreenDiagonal:
    """Test DisplayConfig.screen_diagonal_inches."""

    def test_returns_diagonal_inches_when_dimensions_set(self):
        display = _display_config(active_width_mm=120, active_height_mm=90)

        # 3-4-5 triangle: hypot(120, 90) = 150mm
        assert display.screen_diagonal_inches == pytest.approx(150 / 25.4)

    def test_returns_none_when_width_unset(self):
        display = _display_config(active_width_mm=0, active_height_mm=90)

        assert display.screen_diagonal_inches is None

    def test_returns_none_when_height_unset(self):
        display = _display_config(active_width_mm=120, active_height_mm=0)

        assert display.screen_diagonal_inches is None


class TestDisplayConfigColorScheme:
    """Test DisplayConfig.color_scheme_enum."""

    def test_returns_enum_for_known_color_scheme(self):
        display = _display_config(active_width_mm=120, active_height_mm=90)
        display.color_scheme = 0

        assert display.color_scheme_enum.name == "MONO"

    def test_returns_raw_int_for_unknown_color_scheme(self):
        display = _display_config(active_width_mm=120, active_height_mm=90)
        display.color_scheme = 99

        assert display.color_scheme_enum == 99


class TestManufacturerDataBoardTyping:
    """Test ManufacturerData board typing and names."""

    def _mfg(self, manufacturer_id: int, board_type: int) -> ManufacturerData:
        return ManufacturerData(
            manufacturer_id=manufacturer_id,
            board_type=board_type,
            board_revision=1,
            reserved=b"\x00" * 18,
        )

    def test_seeed_board_type_enum_and_name(self):
        mfg = self._mfg(BoardManufacturer.SEEED, 1)
        assert mfg.board_type_enum == SeeedBoardType.EN04
        assert mfg.board_type_name == "EN04"

    def test_diy_board_type_enum_and_name(self):
        mfg = self._mfg(BoardManufacturer.DIY, 0)
        assert mfg.board_type_enum == DIYBoardType.CUSTOM
        assert mfg.board_type_name == "Custom"

    def test_waveshare_board_type_enum_and_name(self):
        mfg = self._mfg(BoardManufacturer.WAVESHARE, 0)
        assert mfg.board_type_enum == WaveshareBoardType.ESP32_S3_PHOTOPAINTER
        assert mfg.board_type_name == "PhotoPainter"

    def test_unknown_board_type_falls_back_to_int(self):
        mfg = self._mfg(BoardManufacturer.SEEED, 99)
        assert mfg.board_type_enum == 99
        assert mfg.board_type_name is None


class TestDisplayConfigTransmissionModes:
    """Test DisplayConfig.supports_zip from transmission_modes bitfield."""

    def _display(self, transmission_modes: int) -> DisplayConfig:
        d = _display_config(active_width_mm=120, active_height_mm=90)
        d.transmission_modes = transmission_modes
        return d

    def test_supports_zip_true_when_bit_set(self):
        assert self._display(transmission_modes=0x02).supports_zip is True

    def test_supports_zip_false_when_no_bits_set(self):
        assert self._display(transmission_modes=0x00).supports_zip is False

    def test_supports_zip_false_when_only_raw_bit_set(self):
        assert self._display(transmission_modes=0x01).supports_zip is False

    def test_supports_zip_true_with_multiple_bits_set(self):
        assert self._display(transmission_modes=0x03).supports_zip is True
