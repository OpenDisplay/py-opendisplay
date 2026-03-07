"""TLV configuration parser for OpenDisplay devices."""

from __future__ import annotations

import logging
import struct

from ..exceptions import ConfigParseError
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

_LOGGER = logging.getLogger(__name__)


# TLV packet type IDs
PACKET_TYPE_SYSTEM = 0x01
PACKET_TYPE_MANUFACTURER = 0x02
PACKET_TYPE_POWER = 0x04
PACKET_TYPE_DISPLAY = 0x20
PACKET_TYPE_LED = 0x21
PACKET_TYPE_SENSOR = 0x23
PACKET_TYPE_DATABUS = 0x24
PACKET_TYPE_BINARY_INPUT = 0x25
PACKET_TYPE_WIFI_CONFIG = 0x26

WIFI_CONFIG_SIZE = 160
WIFI_CONFIG_LEGACY_SIZE = 65


def parse_config_response(raw_data: bytes) -> GlobalConfig:
    """Parse complete TLV config response from device.

    Firmware sends config data with a wrapper: [length:2][version:1][packets...][crc:2]
    This function strips the wrapper and passes clean packet data to the TLV parser.

    Args:
        raw_data: Complete TLV data assembled from all BLE chunks

    Returns:
        Parsed GlobalConfig

    Raises:
        ConfigParseError: If data is too short or invalid
    """
    if len(raw_data) < 5:  # Min: 2 (length) + 1 (version) + 0 (packets) + 2 (crc)
        raise ConfigParseError(f"Config data too short: {len(raw_data)} bytes (need at least 5)")

    # Parse TLV wrapper header
    config_length = int.from_bytes(raw_data[0:2], "little")
    config_version = raw_data[2]

    _LOGGER.debug(
        "TLV wrapper: length=%d bytes, version=%d",
        config_length,
        config_version,
    )

    # Extract packet data (skip 3-byte header, ignore 2-byte CRC at end)
    if len(raw_data) > 5:
        packet_data = raw_data[3:-2]  # Skip header, ignore CRC
    else:
        packet_data = raw_data[3:]  # Skip header only

    _LOGGER.debug("Packet data after wrapper strip: %d bytes", len(packet_data))

    # Parse TLV packets
    return parse_tlv_config(packet_data, version=config_version)


