"""Tests for TouchController and PassiveBuzzer packet types (protocol v1.2)."""

from __future__ import annotations

import struct

import pytest

from opendisplay.models.config import (
    FlashConfig,
    ManufacturerData,
    NfcConfig,
    PassiveBuzzer,
    SensorData,
    SystemConfig,
    TouchController,
)
from opendisplay.models.enums import (
    ActiveLevel,
    BoardManufacturer,
    FlashIcType,
    NfcFieldDetectMode,
    NfcIcType,
    OpenDisplayBoardType,
    SeeedBoardType,
    SensorType,
    TouchIcType,
    get_board_type_name,
    get_manufacturer_name,
)
from opendisplay.protocol.config_parser import parse_tlv_config
from opendisplay.protocol.config_serializer import (
    serialize_flash_config,
    serialize_manufacturer_data,
    serialize_nfc_config,
    serialize_passive_buzzer,
    serialize_sensor_data,
    serialize_system_config,
    serialize_touch_controller,
)

# ---------------------------------------------------------------------------
# Helpers shared with test_required_packets.py pattern
# ---------------------------------------------------------------------------


def _packet(number: int, packet_type: int, payload: bytes) -> bytes:
    return bytes([number, packet_type]) + payload


def _system_payload() -> bytes:
    return struct.pack("<HBBB", 1, 0, 0, 0) + (b"\x00" * 17)


def _manufacturer_payload() -> bytes:
    return struct.pack("<HBB", 1, 0, 1) + (b"\x00" * 18)


def _power_payload() -> bytes:
    return (
        bytes([1])
        + (1000).to_bytes(3, byteorder="little")
        + struct.pack("<HbBBBBBHIH", 1000, 0, 0, 0xFF, 0xFF, 0, 1, 100, 0, 0)
        + (b"\x00" * 10)
    )


def _display_payload() -> bytes:
    return b"\x00" * 46


def _required_tlv() -> bytes:
    return (
        _packet(0, 0x01, _system_payload())
        + _packet(1, 0x02, _manufacturer_payload())
        + _packet(2, 0x04, _power_payload())
        + _packet(3, 0x20, _display_payload())
    )


# ---------------------------------------------------------------------------
# New enum values
# ---------------------------------------------------------------------------


class TestNewEnumValues:
    def test_opendisplay_manufacturer(self):
        assert BoardManufacturer.OPENDISPLAY == 4
        assert get_manufacturer_name(4) == "OpenDisplay"
        assert get_manufacturer_name(BoardManufacturer.OPENDISPLAY) == "OpenDisplay"

    def test_opendisplay_board_type(self):
        assert OpenDisplayBoardType.OD01 == 0
        assert OpenDisplayBoardType.DEFAULT == 0  # backwards-compat alias
        assert get_board_type_name(BoardManufacturer.OPENDISPLAY, 0) == "OD01"
        assert get_board_type_name(BoardManufacturer.OPENDISPLAY, 99) is None

    def test_seeed_reterminal_e1003(self):
        assert SeeedBoardType.RETERMINAL_E1003 == 8
        assert get_board_type_name(BoardManufacturer.SEEED, 8) == "reTerminal E1003"

    def test_sht40_sensor_type(self):
        assert SensorType.SHT40 == 4

    def test_touch_ic_type_values(self):
        assert TouchIcType.NONE == 0
        assert TouchIcType.GT911 == 1


# ---------------------------------------------------------------------------
# SystemConfig pwr_pin_2 / pwr_pin_3
# ---------------------------------------------------------------------------


