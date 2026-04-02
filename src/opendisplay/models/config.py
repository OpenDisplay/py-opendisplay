"""TLV configuration data structures.

These dataclasses map directly to the firmware's TLV packet structures.
Reference: OpenDisplayFirmware/src/structs.h
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import ClassVar

from epaper_dithering import ColorScheme

from .enums import (
    BoardManufacturer,
    BusType,
    CapacityEstimator,
    DIYBoardType,
    ICType,
    LedType,
    OpenDisplayBoardType,
    PowerMode,
    Rotation,
    SeeedBoardType,
    SensorType,
    SolumBoardType,
    TouchIcType,
    WaveshareBoardType,
    WifiEncryption,
    get_board_type_name,
    get_manufacturer_name,
)


@dataclass
class SystemConfig:
    """System configuration (TLV packet type 0x01).

    Size: 22 bytes (packed struct from firmware)
    """

    ic_type: int  # uint16
    communication_modes: int  # uint8 bitfield
    device_flags: int  # uint8 bitfield
    pwr_pin: int  # uint8 (0xFF = none)
    reserved: bytes  # 15 bytes
    pwr_pin_2: int = 0xFF  # uint8 (0xFF = firmware default)
    pwr_pin_3: int = 0xFF  # uint8 (0xFF = firmware default)

    @property
    def has_pwr_pin(self) -> bool:
        """Check if device has external power management pin (DEVICE_FLAG_PWR_PIN)."""
        return bool(self.device_flags & 0x01)

    @property
    def needs_xiaoinit(self) -> bool:
        """Check if xiaoinit() should be called after config load - nRF52840 only (DEVICE_FLAG_XIAOINIT)."""
        return bool(self.device_flags & 0x02)

    @property
    def needs_ws_pp_init(self) -> bool:
        """Check if Waveshare PhotoPainter power-on init should be called (DEVICE_FLAG_WS_PP_INIT)."""
        return bool(self.device_flags & 0x04)

    @property
    def ic_type_enum(self) -> ICType | int:
        """Get IC type as enum, or raw int if unknown."""
        try:
            return ICType(self.ic_type)
        except ValueError:
            return self.ic_type

    SIZE: ClassVar[int] = 22

    @classmethod
    def from_bytes(cls, data: bytes) -> SystemConfig:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid SystemConfig size: {len(data)} < {cls.SIZE}")

        return cls(
            ic_type=int.from_bytes(data[0:2], "little"),
            communication_modes=data[2],
            device_flags=data[3],
            pwr_pin=data[4],
            reserved=data[5:20],
            pwr_pin_2=data[20],
            pwr_pin_3=data[21],
        )


@dataclass
class ManufacturerData:
    """Manufacturer data (TLV packet type 0x02).

    Size: 22 bytes (packed struct from firmware)
    """

    manufacturer_id: int  # uint16
    board_type: int  # uint8
    board_revision: int  # uint8
    reserved: bytes  # 18 bytes

    @property
    def manufacturer_id_enum(self) -> BoardManufacturer | int:
        """Get manufacturer ID as enum, or raw int if unknown."""
        try:
            return BoardManufacturer(self.manufacturer_id)
        except ValueError:
            return self.manufacturer_id

    @property
    def manufacturer_name(self) -> str | None:
        """Get canonical manufacturer name, if known."""
        return get_manufacturer_name(self.manufacturer_id)

    @property
    def board_type_enum(
        self,
    ) -> DIYBoardType | SeeedBoardType | SolumBoardType | WaveshareBoardType | OpenDisplayBoardType | int:
        """Get board type as manufacturer-specific enum, or raw int if unknown."""
        manufacturer = self.manufacturer_id_enum
        if not isinstance(manufacturer, BoardManufacturer):
            return self.board_type

        try:
            if manufacturer == BoardManufacturer.DIY:
                return DIYBoardType(self.board_type)
            if manufacturer == BoardManufacturer.SEEED:
                return SeeedBoardType(self.board_type)
            if manufacturer == BoardManufacturer.WAVESHARE:
                return WaveshareBoardType(self.board_type)
            if manufacturer == BoardManufacturer.SOLUM:
                return SolumBoardType(self.board_type)
            if manufacturer == BoardManufacturer.OPENDISPLAY:
                return OpenDisplayBoardType(self.board_type)
        except ValueError:
            return self.board_type

        return self.board_type

    @property
    def board_type_name(self) -> str | None:
        """Get human-readable board type name for this manufacturer, if known."""
        return get_board_type_name(self.manufacturer_id, self.board_type)

    SIZE: ClassVar[int] = 22

    @classmethod
    def from_bytes(cls, data: bytes) -> ManufacturerData:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid ManufacturerData size: {len(data)} < {cls.SIZE}")

        return cls(
            manufacturer_id=int.from_bytes(data[0:2], "little"),
            board_type=data[2],
            board_revision=data[3],
            reserved=data[4:22],
        )


@dataclass
class PowerOption:
    """Power configuration (TLV packet type 0x04).

    Size: 32 bytes (packed struct from firmware)
    """

    power_mode: int  # uint8
    battery_capacity_mah: bytes  # 3 bytes (24-bit little-endian)
    sleep_timeout_ms: int  # uint16
    tx_power: int  # uint8
    sleep_flags: int  # uint8 bitfield
    battery_sense_pin: int  # uint8 (0xFF = none)
    battery_sense_enable_pin: int  # uint8 (0xFF = none)
    battery_sense_flags: int  # uint8 bitfield
    capacity_estimator: int  # uint8
    voltage_scaling_factor: int  # uint16
    deep_sleep_current_ua: int  # uint32
    deep_sleep_time_seconds: int  # uint16
    reserved: bytes  # 10 bytes

    @property
    def battery_mah(self) -> int:
        """Get battery capacity in mAh (converts 3-byte little-endian to integer)."""
        return int.from_bytes(self.battery_capacity_mah[:3], "little")

    @property
    def power_mode_enum(self) -> PowerMode | int:
        """Get power mode as enum, or raw int if unknown."""
        try:
            return PowerMode(self.power_mode)
        except ValueError:
            return self.power_mode

    @property
    def capacity_estimator_enum(self) -> CapacityEstimator | int:
        """Get battery chemistry estimator as enum, or raw int if unknown."""
        try:
            return CapacityEstimator(self.capacity_estimator)
        except ValueError:
            return self.capacity_estimator

    @property
    def has_battery_sense(self) -> bool:
        """Return True if device has a battery sense circuit."""
        return self.battery_sense_pin != 0xFF

    SIZE: ClassVar[int] = 32

    @classmethod
    def from_bytes(cls, data: bytes) -> PowerOption:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid PowerOption size: {len(data)} < {cls.SIZE}")

        return cls(
            power_mode=data[0],
            battery_capacity_mah=data[1:4],
            sleep_timeout_ms=int.from_bytes(data[4:6], "little"),
            tx_power=data[6],
            sleep_flags=data[7],
            battery_sense_pin=data[8],
            battery_sense_enable_pin=data[9],
            battery_sense_flags=data[10],
            capacity_estimator=data[11],
            voltage_scaling_factor=int.from_bytes(data[12:14], "little"),
            deep_sleep_current_ua=int.from_bytes(data[14:18], "little"),
            deep_sleep_time_seconds=int.from_bytes(data[18:20], "little"),
            reserved=data[20:32],
        )


@dataclass
class DisplayConfig:
    """Display configuration (TLV packet type 0x20, repeatable max 4).

    Size: 66 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8 (0-3)
    display_technology: int  # uint8
    panel_ic_type: int  # uint16
    pixel_width: int  # uint16
    pixel_height: int  # uint16
    active_width_mm: int  # uint16
    active_height_mm: int  # uint16
    tag_type: int  # uint16 (legacy)
    rotation: int  # uint8 (degrees)
    reset_pin: int  # uint8 (0xFF = none)
    busy_pin: int  # uint8 (0xFF = none)
    dc_pin: int  # uint8 (0xFF = none)
    cs_pin: int  # uint8 (0xFF = none)
    data_pin: int  # uint8
    partial_update_support: int  # uint8
    color_scheme: int  # uint8
    transmission_modes: int  # uint8 bitfield
    clk_pin: int  # uint8
    reserved_pins: bytes  # 7 reserved pins
    full_update_mC: int  # uint16 (milli-coulombs per full update)
    reserved: bytes  # 13 bytes

    @property
    def supports_raw(self) -> bool:
        """Check if display supports raw image transmission (TRANSMISSION_MODE_RAW)."""
        return bool(self.transmission_modes & 0x01)

    @property
    def supports_zip(self) -> bool:
        """Check if display supports ZIP compressed transmission (TRANSMISSION_MODE_ZIP)."""
        return bool(self.transmission_modes & 0x02)

    @property
    def supports_g5(self) -> bool:
        """Check if display supports Group 5 compression (TRANSMISSION_MODE_G5)."""
        return bool(self.transmission_modes & 0x04)

    @property
    def supports_direct_write(self) -> bool:
        """Check if display supports direct write mode - bufferless (TRANSMISSION_MODE_DIRECT_WRITE)."""
        return bool(self.transmission_modes & 0x08)

    @property
    def no_boot_text(self) -> bool:
        """Check if display should suppress boot text (TRANSMISSION_MODE_NO_BOOT_TEXT)."""
        return bool(self.transmission_modes & 0x80)

    @property
    def screen_diagonal_inches(self) -> float | None:
        """Get physical screen diagonal in inches if dimensions are known."""
        if self.active_width_mm <= 0 or self.active_height_mm <= 0:
            return None
        diagonal_mm = math.hypot(self.active_width_mm, self.active_height_mm)
        return diagonal_mm / 25.4

    @property
    def color_scheme_enum(self) -> ColorScheme | int:
        """Get color scheme as enum, or raw int if unknown."""
        try:
            return ColorScheme.from_value(self.color_scheme)
        except ValueError:
            return self.color_scheme

    @property
    def rotation_enum(self) -> Rotation | int:
        """Get rotation as enum, or raw int if unknown.

        Firmware stores rotation as an index (0=0°, 1=90°, 2=180°, 3=270°).
        """
        _INDEX_TO_ROTATION = {
            0: Rotation.ROTATE_0,
            1: Rotation.ROTATE_90,
            2: Rotation.ROTATE_180,
            3: Rotation.ROTATE_270,
        }
        try:
            return Rotation(self.rotation)
        except ValueError:
            return _INDEX_TO_ROTATION.get(self.rotation, self.rotation)

    SIZE: ClassVar[int] = 66

    @classmethod
    def from_bytes(cls, data: bytes) -> DisplayConfig:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid DisplayConfig size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            display_technology=data[1],
            panel_ic_type=int.from_bytes(data[2:4], "little"),
            pixel_width=int.from_bytes(data[4:6], "little"),
            pixel_height=int.from_bytes(data[6:8], "little"),
            active_width_mm=int.from_bytes(data[8:10], "little"),
            active_height_mm=int.from_bytes(data[10:12], "little"),
            tag_type=int.from_bytes(data[12:14], "little"),
            rotation=data[14],
            reset_pin=data[15],
            busy_pin=data[16],
            dc_pin=data[17],
            cs_pin=data[18],
            data_pin=data[19],
            partial_update_support=data[20],
            color_scheme=data[21],
            transmission_modes=data[22],
            clk_pin=data[23],
            reserved_pins=data[24:31],  # pins 2-8
            full_update_mC=int.from_bytes(data[31:33], "little"),
            reserved=data[33:66],
        )