def parse_tlv_config(data: bytes, version: int = 1) -> GlobalConfig:
    """Parse complete TLV configuration from device response.

    BLE format: [TLV packets...] (raw TLV data, no header)

    Each TLV packet: [packet_number:1][packet_type:1][data:fixed_size]

    Args:
        data: Raw TLV data from device (after echo bytes stripped)
        version: Config version from wrapper (default 1 if called directly)

    Returns:
        GlobalConfig with all parsed configuration

    Raises:
        ConfigParseError: If parsing fails
    """
    if len(data) < 2:
        raise ConfigParseError(f"TLV data too short: {len(data)} bytes (need at least 2)")

    _LOGGER.debug("Parsing TLV config, %d bytes", len(data))

    # Parse TLV packets (OEPL format: [packet_number:1][packet_id:1][fixed_data])
    offset = 0
    packets = {}

    while offset < len(data) - 1:
        if offset + 2 > len(data):
            break  # Not enough data for packet header

        packet_number = data[offset]
        packet_type = data[offset + 1]
        offset += 2

        # Determine packet size based on type
        packet_size = _get_packet_size(packet_type)
        if packet_size is None:
            _LOGGER.warning("Unknown packet type 0x%02x at offset %d, skipping", packet_type, offset - 2)
            break

        # Extract packet data
        if offset + packet_size > len(data):
            remaining = len(data) - offset

            # Legacy firmware can return a short 0x26 payload (SSID/password/encryption only).
            if packet_type == PACKET_TYPE_WIFI_CONFIG and remaining == WIFI_CONFIG_LEGACY_SIZE:
                _LOGGER.debug(
                    "Detected legacy wifi_config packet size (%d bytes)",
                    WIFI_CONFIG_LEGACY_SIZE,
                )
                packet_size = WIFI_CONFIG_LEGACY_SIZE
            else:
                raise ConfigParseError(
                    f"Packet type 0x{packet_type:02x} truncated: "
                    f"need {packet_size} bytes, have {remaining}"
                )

        packet_data = data[offset:offset + packet_size]
        offset += packet_size

        # Store packet
        key = (packet_type, packet_number)
        packets[key] = packet_data

        _LOGGER.debug(
            "Parsed packet: type=0x%02x, num=%d, size=%d",
            packet_type, packet_number, packet_size
        )

    # Parse packets in a single pass
    # Note: Firmware uses global sequential numbering across all packet types
    system = None
    manufacturer = None
    power = None
    displays = []
    leds = []
    sensors = []
    data_buses = []
    binary_inputs = []
    wifi_config = None

    for (packet_type, packet_number), data in packets.items():
        if packet_type == PACKET_TYPE_SYSTEM:
            system = _parse_system_config(data)
        elif packet_type == PACKET_TYPE_MANUFACTURER:
            manufacturer = _parse_manufacturer_data(data)
        elif packet_type == PACKET_TYPE_POWER:
            power = _parse_power_option(data)
        elif packet_type == PACKET_TYPE_DISPLAY:
            displays.append(_parse_display_config(data))
        elif packet_type == PACKET_TYPE_LED:
            leds.append(_parse_led_config(data))
        elif packet_type == PACKET_TYPE_SENSOR:
            sensors.append(_parse_sensor_data(data))
        elif packet_type == PACKET_TYPE_DATABUS:
            data_buses.append(_parse_data_bus(data))
        elif packet_type == PACKET_TYPE_BINARY_INPUT:
            binary_inputs.append(_parse_binary_inputs(data))
        elif packet_type == PACKET_TYPE_WIFI_CONFIG:
            wifi_config = _parse_wifi_config(data)

    missing_required = []
    if system is None:
        missing_required.append("system")
    if manufacturer is None:
        missing_required.append("manufacturer")
    if power is None:
        missing_required.append("power")
    if not displays:
        missing_required.append("display")
    if missing_required:
        raise ConfigParseError(
            "Missing required packet(s): " + ", ".join(missing_required)
        )

    assert system is not None
    assert manufacturer is not None
    assert power is not None

    return GlobalConfig(
        system=system,
        manufacturer=manufacturer,
        power=power,
        displays=displays,
        leds=leds,
        sensors=sensors,
        data_buses=data_buses,
        binary_inputs=binary_inputs,
        wifi_config=wifi_config,
        version=version,  # From firmware wrapper
        minor_version=1,  # Not stored in device (only single version byte exists)
        loaded=True,
    )


def _get_packet_size(packet_type: int) -> int | None:
    """Get expected size for a packet type.

    Args:
        packet_type: TLV packet type ID

    Returns:
        Expected packet size in bytes, or None if unknown type
    """
    sizes = {
        PACKET_TYPE_SYSTEM: 22,
        PACKET_TYPE_MANUFACTURER: 22,
        PACKET_TYPE_POWER: 30,  # Fixed: was 32
        PACKET_TYPE_DISPLAY: 46,  # Fixed: was 66
        PACKET_TYPE_LED: 22,
        PACKET_TYPE_SENSOR: 30,
        PACKET_TYPE_DATABUS: 30,  # Fixed: was 28
        PACKET_TYPE_BINARY_INPUT: 30,  # Fixed: was 29
        PACKET_TYPE_WIFI_CONFIG: 160,
    }
    return sizes.get(packet_type)


def _parse_system_config(data: bytes) -> SystemConfig:
    """Parse SystemConfig packet (0x01, 22 bytes)."""
    if len(data) < 22:
        raise ConfigParseError(f"SystemConfig too short: {len(data)} bytes (need 22)")

    ic_type, comm_modes, dev_flags, pwr_pin = struct.unpack_from("<HBBB", data, 0)
    reserved = data[5:22]

    return SystemConfig(
        ic_type=ic_type,
        communication_modes=comm_modes,
        device_flags=dev_flags,
        pwr_pin=pwr_pin,
        reserved=reserved,
    )


