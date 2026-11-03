"""TLV configuration parser for OpenDisplay devices."""

from __future__ import annotations

import logging
import struct

from ..exceptions import ConfigParseError
from ..models.config import (
    BinaryInputs,
    DataBus,
    DataExtended,
    DisplayCalibrationInfo,
    DisplayConfig,
    DisplayLutInfo,
    DisplayModeInfo,
    DisplayPanelInfo,
    FlashConfig,
    GlobalConfig,
    LedConfig,
    ManufacturerData,
    NfcConfig,
    PassiveBuzzer,
    PowerOption,
    SecurityConfig,
    SensorData,
    SystemConfig,
    TouchController,
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
PACKET_TYPE_SECURITY_CONFIG = 0x27
PACKET_TYPE_TOUCH_CONTROLLER = 0x28
PACKET_TYPE_PASSIVE_BUZZER = 0x29
PACKET_TYPE_NFC_CONFIG = 0x2A
PACKET_TYPE_FLASH_CONFIG = 0x2B
PACKET_TYPE_DATA_EXTENDED = 0x2C
# 0x2D findmy_config: the LAST legacy-range id (the range is frozen at it).
# Carried as opaque bytes -- see GlobalConfig.findmy_raw.
PACKET_TYPE_FINDMY = 0x2D
# Panel drive data lives in the LENGTH-PREFIXED id range (structs 2.2): on the
# wire each packet is [number][id][length:2 LE][body], so this parser can SKIP
# any 0x80+ id it does not know instead of abandoning the tail.
PACKET_TYPE_DISPLAY_MODE = 0xAE
PACKET_TYPE_DISPLAY_LUT = 0xAF
PACKET_TYPE_DISPLAY_CALIBRATION = 0xB0
PACKET_TYPE_DISPLAY_PANEL = 0xB1
# Ids 0x2E-0x31 were burned by pre-release lab builds carrying FIXED-SIZE
# drafts of the drive packets. Stepped over by size and NOT modelled: dropping
# them on a read-modify-write is deliberate cleanup -- current firmware ignores
# them too, and re-emitting them would only waste config space.
_BURNED_SIZES = {0x2E: 157, 0x2F: 541, 0x30: 116, 0x31: 36}

WIFI_CONFIG_SIZE = 160
WIFI_CONFIG_LEGACY_SIZE = 65
SECURITY_CONFIG_SIZE = 64


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


def _split_packets(
    data: bytes,
) -> tuple[dict[tuple[int, int], bytes], list[tuple[int, int, bytes]], bytes]:
    """Walk the TLV body into per-packet byte slices.

    Legacy ids (< 0x80) are fixed-size: an unknown one stops the walk and the
    remainder lands in unparsed_tail. Ids 0x80+ carry [length:2 LE] and are
    skippable by design: unknown ones are collected (number, type, body) for
    verbatim re-emission. The burned Rev 1 drive ids (0x2E-0x31) are stepped
    over by their known sizes and deliberately dropped.

    Returns:
        (packets keyed by (type, number), unknown 0x80+ packets, unparsed tail)
    """
    offset = 0
    packets: dict[tuple[int, int], bytes] = {}
    unparsed_tail = b""
    unknown_ex_packets: list[tuple[int, int, bytes]] = []

    while offset < len(data) - 1:
        packet_number = data[offset]
        packet_type = data[offset + 1]
        offset += 2

        # LENGTH-PREFIXED range: skippable by design, known or not.
        if packet_type >= 0x80:
            if offset + 2 > len(data):
                raise ConfigParseError(f"Packet 0x{packet_type:02x}: truncated length header")
            ex_len = int.from_bytes(data[offset : offset + 2], "little")
            offset += 2
            if offset + ex_len > len(data):
                raise ConfigParseError(f"Packet 0x{packet_type:02x}: body {ex_len} bytes runs past the blob end")
            body = data[offset : offset + ex_len]
            offset += ex_len
            if packet_type in (
                PACKET_TYPE_DISPLAY_MODE,
                PACKET_TYPE_DISPLAY_LUT,
                PACKET_TYPE_DISPLAY_CALIBRATION,
                PACKET_TYPE_DISPLAY_PANEL,
            ):
                packets[(packet_type, packet_number)] = body
            else:
                unknown_ex_packets.append((packet_number, packet_type, bytes(body)))
                _LOGGER.debug(
                    "Skipped unknown length-prefixed packet 0x%02x (%d bytes); will re-emit verbatim",
                    packet_type,
                    ex_len,
                )
            continue

        # Burned pre-release drive packet ids: step over, do not model.
        if packet_type in _BURNED_SIZES:
            burned = _BURNED_SIZES[packet_type]
            if offset + burned > len(data):
                raise ConfigParseError(f"Burned packet 0x{packet_type:02x} truncated: need {burned} bytes")
            offset += burned
            _LOGGER.info(
                "Dropping burned pre-release drive packet 0x%02x (%d bytes); rebuild the drive data as 0xAE-0xB1",
                packet_type,
                burned,
            )
            continue

        # Legacy range: size implied by the type.
        packet_size = _get_packet_size(packet_type)
        if packet_size is None:
            unparsed_tail = data[offset - 2 :]
            _LOGGER.warning(
                "Unknown packet type 0x%02x at offset %d; keeping the remaining %d bytes unparsed",
                packet_type,
                offset - 2,
                len(unparsed_tail),
            )
            break

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
                    f"Packet type 0x{packet_type:02x} truncated: need {packet_size} bytes, have {remaining}"
                )

        packets[(packet_type, packet_number)] = data[offset : offset + packet_size]
        offset += packet_size

    return packets, unknown_ex_packets, unparsed_tail


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

    packets, unknown_ex_packets, unparsed_tail = _split_packets(data)

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
    security_config = None
    touch_controllers = []
    buzzers = []
    nfc_configs = []
    flash_configs = []
    data_extended = None
    findmy_raw = b""
    display_modes = []
    display_luts = []
    display_panels = []
    display_calibrations = []

    for (packet_type, _), packet_data in packets.items():
        if packet_type == PACKET_TYPE_SYSTEM:
            system = _parse_system_config(packet_data)
        elif packet_type == PACKET_TYPE_MANUFACTURER:
            manufacturer = _parse_manufacturer_data(packet_data)
        elif packet_type == PACKET_TYPE_POWER:
            power = _parse_power_option(packet_data)
        elif packet_type == PACKET_TYPE_DISPLAY:
            displays.append(_parse_display_config(packet_data))
        elif packet_type == PACKET_TYPE_LED:
            leds.append(_parse_led_config(packet_data))
        elif packet_type == PACKET_TYPE_SENSOR:
            sensors.append(_parse_sensor_data(packet_data))
        elif packet_type == PACKET_TYPE_DATABUS:
            data_buses.append(_parse_data_bus(packet_data))
        elif packet_type == PACKET_TYPE_BINARY_INPUT:
            binary_inputs.append(_parse_binary_inputs(packet_data))
        elif packet_type == PACKET_TYPE_WIFI_CONFIG:
            wifi_config = _parse_wifi_config(packet_data)
        elif packet_type == PACKET_TYPE_SECURITY_CONFIG:
            security_config = SecurityConfig.from_bytes(packet_data)
        elif packet_type == PACKET_TYPE_TOUCH_CONTROLLER:
            touch_controllers.append(TouchController.from_bytes(packet_data))
        elif packet_type == PACKET_TYPE_PASSIVE_BUZZER:
            buzzers.append(PassiveBuzzer.from_bytes(packet_data))
        elif packet_type == PACKET_TYPE_NFC_CONFIG:
            nfc_configs.append(NfcConfig.from_bytes(packet_data))
        elif packet_type == PACKET_TYPE_FLASH_CONFIG:
            flash_configs.append(FlashConfig.from_bytes(packet_data))
        elif packet_type == PACKET_TYPE_DISPLAY_MODE:
            display_modes.append(_parse_display_mode(packet_data))
        elif packet_type == PACKET_TYPE_DISPLAY_LUT:
            display_luts.append(_parse_display_lut(packet_data))
        elif packet_type == PACKET_TYPE_DISPLAY_PANEL:
            display_panels.append(_parse_display_panel(packet_data))
        elif packet_type == PACKET_TYPE_DISPLAY_CALIBRATION:
            display_calibrations.append(_parse_display_calibration(packet_data))
        elif packet_type == PACKET_TYPE_DATA_EXTENDED:
            data_extended = DataExtended.from_bytes(packet_data)
        elif packet_type == PACKET_TYPE_FINDMY:
            findmy_raw = bytes(packet_data)

    missing_required = [
        name
        for name, present in [
            ("system", system),
            ("manufacturer", manufacturer),
            ("power", power),
            ("display", displays),
        ]
        if not present
    ]
    if missing_required:
        raise ConfigParseError("Missing required packet(s): " + ", ".join(missing_required))

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
        security_config=security_config,
        touch_controllers=touch_controllers,
        buzzers=buzzers,
        nfc_configs=nfc_configs,
        flash_configs=flash_configs,
        data_extended=data_extended,
        findmy_raw=findmy_raw,
        unknown_ex_packets=unknown_ex_packets,
        display_modes=display_modes,
        display_luts=display_luts,
        display_panels=display_panels,
        display_calibrations=display_calibrations,
        unparsed_tail=unparsed_tail,
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
        PACKET_TYPE_SECURITY_CONFIG: SECURITY_CONFIG_SIZE,
        PACKET_TYPE_TOUCH_CONTROLLER: 32,
        PACKET_TYPE_PASSIVE_BUZZER: 32,
        PACKET_TYPE_NFC_CONFIG: 32,
        PACKET_TYPE_FLASH_CONFIG: 32,
        PACKET_TYPE_DATA_EXTENDED: DataExtended.SIZE,
        PACKET_TYPE_FINDMY: 128,
    }
    return sizes.get(packet_type)