class TestSystemConfigPwrPins:
    def _make_payload(self, pwr_pin_2: int = 0xFF, pwr_pin_3: int = 0xFE) -> bytes:
        return struct.pack("<HBBB", 2, 0x01, 0x03, 0x2B) + b"\x00" * 15 + bytes([pwr_pin_2, pwr_pin_3])

    def test_from_bytes_extracts_pwr_pin_2_and_3(self):
        payload = self._make_payload(pwr_pin_2=0x01, pwr_pin_3=0x02)
        cfg = SystemConfig.from_bytes(payload)
        assert cfg.pwr_pin_2 == 0x01
        assert cfg.pwr_pin_3 == 0x02

    def test_from_bytes_reserved_is_15_bytes(self):
        payload = self._make_payload()
        cfg = SystemConfig.from_bytes(payload)
        assert len(cfg.reserved) == 15

    def test_serialize_round_trip(self):
        cfg = SystemConfig(
            ic_type=2,
            communication_modes=0x01,
            device_flags=0x01,
            pwr_pin=0x2B,
            reserved=b"\x00" * 15,
            pwr_pin_2=0x0A,
            pwr_pin_3=0x0B,
        )
        data = serialize_system_config(cfg)
        assert len(data) == 22
        assert data[20] == 0x0A
        assert data[21] == 0x0B

    def test_default_pwr_pins_are_0xff(self):
        cfg = SystemConfig(
            ic_type=1,
            communication_modes=0,
            device_flags=0,
            pwr_pin=0xFF,
            reserved=b"\x00" * 15,
        )
        assert cfg.pwr_pin_2 == 0xFF
        assert cfg.pwr_pin_3 == 0xFF


# ---------------------------------------------------------------------------
# SensorData i2c_addr_7bit / msd_data_start_byte
# ---------------------------------------------------------------------------


class TestSensorDataNewFields:
    def _make_payload(self, i2c_addr: int = 0x5D, msd_start: int = 3) -> bytes:
        header = struct.pack("<BHB", 0, SensorType.SHT40, 0)
        return header + bytes([i2c_addr, msd_start]) + b"\x00" * 24

    def test_from_bytes_extracts_i2c_and_msd(self):
        payload = self._make_payload(i2c_addr=0x44, msd_start=2)
        cfg = SensorData.from_bytes(payload)
        assert cfg.i2c_addr_7bit == 0x44
        assert cfg.msd_data_start_byte == 2
        assert cfg.sensor_type == SensorType.SHT40

    def test_serialize_round_trip(self):
        cfg = SensorData(
            instance_number=0,
            sensor_type=SensorType.SHT40,
            bus_id=1,
            i2c_addr_7bit=0x44,
            msd_data_start_byte=2,
            reserved=b"\x00" * 24,
        )
        data = serialize_sensor_data(cfg)
        assert len(data) == 30
        assert data[4] == 0x44
        assert data[5] == 2


# ---------------------------------------------------------------------------
# TouchController
# ---------------------------------------------------------------------------


def _touch_payload(
    *,
    instance: int = 0,
    touch_ic: int = TouchIcType.GT911,
    bus_id: int = 0xFF,
    i2c_addr: int = 0x14,
    int_pin: int = 0x04,
    rst_pin: int = 0x05,
    display_instance: int = 0,
    flags: int = 0x01,
    poll_ms: int = 10,
    start_byte: int = 2,
) -> bytes:
    data = bytes([instance])
    data += touch_ic.to_bytes(2, "little")
    data += bytes([bus_id, i2c_addr, int_pin, rst_pin, display_instance, flags, poll_ms, start_byte])
    return data + b"\x00" * 21