def _parse_manufacturer_data(data: bytes) -> ManufacturerData:
    """Parse ManufacturerData packet (0x02, 22 bytes)."""
    if len(data) < 22:
        raise ConfigParseError(f"ManufacturerData too short: {len(data)} bytes (need 22)")

    mfg_id, board_type, board_rev = struct.unpack_from("<HBB", data, 0)
    reserved = data[4:22]

    return ManufacturerData(
        manufacturer_id=mfg_id,
        board_type=board_type,
        board_revision=board_rev,
        reserved=reserved,
    )


def _parse_power_option(data: bytes) -> PowerOption:
    """Parse PowerOption packet (0x04, 30 bytes)."""
    if len(data) < 30:
        raise ConfigParseError(f"PowerOption too short: {len(data)} bytes (need 30)")

    power_mode = data[0]

    # Battery capacity is 3 bytes (little-endian)
    battery_capacity_bytes = data[1:4]

    (
        sleep_timeout,
        tx_power,
        sleep_flags,
        battery_sense_pin,
        battery_sense_enable_pin,
        battery_sense_flags,
        capacity_estimator,
        voltage_scaling_factor,
        deep_sleep_current_ua,
        deep_sleep_time_seconds,
    ) = struct.unpack_from("<HbBBBBBHIH", data, 4)

    reserved = data[20:30]  # 10 reserved bytes, not 12

    return PowerOption(
        power_mode=power_mode,
        battery_capacity_mah=battery_capacity_bytes,
        sleep_timeout_ms=sleep_timeout,
        tx_power=tx_power,
        sleep_flags=sleep_flags,
        battery_sense_pin=battery_sense_pin,
        battery_sense_enable_pin=battery_sense_enable_pin,
        battery_sense_flags=battery_sense_flags,
        capacity_estimator=capacity_estimator,
        voltage_scaling_factor=voltage_scaling_factor,
        deep_sleep_current_ua=deep_sleep_current_ua,
        deep_sleep_time_seconds=deep_sleep_time_seconds,
        reserved=reserved,
    )


def _parse_display_config(data: bytes) -> DisplayConfig:
    """Parse DisplayConfig packet (0x20, 66 bytes)."""
    if len(data) < 46:
        raise ConfigParseError(f"DisplayConfig too short: {len(data)} bytes (need 46)")

    (
        instance_num,
        display_tech,
        panel_ic,
        pixel_width,
        pixel_height,
        active_width_mm,
        active_height_mm,
        tag_type,
        rotation,
        reset_pin,
        busy_pin,
        dc_pin,
        cs_pin,
        data_pin,
        partial_update,
        color_scheme,
        trans_modes,
        clk_pin,
    ) = struct.unpack_from("<BBHHHHHHBBBBBBBBBB", data, 0)

    reserved_pins = data[24:31]  # 7 reserved pin bytes
    full_update_mC = int.from_bytes(data[31:33], 'little') if len(data) >= 33 else 0
    reserved = data[33:] if len(data) >= 33 else data[31:]

    return DisplayConfig(
        instance_number=instance_num,
        display_technology=display_tech,
        panel_ic_type=panel_ic,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
        active_width_mm=active_width_mm,
        active_height_mm=active_height_mm,
        tag_type=tag_type,
        rotation=rotation,
        reset_pin=reset_pin,
        busy_pin=busy_pin,
        dc_pin=dc_pin,
        cs_pin=cs_pin,
        data_pin=data_pin,
        partial_update_support=partial_update,
        color_scheme=color_scheme,
        transmission_modes=trans_modes,
        clk_pin=clk_pin,
        reserved_pins=reserved_pins,
        full_update_mC=full_update_mC,
        reserved=reserved,
    )