def _parse_display_mode(data: bytes) -> DisplayModeInfo:
    """0xAE: 37-byte head + seq[seq_len] tail. Mirrors DisplayMode.serialize()."""
    if len(data) < 37:
        raise ConfigParseError(f"display_mode body too short: {len(data)} bytes")
    seq_len = int.from_bytes(data[35:37], "little")
    if 37 + seq_len > len(data):
        raise ConfigParseError("display_mode seq_len runs past the packet body")
    return DisplayModeInfo(
        instance_number=data[0],
        mode_index=data[1],
        name=data[2:14].rstrip(b"\x00").decode("utf-8", "replace"),
        color_scheme=data[14],
        passes=data[15],
        temp_min_c=struct.unpack_from("<b", data, 16)[0],
        temp_max_c=struct.unpack_from("<b", data, 17)[0],
        temp_policy=data[18],
        lut_slot=list(data[19:27]),
        vsn1=list(data[27:35]),
        init_seq=bytes(data[37 : 37 + seq_len]),
    )


def _parse_display_lut(data: bytes) -> DisplayLutInfo:
    """0xAF: 4-byte head + the waveform at its real size (length = body - 4)."""
    if len(data) < 5:
        raise ConfigParseError(f"display_lut body too short: {len(data)} bytes")
    inst, slot, crc = struct.unpack_from("<BBH", data, 0)
    wave = bytes(data[4:])
    return DisplayLutInfo(
        instance_number=inst,
        lut_slot=slot,
        lut_len=len(wave),
        crc16=crc,
        data=wave,
    )


