"""TLV configuration serializer for OpenDisplay devices."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from .config_parser import (
    PACKET_TYPE_BINARY_INPUT,
    PACKET_TYPE_DATABUS,
    PACKET_TYPE_DISPLAY,
    PACKET_TYPE_LED,
    PACKET_TYPE_MANUFACTURER,
    PACKET_TYPE_POWER,
    PACKET_TYPE_SENSOR,
    PACKET_TYPE_SYSTEM,
    PACKET_TYPE_WIFI_CONFIG,
)

# PACKET_TYPE_SECURITY_CONFIG is intentionally not imported here — the serializer
# does not handle SecurityConfig (it is read-only via this library).

if TYPE_CHECKING:
    from ..models.config import (
        BinaryInputs,
        DataBus,
        DisplayConfig,
        GlobalConfig,
        LedConfig,
        ManufacturerData,
        PowerOption,
        SensorData,
        SystemConfig,
        WifiConfig,
    )


def calculate_config_crc(data: bytes) -> int:
    """Calculate CRC32 and return lower 16 bits.

    Uses standard CRC32 algorithm (same as zlib/firmware) but only returns
    the lower 16 bits for backwards compatibility with firmware.

    Firmware source: main.cpp:1543-1556, 1912-1914
    The firmware calculates full CRC32 but only uses lower 16 bits.

    Args:
        data: Config data to calculate CRC over

    Returns:
        Lower 16 bits of CRC32 value
    """
    crc = 0xFFFFFFFF

    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc = crc >> 1

    crc32 = (~crc) & 0xFFFFFFFF
    return crc32 & 0xFFFF  # Return lower 16 bits only


def serialize_system_config(config: SystemConfig) -> bytes:
    """Serialize SystemConfig to 22 bytes.

    Format (little-endian):
    - ic_type: uint16
    - communication_modes: uint8
    - device_flags: uint8
    - pwr_pin: uint8
    - reserved: 17 bytes

    Args:
        config: SystemConfig instance

    Returns:
        22 bytes of serialized data
    """
    data = struct.pack(
        "<HBBB",
        config.ic_type,
        config.communication_modes,
        config.device_flags,
        config.pwr_pin,
    )

    # Pad with reserved bytes to 22 total
    reserved = config.reserved if config.reserved else b"\x00" * 17
    return data + reserved[:17]


def serialize_manufacturer_data(config: ManufacturerData) -> bytes:
    """Serialize ManufacturerData to 22 bytes.

    Format (little-endian):
    - manufacturer_id: uint16
    - board_type: uint8
    - board_revision: uint8
    - reserved: 18 bytes

    Args:
        config: ManufacturerData instance

    Returns:
        22 bytes of serialized data
    """
    data = struct.pack(
        "<HBB",
        config.manufacturer_id,
        config.board_type,
        config.board_revision,
    )

    # Pad with reserved bytes to 22 total
    reserved = config.reserved if config.reserved else b"\x00" * 18
    return data + reserved[:18]


def serialize_power_option(config: PowerOption) -> bytes:
    """Serialize PowerOption to 30 bytes.

    Format (little-endian):
    - power_mode: uint8
    - battery_capacity_mah: 3 bytes (24-bit LE)
    - sleep_timeout_ms: uint16
    - tx_power: int8 (signed)
    - sleep_flags: uint8
    - battery_sense_pin: uint8
    - battery_sense_enable_pin: uint8
    - battery_sense_flags: uint8
    - capacity_estimator: uint8
    - voltage_scaling_factor: uint16
    - deep_sleep_current_ua: uint32
    - deep_sleep_time_seconds: uint16
    - reserved: 10 bytes

    Args:
        config: PowerOption instance

    Returns:
        30 bytes of serialized data
    """
    # Start with power_mode
    data = bytes([config.power_mode])

    # Add 3-byte battery capacity (little-endian)
    if isinstance(config.battery_capacity_mah, bytes):
        data += config.battery_capacity_mah[:3]
    else:
        # Convert int to 3 bytes little-endian
        capacity_bytes = config.battery_capacity_mah.to_bytes(3, byteorder="little")
        data += capacity_bytes

    # Pack remaining fields
    data += struct.pack(
        "<HbBBBBBHIH",
        config.sleep_timeout_ms,
        config.tx_power,
        config.sleep_flags,
        config.battery_sense_pin,
        config.battery_sense_enable_pin,
        config.battery_sense_flags,
        config.capacity_estimator,
        config.voltage_scaling_factor,
        config.deep_sleep_current_ua,
        config.deep_sleep_time_seconds,
    )

    # Pad with reserved bytes to 30 total
    reserved = config.reserved if config.reserved else b"\x00" * 10
    return data + reserved[:10]


def serialize_display_config(config: DisplayConfig) -> bytes:
    """Serialize DisplayConfig to 46 bytes.

    Format (little-endian):

    - instance_number: uint8
    - display_technology: uint8
    - panel_ic_type: uint16
    - pixel_width: uint16
    - pixel_height: uint16
    - active_width_mm: uint16
    - active_height_mm: uint16
    - tag_type: uint16
    - rotation: uint8
    - reset_pin: uint8
    - busy_pin: uint8
    - dc_pin: uint8
    - cs_pin: uint8
    - data_pin: uint8
    - partial_update_support: uint8
    - color_scheme: uint8
    - transmission_modes: uint8
    - clk_pin: uint8
    - reserved_pins: 7 bytes
    - reserved: 15 bytes

    Args:
        config: DisplayConfig instance

    Returns:
        46 bytes of serialized data
    """
    data = struct.pack(
        "<BBHHHHHHBBBBBBBBBB",
        config.instance_number,
        config.display_technology,
        config.panel_ic_type,
        config.pixel_width,
        config.pixel_height,
        config.active_width_mm,
        config.active_height_mm,
        config.tag_type,
        config.rotation,
        config.reset_pin,
        config.busy_pin,
        config.dc_pin,
        config.cs_pin,
        config.data_pin,
        config.partial_update_support,
        config.color_scheme,
        config.transmission_modes,
        config.clk_pin,
    )

    # Add reserved pins (7 bytes)
    reserved_pins = config.reserved_pins if config.reserved_pins else b"\xff" * 7
    data += reserved_pins[:7]

    # Add reserved bytes (15 bytes) to total 46
    reserved = config.reserved if config.reserved else b"\x00" * 15
    return data + reserved[:15]


def serialize_led_config(config: LedConfig) -> bytes:
    """Serialize LedConfig to 22 bytes.

    Format:

    - instance_number: uint8
    - led_type: uint8
    - led_1_r: uint8
    - led_2_g: uint8
    - led_3_b: uint8
    - led_4: uint8
    - led_flags: uint8
    - reserved: 15 bytes

    Args:
        config: LedConfig instance

    Returns:
        22 bytes of serialized data
    """
    data = struct.pack(
        "<BBBBBBB",
        config.instance_number,
        config.led_type,
        config.led_1_r,
        config.led_2_g,
        config.led_3_b,
        config.led_4,
        config.led_flags,
    )

    # Pad with reserved bytes to 22 total
    reserved = config.reserved if config.reserved else b"\x00" * 15
    return data + reserved[:15]


def serialize_sensor_data(config: SensorData) -> bytes:
    """Serialize SensorData to 30 bytes.

    Format (little-endian):

    - instance_number: uint8
    - sensor_type: uint16
    - bus_id: uint8
    - reserved: 26 bytes

    Args:
        config: SensorData instance

    Returns:
        30 bytes of serialized data
    """
    data = struct.pack(
        "<BHB",
        config.instance_number,
        config.sensor_type,
        config.bus_id,
    )

    # Pad with reserved bytes to 30 total
    reserved = config.reserved if config.reserved else b"\x00" * 26
    return data + reserved[:26]


def serialize_data_bus(config: DataBus) -> bytes:
    """Serialize DataBus to 30 bytes.

    Format (little-endian):

    - instance_number: uint8
    - bus_type: uint8
    - pin_1 through pin_7: 7x uint8
    - bus_speed_hz: uint32
    - bus_flags: uint8
    - pullups: uint8
    - pulldowns: uint8
    - reserved: 14 bytes

    Args:
        config: DataBus instance

    Returns:
        30 bytes of serialized data
    """
    data = struct.pack(
        "<BBBBBBBBBIBBB",
        config.instance_number,
        config.bus_type,
        config.pin_1,
        config.pin_2,
        config.pin_3,
        config.pin_4,
        config.pin_5,
        config.pin_6,
        config.pin_7,
        config.bus_speed_hz,
        config.bus_flags,
        config.pullups,
        config.pulldowns,
    )

    # Pad with reserved bytes to 30 total
    reserved = config.reserved if config.reserved else b"\x00" * 14
    return data + reserved[:14]


def serialize_binary_inputs(config: BinaryInputs) -> bytes:
    """Serialize BinaryInputs to 30 bytes.

    Format:

    - instance_number: uint8
    - input_type: uint8
    - display_as: uint8
    - reserved_pins: 8 bytes
    - input_flags: uint8
    - invert: uint8
    - pullups: uint8
    - pulldowns: uint8
    - button_data_byte_index: uint8
    - reserved: 14 bytes

    Args:
        config: BinaryInputs instance

    Returns:
        30 bytes of serialized data
    """
    data = struct.pack(
        "<BBB",
        config.instance_number,
        config.input_type,
        config.display_as,
    )

    # Add reserved pins (8 bytes)
    reserved_pins = config.reserved_pins if config.reserved_pins else b"\x00" * 8
    data += reserved_pins[:8]

    # Add flags
    data += struct.pack(
        "<BBBB",
        config.input_flags,
        config.invert,
        config.pullups,
        config.pulldowns,
    )

    # Dynamic return byte index (v1+ firmware feature)
    data += bytes([config.button_data_byte_index & 0xFF])

    # Pad with reserved bytes to 30 total
    reserved = config.reserved if config.reserved else b"\x00" * 14
    return data + reserved[:14]


def serialize_wifi_config(config: WifiConfig) -> bytes:
    """Serialize WifiConfig to 160 bytes."""
    return config.to_bytes()


def serialize_config(config: GlobalConfig) -> bytes:
    """Serialize complete GlobalConfig to TLV binary format.

    Format:
    [2 bytes: padding/reserved]
    [1 byte: version]
    [TLV packets...]
    [2 bytes: CRC16 (lower 16 bits of CRC32)]

    TLV Packet Format:
    [1 byte: packet_number]  # 0-3 for repeatable types
    [1 byte: packet_type]    # 0x01, 0x02, 0x04, 0x20-0x26
    [N bytes: fixed-size data]

    Args:
        config: GlobalConfig to serialize

    Returns:
        Complete config data ready to send to device

    Raises:
        ValueError: If config exceeds maximum size (4096 bytes)
    """
    if config.system is None or config.manufacturer is None or config.power is None:
        raise ValueError("Config is missing required packets")

    # Start with 2 bytes padding and 1 byte version
    packet_data = b"\x00\x00"  # 2 bytes padding
    packet_data += bytes([config.version])  # 1 byte version

    # Serialize single-instance packets
    packet_data += bytes([0, PACKET_TYPE_SYSTEM])
    packet_data += serialize_system_config(config.system)

    packet_data += bytes([0, PACKET_TYPE_MANUFACTURER])
    packet_data += serialize_manufacturer_data(config.manufacturer)

    packet_data += bytes([0, PACKET_TYPE_POWER])
    packet_data += serialize_power_option(config.power)

    # Serialize repeatable packets
    for i, display in enumerate(config.displays):
        if i >= 4:  # Max 4 instances
            break
        packet_data += bytes([i, PACKET_TYPE_DISPLAY])
        packet_data += serialize_display_config(display)

    for i, led in enumerate(config.leds):
        if i >= 4:  # Max 4 instances
            break
        packet_data += bytes([i, PACKET_TYPE_LED])
        packet_data += serialize_led_config(led)

    for i, sensor in enumerate(config.sensors):
        if i >= 4:  # Max 4 instances
            break
        packet_data += bytes([i, PACKET_TYPE_SENSOR])
        packet_data += serialize_sensor_data(sensor)

    for i, bus in enumerate(config.data_buses):
        if i >= 4:  # Max 4 instances
            break
        packet_data += bytes([i, PACKET_TYPE_DATABUS])
        packet_data += serialize_data_bus(bus)

    for i, binary_input in enumerate(config.binary_inputs):
        if i >= 4:  # Max 4 instances
            break
        packet_data += bytes([i, PACKET_TYPE_BINARY_INPUT])
        packet_data += serialize_binary_inputs(binary_input)

    if config.wifi_config is not None:
        packet_data += bytes([0, PACKET_TYPE_WIFI_CONFIG])
        packet_data += serialize_wifi_config(config.wifi_config)

    # SecurityConfig (0x27) is intentionally NOT serialized here.
    # It is read-only via this library: writing it could accidentally disable
    # device encryption if the key or flags are corrupted in transit.

    # Validate size (max 4096 bytes including wrapper and CRC)
    total_size = len(packet_data) + 2  # +2 for CRC
    if total_size > 4096:
        raise ValueError(f"Config size {total_size} bytes exceeds maximum 4096 bytes")

    # Calculate CRC over packet data (excluding CRC itself)
    crc16 = calculate_config_crc(packet_data)

    # Append CRC as 2 bytes little-endian
    packet_data += crc16.to_bytes(2, byteorder="little")

    return packet_data