def _parse_led_config(data: bytes) -> LedConfig:
    """Parse LedConfig packet (0x21, 22 bytes)."""
    if len(data) < 22:
        raise ConfigParseError(f"LedConfig too short: {len(data)} bytes (need 22)")

    instance_num, led_type, led_1, led_2, led_3, led_4, led_flags = struct.unpack_from(
        "<BBBBBBB", data, 0
    )
    reserved = data[7:22]

    return LedConfig(
        instance_number=instance_num,
        led_type=led_type,
        led_1_r=led_1,
        led_2_g=led_2,
        led_3_b=led_3,
        led_4=led_4,
        led_flags=led_flags,
        reserved=reserved,
    )


def _parse_sensor_data(data: bytes) -> SensorData:
    """Parse SensorData packet (0x23, 30 bytes)."""
    if len(data) < 30:
        raise ConfigParseError(f"SensorData too short: {len(data)} bytes (need 30)")

    instance_num, sensor_type, bus_id = struct.unpack_from("<BHB", data, 0)
    reserved = data[4:30]

    return SensorData(
        instance_number=instance_num,
        sensor_type=sensor_type,
        bus_id=bus_id,
        reserved=reserved,
    )


def _parse_data_bus(data: bytes) -> DataBus:
    """Parse DataBus packet (0x24, 30 bytes)."""
    if len(data) < 30:
        raise ConfigParseError(f"DataBus too short: {len(data)} bytes (need 30)")

    (
        instance_num,
        bus_type,
        pin_1,
        pin_2,
        pin_3,
        pin_4,
        pin_5,
        pin_6,
        pin_7,
        bus_speed_hz,
        bus_flags,
        pullups,
        pulldowns,
    ) = struct.unpack_from("<BBBBBBBBBIBBB", data, 0)

    reserved = data[16:30]

    return DataBus(
        instance_number=instance_num,
        bus_type=bus_type,
        pin_1=pin_1,
        pin_2=pin_2,
        pin_3=pin_3,
        pin_4=pin_4,
        pin_5=pin_5,
        pin_6=pin_6,
        pin_7=pin_7,
        bus_speed_hz=bus_speed_hz,
        bus_flags=bus_flags,
        pullups=pullups,
        pulldowns=pulldowns,
        reserved=reserved,
    )


def _parse_binary_inputs(data: bytes) -> BinaryInputs:
    """Parse BinaryInputs packet (0x25, 30 bytes)."""
    if len(data) < 30:
        raise ConfigParseError(f"BinaryInputs too short: {len(data)} bytes (need 30)")

    instance_num, input_type, display_as = struct.unpack_from("<BBB", data, 0)
    reserved_pins = data[3:11]  # 8 reserved pin bytes
    input_flags, invert, pullups, pulldowns = struct.unpack_from("<BBBB", data, 11)
    button_data_byte_index = data[15]
    reserved = data[16:30]

    return BinaryInputs(
        instance_number=instance_num,
        input_type=input_type,
        display_as=display_as,
        reserved_pins=reserved_pins,
        input_flags=input_flags,
        invert=invert,
        pullups=pullups,
        pulldowns=pulldowns,
        button_data_byte_index=button_data_byte_index,
        reserved=reserved,
    )


def _parse_wifi_config(data: bytes) -> WifiConfig:
    """Parse WifiConfig packet (0x26, legacy 65 bytes or current 160 bytes)."""
    if len(data) >= WIFI_CONFIG_SIZE:
        return WifiConfig.from_bytes(data[:WIFI_CONFIG_SIZE])

    if len(data) == WIFI_CONFIG_LEGACY_SIZE:
        ssid = data[0:32]
        password = data[32:64]
        encryption_type = data[64]
        return WifiConfig(
            ssid=ssid,
            password=password,
            encryption_type=encryption_type,
            server_url=b"\x00" * 64,
            server_port=2446,
            reserved=b"\x00" * 29,
        )

    raise ConfigParseError(
        f"WifiConfig too short: {len(data)} bytes (need {WIFI_CONFIG_LEGACY_SIZE} or {WIFI_CONFIG_SIZE})"
    )