def _parse_display_panel(data: bytes) -> DisplayPanelInfo:
    """0xB1: 13-byte head + sleep_seq[sleep_seq_len] tail."""
    if len(data) < 13:
        raise ConfigParseError(f"display_panel body too short: {len(data)} bytes")
    inst, flags, idle, timeout, fixed = struct.unpack_from("<BBBHH", data, 0)
    sleep_len = data[12]
    return DisplayPanelInfo(
        instance_number=inst,
        busy_usable=bool(flags & 0x01),
        lut_supported=bool(flags & 0x02),
        busy_idle_level=idle,
        busy_timeout_ms=timeout,
        busy_fixed_delay_ms=fixed,
        code_map=list(data[7:11]),
        mode_count=data[11],
        sleep_seq=bytes(data[13 : 13 + min(sleep_len, 16)]),
    )


def _parse_display_calibration(data: bytes) -> DisplayCalibrationInfo:
    """0xB0: 4-byte head + entry_count x 8-byte entries.

    An entry is codes:u16 LE (2-bit panel code of pass p in bits [2p+1:2p],
    LSB-first -- NOT the Rev 1 one-byte MSB-first packing) + CIELAB x100 as
    3 x int16 LE. The tuple length is capped at 8 passes; trailing zero code
    bits are not distinguishable from "pass uses code 0", so the consumer
    slices tuples by the mode's pass count.
    """
    if len(data) < 4:
        raise ConfigParseError(f"display_calibration body too short: {len(data)} bytes")
    inst, mode_index, flags, count = struct.unpack_from("<BBBB", data, 0)
    n = min(count, (len(data) - 4) // 8)
    codes = []
    lab = []
    for i in range(n):
        cw, l_, a_, b_ = struct.unpack_from("<Hhhh", data, 4 + i * 8)
        codes.append(tuple((cw >> (2 * p)) & 3 for p in range(8)))
        lab.append((l_ / 100.0, a_ / 100.0, b_ / 100.0))
    return DisplayCalibrationInfo(
        instance_number=inst,
        mode_index=mode_index,
        entry_count=count,
        measured=bool(flags & 0x01),
        lab=lab,
        codes=codes,
    )


def _parse_system_config(data: bytes) -> SystemConfig:
    """Parse SystemConfig packet (0x01, 22 bytes)."""
    if len(data) < 22:
        raise ConfigParseError(f"SystemConfig too short: {len(data)} bytes (need 22)")

    ic_type, comm_modes, dev_flags, pwr_pin = struct.unpack_from("<HBBB", data, 0)
    reserved = data[5:20]
    pwr_pin_2 = data[20]
    pwr_pin_3 = data[21]

    return SystemConfig(
        ic_type=ic_type,
        communication_modes=comm_modes,
        device_flags=dev_flags,
        pwr_pin=pwr_pin,
        reserved=reserved,
        pwr_pin_2=pwr_pin_2,
        pwr_pin_3=pwr_pin_3,
    )


def _parse_manufacturer_data(data: bytes) -> ManufacturerData:
    """Parse ManufacturerData packet (0x02, 22 bytes)."""
    if len(data) < 22:
        raise ConfigParseError(f"ManufacturerData too short: {len(data)} bytes (need 22)")

    # Delegate to the model so the simple-config fields (driver/display/power
    # index + configured_at at offsets 4-15) and the true 6-byte reserved
    # (offsets 16-21) are parsed once. Storing data[4:22] into reserved here
    # would drop that metadata and corrupt it on a read-modify-write.
    return ManufacturerData.from_bytes(data)


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
    ) = struct.unpack_from("<HBBBBBBHIH", data, 4)  # tx_power is uint8, not int8

    (
        charge_enable_pin,
        charge_state_pin,
        charger_flags,
        min_wake_time_seconds,
        screen_timeout_seconds,
    ) = struct.unpack_from("<BBBHB", data, 20)  # named fields at offsets 20-25

    reserved = data[26:30]  # 4 reserved bytes (offsets 26-29)

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
        charge_enable_pin=charge_enable_pin,
        charge_state_pin=charge_state_pin,
        charger_flags=charger_flags,
        min_wake_time_seconds=min_wake_time_seconds,
        screen_timeout_seconds=screen_timeout_seconds,
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
    full_update_mC = int.from_bytes(data[31:33], "little") if len(data) >= 33 else 0
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

    instance_num, led_type, led_1, led_2, led_3, led_4, led_flags = struct.unpack_from("<BBBBBBB", data, 0)
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
    i2c_addr_7bit = data[4]
    msd_data_start_byte = data[5]
    reserved = data[6:30]

    return SensorData(
        instance_number=instance_num,
        sensor_type=sensor_type,
        bus_id=bus_id,
        i2c_addr_7bit=i2c_addr_7bit,
        msd_data_start_byte=msd_data_start_byte,
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
    """Parse BinaryInputs packet (0x25, 30 bytes) using the canonical field order."""
    if len(data) < 30:
        raise ConfigParseError(f"BinaryInputs too short: {len(data)} bytes (need 30)")

    instance_num, input_type, display_as = struct.unpack_from("<BBB", data, 0)
    reserved_pins = data[3:11]  # 8 reserved pin bytes
    input_flags, invert, pullups, pulldowns = struct.unpack_from("<BBBB", data, 11)
    button_data_byte_index = data[15]
    power_off_flags = data[16]
    power_off_hold_sec = data[17]
    reserved = data[18:30]

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
        power_off_flags=power_off_flags,
        power_off_hold_sec=power_off_hold_sec,
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