@dataclass
class LedConfig:
    """LED configuration (TLV packet type 0x21, repeatable max 4).

    Size: 22 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8
    led_type: int  # uint8
    led_1_r: int  # uint8 (red channel pin)
    led_2_g: int  # uint8 (green channel pin)
    led_3_b: int  # uint8 (blue channel pin)
    led_4: int  # uint8 (4th channel pin)
    led_flags: int  # uint8 bitfield
    reserved: bytes  # 15 bytes

    @property
    def led_type_enum(self) -> LedType | int:
        """Get LED type as enum, or raw int if unknown."""
        try:
            return LedType(self.led_type)
        except ValueError:
            return self.led_type

    SIZE: ClassVar[int] = 22

    @classmethod
    def from_bytes(cls, data: bytes) -> LedConfig:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid LedConfig size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            led_type=data[1],
            led_1_r=data[2],
            led_2_g=data[3],
            led_3_b=data[4],
            led_4=data[5],
            led_flags=data[6],
            reserved=data[7:22],
        )


@dataclass
class SensorData:
    """Sensor configuration (TLV packet type 0x23, repeatable max 4).

    Size: 30 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8
    sensor_type: int  # uint16
    bus_id: int  # uint8
    i2c_addr_7bit: int = 0  # uint8 (0/0xFF = auto/default)
    msd_data_start_byte: int = 0  # uint8: start byte in dynamicreturndata
    reserved: bytes = b""  # 24 bytes

    @property
    def sensor_type_enum(self) -> SensorType | int:
        """Get sensor type as enum, or raw int if unknown."""
        try:
            return SensorType(self.sensor_type)
        except ValueError:
            return self.sensor_type

    SIZE: ClassVar[int] = 30

    @classmethod
    def from_bytes(cls, data: bytes) -> SensorData:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid SensorData size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            sensor_type=int.from_bytes(data[1:3], "little"),
            bus_id=data[3],
            i2c_addr_7bit=data[4],
            msd_data_start_byte=data[5],
            reserved=data[6:30],
        )