class TestTouchController:
    def test_from_bytes_parses_all_fields(self):
        payload = _touch_payload()
        tc = TouchController.from_bytes(payload)

        assert tc.instance_number == 0
        assert tc.touch_ic_type == TouchIcType.GT911
        assert tc.bus_id == 0xFF
        assert tc.i2c_addr_7bit == 0x14
        assert tc.int_pin == 0x04
        assert tc.rst_pin == 0x05
        assert tc.display_instance == 0
        assert tc.flags == 0x01
        assert tc.poll_interval_ms == 10
        assert tc.touch_data_start_byte == 2
        assert len(tc.reserved) == 21

    def test_touch_ic_type_enum_property(self):
        tc = TouchController.from_bytes(_touch_payload(touch_ic=TouchIcType.GT911))
        assert tc.touch_ic_type_enum == TouchIcType.GT911

    def test_touch_ic_type_unknown_returns_int(self):
        tc = TouchController.from_bytes(_touch_payload(touch_ic=99))
        assert tc.touch_ic_type_enum == 99

    def test_flag_properties(self):
        tc_invert_x = TouchController.from_bytes(_touch_payload(flags=0x01))
        assert tc_invert_x.invert_x is True
        assert tc_invert_x.invert_y is False
        assert tc_invert_x.swap_xy is False

        tc_all = TouchController.from_bytes(_touch_payload(flags=0x07))
        assert tc_all.invert_x is True
        assert tc_all.invert_y is True
        assert tc_all.swap_xy is True

    def test_serialize_is_32_bytes(self):
        tc = TouchController.from_bytes(_touch_payload())
        data = serialize_touch_controller(tc)
        assert len(data) == 32

    def test_serialize_round_trip(self):
        original = _touch_payload(i2c_addr=0x5D, flags=0x06, poll_ms=25)
        tc = TouchController.from_bytes(original)
        serialized = serialize_touch_controller(tc)
        assert serialized == original

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid TouchController size"):
            TouchController.from_bytes(b"\x00" * 10)

    def test_parse_from_tlv(self):
        data = _required_tlv() + _packet(4, 0x28, _touch_payload())
        cfg = parse_tlv_config(data)

        assert len(cfg.touch_controllers) == 1
        tc = cfg.touch_controllers[0]
        assert tc.touch_ic_type == TouchIcType.GT911
        assert tc.invert_x is True

    def test_parse_multiple_touch_controllers(self):
        data = (
            _required_tlv()
            + _packet(4, 0x28, _touch_payload(instance=0, touch_ic=TouchIcType.GT911))
            + _packet(5, 0x28, _touch_payload(instance=1, touch_ic=TouchIcType.NONE))
        )
        cfg = parse_tlv_config(data)
        assert len(cfg.touch_controllers) == 2
        assert cfg.touch_controllers[0].touch_ic_type == TouchIcType.GT911
        assert cfg.touch_controllers[1].touch_ic_type == TouchIcType.NONE


# ---------------------------------------------------------------------------
# PassiveBuzzer
# ---------------------------------------------------------------------------


def _buzzer_payload(
    *,
    instance: int = 0,
    drive_pin: int = 0x05,
    enable_pin: int = 0xFF,
    flags: int = 0x01,
    duty: int = 50,
) -> bytes:
    return bytes([instance, drive_pin, enable_pin, flags, duty]) + b"\x00" * 27


class TestPassiveBuzzer:
    def test_from_bytes_parses_all_fields(self):
        payload = _buzzer_payload()
        bz = PassiveBuzzer.from_bytes(payload)

        assert bz.instance_number == 0
        assert bz.drive_pin == 0x05
        assert bz.enable_pin == 0xFF
        assert bz.flags == 0x01
        assert bz.duty_percent == 50
        assert len(bz.reserved) == 27

    def test_enable_active_high_property(self):
        bz_active_high = PassiveBuzzer.from_bytes(_buzzer_payload(flags=0x01))
        assert bz_active_high.enable_active_high is True

        bz_active_low = PassiveBuzzer.from_bytes(_buzzer_payload(flags=0x00))
        assert bz_active_low.enable_active_high is False

    def test_serialize_is_32_bytes(self):
        bz = PassiveBuzzer.from_bytes(_buzzer_payload())
        data = serialize_passive_buzzer(bz)
        assert len(data) == 32

    def test_serialize_round_trip(self):
        original = _buzzer_payload(drive_pin=0x0C, enable_pin=0x0D, flags=0x00, duty=75)
        bz = PassiveBuzzer.from_bytes(original)
        serialized = serialize_passive_buzzer(bz)
        assert serialized == original

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid PassiveBuzzer size"):
            PassiveBuzzer.from_bytes(b"\x00" * 4)

    def test_parse_from_tlv(self):
        data = _required_tlv() + _packet(4, 0x29, _buzzer_payload(drive_pin=0x0C))
        cfg = parse_tlv_config(data)

        assert len(cfg.buzzers) == 1
        assert cfg.buzzers[0].drive_pin == 0x0C

    def test_parse_touch_and_buzzer_together(self):
        data = _required_tlv() + _packet(4, 0x28, _touch_payload()) + _packet(5, 0x29, _buzzer_payload())
        cfg = parse_tlv_config(data)
        assert len(cfg.touch_controllers) == 1
        assert len(cfg.buzzers) == 1