@dataclass
class DataBus:
    """Data bus configuration (TLV packet type 0x24, repeatable max 4).

    Size: 28 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8
    bus_type: int  # uint8
    pin_1: int  # uint8 (SCL for I2C)
    pin_2: int  # uint8 (SDA for I2C)
    pin_3: int  # uint8
    pin_4: int  # uint8
    pin_5: int  # uint8
    pin_6: int  # uint8
    pin_7: int  # uint8
    bus_speed_hz: int  # uint32
    bus_flags: int  # uint8 bitfield
    pullups: int  # uint8 bitfield
    pulldowns: int  # uint8 bitfield
    reserved: bytes  # 14 bytes

    SIZE: ClassVar[int] = 28

    @classmethod
    def from_bytes(cls, data: bytes) -> DataBus:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid DataBus size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            bus_type=data[1],
            pin_1=data[2],
            pin_2=data[3],
            pin_3=data[4],
            pin_4=data[5],
            pin_5=data[6],
            pin_6=data[7],
            pin_7=data[8],
            bus_speed_hz=int.from_bytes(data[9:13], "little"),
            bus_flags=data[13],
            pullups=data[14],
            pulldowns=data[15],
            reserved=data[16:28],
        )

    @property
    def bus_type_enum(self) -> BusType | int:
        """Get bus type as enum, or raw int if unknown."""
        try:
            return BusType(self.bus_type)
        except ValueError:
            return self.bus_type


@dataclass
class BinaryInputs:
    """Binary inputs configuration (TLV packet type 0x25, repeatable max 4).

    Size: 30 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8
    input_type: int  # uint8
    display_as: int  # uint8
    reserved_pins: bytes  # 8 reserved pins
    input_flags: int  # uint8 bitfield
    invert: int  # uint8 bitfield
    pullups: int  # uint8 bitfield
    pulldowns: int  # uint8 bitfield
    reserved: bytes = b""  # 14 bytes
    button_data_byte_index: int = 0  # uint8 (v1+): dynamic return byte index (0-10)

    SIZE: ClassVar[int] = 30

    @classmethod
    def from_bytes(cls, data: bytes) -> BinaryInputs:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid BinaryInputs size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            input_type=data[1],
            display_as=data[2],
            reserved_pins=data[3:11],  # 8 pins
            input_flags=data[11],
            invert=data[12],
            pullups=data[13],
            pulldowns=data[14],
            button_data_byte_index=data[15],
            reserved=data[16:30],
        )


@dataclass
class WifiConfig:
    """WiFi configuration (TLV packet type 0x26).

    Size: 160 bytes (firmware fixed layout, excluding packet CRC)
    """

    ssid: bytes  # 32-byte null-terminated string buffer
    password: bytes  # 32-byte null-terminated string buffer
    encryption_type: int  # uint8
    server_url: bytes  # 64-byte null-terminated string buffer
    server_port: int  # uint16 (big-endian / network byte order)
    reserved: bytes  # 29 bytes

    SIZE: ClassVar[int] = 160

    @staticmethod
    def encode_c_string(value: str, size: int) -> bytes:
        """Encode string into fixed-size null-padded C string bytes."""
        encoded = value.encode("utf-8")
        return encoded[:size].ljust(size, b"\x00")

    @staticmethod
    def decode_c_string(value: bytes) -> str:
        """Decode fixed-size C string bytes (truncate at first null byte)."""
        return value.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    @property
    def ssid_text(self) -> str:
        """Get SSID as decoded text."""
        return self.decode_c_string(self.ssid)

    @property
    def password_text(self) -> str:
        """Get password as decoded text."""
        return self.decode_c_string(self.password)

    @property
    def server_url_text(self) -> str:
        """Get server URL/hostname as decoded text."""
        return self.decode_c_string(self.server_url)

    @property
    def encryption_type_enum(self) -> WifiEncryption | int:
        """Get WiFi encryption type as enum, or raw int if unknown."""
        try:
            return WifiEncryption(self.encryption_type)
        except ValueError:
            return self.encryption_type

    @classmethod
    def from_bytes(cls, data: bytes) -> WifiConfig:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid WifiConfig size: {len(data)} < {cls.SIZE}")

        return cls(
            ssid=data[0:32],
            password=data[32:64],
            encryption_type=data[64],
            server_url=data[65:129],
            server_port=int.from_bytes(data[129:131], "big"),
            reserved=data[131:160],
        )

    @classmethod
    def from_strings(
        cls,
        *,
        ssid: str,
        password: str,
        encryption_type: int = 0,
        server_url: str = "",
        server_port: int = 2446,
        reserved: bytes | None = None,
    ) -> WifiConfig:
        """Build a WiFi config from user-facing string fields."""
        return cls(
            ssid=cls.encode_c_string(ssid, 32),
            password=cls.encode_c_string(password, 32),
            encryption_type=encryption_type & 0xFF,
            server_url=cls.encode_c_string(server_url, 64),
            server_port=server_port & 0xFFFF,
            reserved=(reserved or b"\x00" * 29)[:29],
        )

    def to_bytes(self) -> bytes:
        """Serialize to firmware packet bytes."""
        ssid = self.ssid[:32].ljust(32, b"\x00")
        password = self.password[:32].ljust(32, b"\x00")
        server_url = self.server_url[:64].ljust(64, b"\x00")
        reserved = self.reserved[:29].ljust(29, b"\x00")
        return (
            ssid
            + password
            + bytes([self.encryption_type & 0xFF])
            + server_url
            + self.server_port.to_bytes(2, byteorder="big")
            + reserved
        )