# ---------------------------------------------------------------------------
# ManufacturerData simple_config_* fields
# ---------------------------------------------------------------------------


class TestManufacturerSimpleConfig:
    def _make_payload(
        self,
        *,
        mfr_id: int = 4,
        board_type: int = 0,
        board_rev: int = 1,
        driver: int = 5,
        display: int = 6,
        power: int = 7,
        configured_at: int = 0x1234567890,
    ) -> bytes:
        return (
            struct.pack("<HBBHHH", mfr_id, board_type, board_rev, driver, display, power)
            + configured_at.to_bytes(6, "little")
            + b"\x00" * 6
        )

    def test_from_bytes_extracts_simple_config(self):
        mfr = ManufacturerData.from_bytes(self._make_payload())
        assert mfr.simple_config_driver_index == 5
        assert mfr.simple_config_display_index == 6
        assert mfr.simple_config_power_index == 7
        assert mfr.simple_config_configured_at == 0x1234567890
        assert len(mfr.reserved) == 6

    def test_serialize_round_trip(self):
        original = self._make_payload(driver=1, display=2, power=3, configured_at=1700000000)
        mfr = ManufacturerData.from_bytes(original)
        assert serialize_manufacturer_data(mfr) == original

    def test_legacy_payload_defaults_to_zero(self):
        # An all-reserved (old-style) payload parses with simple_config fields = 0
        legacy = struct.pack("<HBB", 1, 0, 1) + (b"\x00" * 18)
        mfr = ManufacturerData.from_bytes(legacy)
        assert mfr.simple_config_driver_index == 0
        assert mfr.simple_config_configured_at == 0


# ---------------------------------------------------------------------------
# NfcConfig (0x2a)
# ---------------------------------------------------------------------------


def _nfc_payload(
    *,
    instance: int = 0,
    nfc_ic: int = NfcIcType.TNB132M,
    bus: int = 0,
    flags: int = 0x01,
    field_detect_pin: int = 0x32,
    field_detect_mode: int = NfcFieldDetectMode.IRQ_LATCHED,
    field_detect_active: int = ActiveLevel.ACTIVE_HIGH,
    debounce: int = 0,
    power_pin: int = 0x30,
    power_active: int = ActiveLevel.ACTIVE_HIGH,
    power_on: int = 0x28,
    power_off: int = 0,
    adv_byte: int = 1,
    adv_id: int = 7,
    rsv_pin_1: int = 0,
    rsv_pin_2: int = 0,
) -> bytes:
    return (
        bytes(
            [
                instance,
                nfc_ic,
                bus,
                flags,
                field_detect_pin,
                field_detect_mode,
                field_detect_active,
                debounce,
                power_pin,
                power_active,
                power_on,
                power_off,
                adv_byte,
                adv_id,
                rsv_pin_1,
                rsv_pin_2,
            ]
        )
        + b"\x00" * 16
    )