@dataclass
class SecurityConfig:
    """Security and encryption configuration (TLV packet type 0x27).

    Size: 64 bytes (firmware fixed layout, excluding packet header)
    """

    encryption_enabled: int  # uint8: 0=disabled, 1=enabled
    encryption_key: bytes  # 16-byte AES-128 master key (all-zero means disabled)
    session_timeout_seconds: int  # uint16 LE: 0 = no timeout
    flags: int  # uint8 bitfield (see flag properties below)
    reset_pin: int  # uint8: pin number for hardware reset
    reserved: bytes  # 43 bytes

    SIZE: ClassVar[int] = 64

    @property
    def encryption_enabled_flag(self) -> bool:
        """True if encryption is both enabled and key is non-zero."""
        return self.encryption_enabled != 0 and any(self.encryption_key)

    @property
    def rewrite_allowed(self) -> bool:
        """Bit 0: allow unauthenticated WRITE_CONFIG even when encryption is on."""
        return bool(self.flags & 0x01)

    @property
    def show_key_on_screen(self) -> bool:
        """Bit 1: display encryption key on device screen."""
        return bool(self.flags & 0x02)

    @property
    def reset_pin_enabled(self) -> bool:
        """Bit 2: hardware reset pin is active."""
        return bool(self.flags & 0x04)

    @property
    def reset_pin_polarity(self) -> bool:
        """Bit 3: reset pin polarity (True = active high)."""
        return bool(self.flags & 0x08)

    @property
    def reset_pin_pullup(self) -> bool:
        """Bit 4: enable internal pull-up on reset pin."""
        return bool(self.flags & 0x10)

    @property
    def reset_pin_pulldown(self) -> bool:
        """Bit 5: enable internal pull-down on reset pin."""
        return bool(self.flags & 0x20)

    @classmethod
    def from_bytes(cls, data: bytes) -> SecurityConfig:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid SecurityConfig size: {len(data)} < {cls.SIZE}")
        return cls(
            encryption_enabled=data[0],
            encryption_key=bytes(data[1:17]),
            session_timeout_seconds=int.from_bytes(data[17:19], "little"),
            flags=data[19],
            reset_pin=data[20],
            reserved=bytes(data[21:64]),
        )


@dataclass
class TouchController:
    """Touch controller configuration (TLV packet type 0x28, repeatable max 4).

    Size: 32 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8 (0-3)
    touch_ic_type: int  # uint16
    bus_id: int  # uint8 (0xFF = default bus 0)
    i2c_addr_7bit: int  # uint8 (0/0xFF = auto)
    int_pin: int  # uint8 (0xFF = poll only)
    rst_pin: int  # uint8 (0xFF = skip reset)
    display_instance: int  # uint8
    flags: int  # uint8 bitfield
    poll_interval_ms: int  # uint8 (0 = default 25ms)
    touch_data_start_byte: int  # uint8 (0-6)
    reserved: bytes  # 21 bytes

    SIZE: ClassVar[int] = 32

    @property
    def touch_ic_type_enum(self) -> TouchIcType | int:
        """Get touch IC type as enum, or raw int if unknown."""
        try:
            return TouchIcType(self.touch_ic_type)
        except ValueError:
            return self.touch_ic_type

    @property
    def invert_x(self) -> bool:
        """Bit 0: invert X axis coordinates."""
        return bool(self.flags & 0x01)

    @property
    def invert_y(self) -> bool:
        """Bit 1: invert Y axis coordinates."""
        return bool(self.flags & 0x02)

    @property
    def swap_xy(self) -> bool:
        """Bit 2: swap X and Y axes."""
        return bool(self.flags & 0x04)

    @classmethod
    def from_bytes(cls, data: bytes) -> TouchController:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid TouchController size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            touch_ic_type=int.from_bytes(data[1:3], "little"),
            bus_id=data[3],
            i2c_addr_7bit=data[4],
            int_pin=data[5],
            rst_pin=data[6],
            display_instance=data[7],
            flags=data[8],
            poll_interval_ms=data[9],
            touch_data_start_byte=data[10],
            reserved=data[11:32],
        )


@dataclass
class PassiveBuzzer:
    """Passive buzzer configuration (TLV packet type 0x29, repeatable max 4).

    Size: 32 bytes (packed struct from firmware)
    """

    instance_number: int  # uint8 (0-3)
    drive_pin: int  # uint8
    enable_pin: int  # uint8 (0xFF = unused)
    flags: int  # uint8 bitfield
    duty_percent: int  # uint8 (0 = default 50%)
    reserved: bytes  # 27 bytes

    SIZE: ClassVar[int] = 32

    @property
    def enable_active_high(self) -> bool:
        """Bit 0: enable pin is active high."""
        return bool(self.flags & 0x01)

    @classmethod
    def from_bytes(cls, data: bytes) -> PassiveBuzzer:
        """Parse from TLV packet data."""
        if len(data) < cls.SIZE:
            raise ValueError(f"Invalid PassiveBuzzer size: {len(data)} < {cls.SIZE}")

        return cls(
            instance_number=data[0],
            drive_pin=data[1],
            enable_pin=data[2],
            flags=data[3],
            duty_percent=data[4],
            reserved=data[5:32],
        )


@dataclass
class GlobalConfig:
    """Complete device configuration parsed from TLV data.

    Corresponds to GlobalConfig struct in firmware.
    """

    # Required single-instance packets
    system: SystemConfig
    manufacturer: ManufacturerData
    power: PowerOption

    # Optional repeatable packets (max 4 each)
    displays: list[DisplayConfig] = field(default_factory=list)
    leds: list[LedConfig] = field(default_factory=list)
    sensors: list[SensorData] = field(default_factory=list)
    data_buses: list[DataBus] = field(default_factory=list)
    binary_inputs: list[BinaryInputs] = field(default_factory=list)
    wifi_config: WifiConfig | None = None
    security_config: SecurityConfig | None = None
    touch_controllers: list[TouchController] = field(default_factory=list)
    buzzers: list[PassiveBuzzer] = field(default_factory=list)

    # Metadata
    version: int = 0
    minor_version: int = 0
    loaded: bool = False