class TestNfcConfig:
    def test_from_bytes_parses_all_fields(self):
        nfc = NfcConfig.from_bytes(_nfc_payload())
        assert nfc.instance_number == 0
        assert nfc.nfc_ic_type == NfcIcType.TNB132M
        assert nfc.bus_instance == 0
        assert nfc.flags == 0x01
        assert nfc.field_detect_pin == 0x32
        assert nfc.field_detect_mode == NfcFieldDetectMode.IRQ_LATCHED
        assert nfc.field_detect_active == ActiveLevel.ACTIVE_HIGH
        assert nfc.power_pin == 0x30
        assert nfc.power_on_delay_ms == 0x28
        assert nfc.adv_button_byte_index == 1
        assert nfc.adv_button_button_id == 7
        assert len(nfc.reserved) == 16

    def test_enum_properties(self):
        nfc = NfcConfig.from_bytes(_nfc_payload())
        assert nfc.nfc_ic_type_enum == NfcIcType.TNB132M
        assert nfc.field_detect_mode_enum == NfcFieldDetectMode.IRQ_LATCHED
        assert nfc.field_detect_active_enum == ActiveLevel.ACTIVE_HIGH
        assert nfc.power_active_enum == ActiveLevel.ACTIVE_HIGH

    def test_unknown_enum_returns_int(self):
        nfc = NfcConfig.from_bytes(_nfc_payload(nfc_ic=99))
        assert nfc.nfc_ic_type_enum == 99

    def test_enabled_property(self):
        assert NfcConfig.from_bytes(_nfc_payload(flags=0x01)).enabled is True
        assert NfcConfig.from_bytes(_nfc_payload(flags=0x00)).enabled is False

    def test_serialize_is_32_bytes(self):
        assert len(serialize_nfc_config(NfcConfig.from_bytes(_nfc_payload()))) == 32

    def test_serialize_round_trip(self):
        original = _nfc_payload(power_pin=0x10, adv_id=3)
        assert serialize_nfc_config(NfcConfig.from_bytes(original)) == original

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid NfcConfig size"):
            NfcConfig.from_bytes(b"\x00" * 10)

    def test_parse_from_tlv(self):
        data = _required_tlv() + _packet(4, 0x2A, _nfc_payload())
        cfg = parse_tlv_config(data)
        assert len(cfg.nfc_configs) == 1
        assert cfg.nfc_configs[0].nfc_ic_type == NfcIcType.TNB132M
        assert cfg.nfc_configs[0].enabled is True


# ---------------------------------------------------------------------------
# FlashConfig (0x2b)
# ---------------------------------------------------------------------------


def _flash_payload(
    *,
    instance: int = 0,
    flash_ic: int = FlashIcType.AUTO,
    bus: int = 0,
    flags: int = 0x01,
    mosi: int = 0x21,
    sck: int = 0x22,
    cs: int = 0x23,
    power_pin: int = 0xFF,
    power_active: int = ActiveLevel.ACTIVE_HIGH,
    power_on: int = 0,
    power_off: int = 0,
    mode: int = 0,
) -> bytes:
    return (
        bytes([instance, flash_ic, bus, flags, mosi, sck, cs, power_pin, power_active, power_on, power_off, mode])
        + b"\x00" * 20
    )


class TestFlashConfig:
    def test_from_bytes_parses_all_fields(self):
        flash = FlashConfig.from_bytes(_flash_payload())
        assert flash.instance_number == 0
        assert flash.flash_ic_type == FlashIcType.AUTO
        assert flash.flags == 0x01
        assert flash.mosi_pin == 0x21
        assert flash.sck_pin == 0x22
        assert flash.cs_pin == 0x23
        assert flash.power_pin == 0xFF
        assert len(flash.reserved) == 20

    def test_enum_and_enabled_properties(self):
        flash = FlashConfig.from_bytes(_flash_payload())
        assert flash.flash_ic_type_enum == FlashIcType.AUTO
        assert flash.power_active_enum == ActiveLevel.ACTIVE_HIGH
        assert flash.enabled is True
        assert FlashConfig.from_bytes(_flash_payload(flags=0x00)).enabled is False

    def test_serialize_is_32_bytes(self):
        assert len(serialize_flash_config(FlashConfig.from_bytes(_flash_payload()))) == 32

    def test_serialize_round_trip(self):
        original = _flash_payload(mosi=0x10, sck=0x11, cs=0x12)
        assert serialize_flash_config(FlashConfig.from_bytes(original)) == original

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid FlashConfig size"):
            FlashConfig.from_bytes(b"\x00" * 4)

    def test_parse_from_tlv(self):
        data = _required_tlv() + _packet(4, 0x2B, _flash_payload(mosi=0x21))
        cfg = parse_tlv_config(data)
        assert len(cfg.flash_configs) == 1
        assert cfg.flash_configs[0].mosi_pin == 0x21


# ---------------------------------------------------------------------------
# Regression: 0x2a/0x2b no longer break the parse loop
# ---------------------------------------------------------------------------


class TestNfcFlashRegression:
    def test_nfc_at_tail_does_not_warn_or_break(self, caplog):
        # Reproduces the original "Unknown packet type 0x2a ... skipping" report:
        # nfc/flash trailing the stream must parse, not truncate the config.
        data = _required_tlv() + _packet(4, 0x2A, _nfc_payload()) + _packet(5, 0x2B, _flash_payload())
        with caplog.at_level("WARNING"):
            cfg = parse_tlv_config(data)

        assert "Unknown packet type" not in caplog.text
        assert len(cfg.nfc_configs) == 1
        assert len(cfg.flash_configs) == 1

    def test_packets_after_nfc_still_parsed(self):
        # A buzzer placed *after* nfc/flash would have been dropped before the fix.
        data = (
            _required_tlv()
            + _packet(4, 0x2A, _nfc_payload())
            + _packet(5, 0x2B, _flash_payload())
            + _packet(6, 0x29, _buzzer_payload(drive_pin=0x0C))
        )
        cfg = parse_tlv_config(data)
        assert len(cfg.nfc_configs) == 1
        assert len(cfg.flash_configs) == 1
        assert len(cfg.buzzers) == 1
        assert cfg.buzzers[0].drive_pin == 0x0C


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def _minimal_config(self, **kwargs):
        from opendisplay.models.config import (
            DisplayConfig,
            GlobalConfig,
            ManufacturerData,
            PowerOption,
            SystemConfig,
        )

        return GlobalConfig(
            system=SystemConfig(
                ic_type=2,
                communication_modes=0x01,
                device_flags=0x01,
                pwr_pin=0x2B,
                reserved=b"\x00" * 15,
                pwr_pin_2=0xAA,
                pwr_pin_3=0xBB,
            ),
            manufacturer=ManufacturerData(
                manufacturer_id=1,
                board_type=0,
                board_revision=1,
                reserved=b"\x00" * 18,
            ),
            power=PowerOption(
                power_mode=1,
                battery_capacity_mah=(2000).to_bytes(3, "little"),
                sleep_timeout_ms=1000,
                tx_power=8,
                sleep_flags=0,
                battery_sense_pin=0xFF,
                battery_sense_enable_pin=0xFF,
                battery_sense_flags=0,
                capacity_estimator=1,
                voltage_scaling_factor=100,
                deep_sleep_current_ua=0,
                deep_sleep_time_seconds=0,
                reserved=b"\x00" * 10,
            ),
            displays=[
                DisplayConfig(
                    instance_number=0,
                    display_technology=1,
                    panel_ic_type=33,
                    pixel_width=152,
                    pixel_height=296,
                    active_width_mm=0,
                    active_height_mm=0,
                    tag_type=0,
                    rotation=0,
                    reset_pin=0xFF,
                    busy_pin=0xFF,
                    dc_pin=0xFF,
                    cs_pin=0xFF,
                    data_pin=0,
                    partial_update_support=1,
                    color_scheme=0,
                    transmission_modes=0x0A,
                    clk_pin=0,
                    reserved_pins=b"\x00" * 7,
                    full_update_mC=0,
                    reserved=b"\x00" * 13,
                )
            ],
            version=1,
            **kwargs,
        )

    def test_touch_controller_round_trip(self):
        from opendisplay.models.config import TouchController
        from opendisplay.models.config_json import config_from_json, config_to_json

        tc = TouchController(
            instance_number=0,
            touch_ic_type=TouchIcType.GT911,
            bus_id=0xFF,
            i2c_addr_7bit=0x14,
            int_pin=0x04,
            rst_pin=0x05,
            display_instance=0,
            flags=0x03,
            poll_interval_ms=25,
            touch_data_start_byte=1,
            reserved=b"\x00" * 21,
        )
        cfg = self._minimal_config(touch_controllers=[tc])

        exported = config_to_json(cfg)
        reimported = config_from_json(exported)

        assert len(reimported.touch_controllers) == 1
        tc2 = reimported.touch_controllers[0]
        assert tc2.touch_ic_type == TouchIcType.GT911
        assert tc2.i2c_addr_7bit == 0x14
        assert tc2.flags == 0x03
        assert tc2.invert_x is True
        assert tc2.invert_y is True

    def test_passive_buzzer_round_trip(self):
        from opendisplay.models.config import PassiveBuzzer
        from opendisplay.models.config_json import config_from_json, config_to_json

        bz = PassiveBuzzer(
            instance_number=0,
            drive_pin=0x0C,
            enable_pin=0xFF,
            flags=0x01,
            duty_percent=75,
            reserved=b"\x00" * 27,
        )
        cfg = self._minimal_config(buzzers=[bz])

        exported = config_to_json(cfg)
        reimported = config_from_json(exported)

        assert len(reimported.buzzers) == 1
        bz2 = reimported.buzzers[0]
        assert bz2.drive_pin == 0x0C
        assert bz2.duty_percent == 75
        assert bz2.enable_active_high is True

    def test_system_pwr_pins_round_trip(self):
        from opendisplay.models.config_json import config_from_json, config_to_json

        cfg = self._minimal_config()
        exported = config_to_json(cfg)
        reimported = config_from_json(exported)

        assert reimported.system.pwr_pin_2 == 0xAA
        assert reimported.system.pwr_pin_3 == 0xBB

    def test_nfc_config_round_trip(self):
        from opendisplay.models.config_json import config_from_json, config_to_json

        nfc = NfcConfig.from_bytes(_nfc_payload(power_pin=0x30, adv_id=7))
        cfg = self._minimal_config(nfc_configs=[nfc])

        reimported = config_from_json(config_to_json(cfg))

        assert len(reimported.nfc_configs) == 1
        nfc2 = reimported.nfc_configs[0]
        assert nfc2.nfc_ic_type == NfcIcType.TNB132M
        assert nfc2.field_detect_mode == NfcFieldDetectMode.IRQ_LATCHED
        assert nfc2.power_pin == 0x30
        assert nfc2.adv_button_button_id == 7
        assert nfc2.enabled is True

    def test_flash_config_round_trip(self):
        from opendisplay.models.config_json import config_from_json, config_to_json

        flash = FlashConfig.from_bytes(_flash_payload(mosi=0x21, sck=0x22, cs=0x23))
        cfg = self._minimal_config(flash_configs=[flash])

        reimported = config_from_json(config_to_json(cfg))

        assert len(reimported.flash_configs) == 1
        flash2 = reimported.flash_configs[0]
        assert flash2.mosi_pin == 0x21
        assert flash2.sck_pin == 0x22
        assert flash2.cs_pin == 0x23
        assert flash2.enabled is True

    def test_manufacturer_simple_config_round_trip(self):
        from opendisplay.models.config_json import config_from_json, config_to_json

        cfg = self._minimal_config()
        cfg.manufacturer.simple_config_driver_index = 5
        cfg.manufacturer.simple_config_display_index = 6
        cfg.manufacturer.simple_config_power_index = 7
        cfg.manufacturer.simple_config_configured_at = 1700000000

        reimported = config_from_json(config_to_json(cfg))

        assert reimported.manufacturer.simple_config_driver_index == 5
        assert reimported.manufacturer.simple_config_display_index == 6
        assert reimported.manufacturer.simple_config_power_index == 7
        assert reimported.manufacturer.simple_config_configured_at == 1700000000
