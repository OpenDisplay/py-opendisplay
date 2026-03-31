"""JSON serialization/deserialization for device configuration.

Compatible with the Open Display Config Builder web tool format.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import (
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


def _parse_int(value: str | int) -> int:
    """Parse integer from string (handles "0x" hex or decimal).

    Args:
        value: String or int value to parse

    Returns:
        Integer value
    """
    if isinstance(value, int):
        return value
    value = str(value).strip()
    if value.startswith("0x") or value.startswith("0X"):
        return int(value, 16)
    return int(value)


def config_to_json(config: GlobalConfig) -> dict[str, Any]:
    """Export GlobalConfig to JSON-serializable dict.

    Format matches the Open Display Config Builder web tool exactly:
    - Packet IDs are decimal strings ("1", "32")
    - Field values are mixed format (decimal strings or hex strings)
    - All fields included, including reserved fields

    Args:
        config: GlobalConfig to export

    Returns:
        Dict with version, packets array, and metadata
    """
    packets = []

    # System config (packet type 0x01 = 1)
    sys = config.system
    packets.append(
        {
            "id": "1",  # Decimal string
            "name": "system_config",
            "fields": {
                "ic_type": str(sys.ic_type),
                "communication_modes": f"0x{sys.communication_modes:x}",
                "device_flags": f"0x{sys.device_flags:x}",
                "pwr_pin": f"0x{sys.pwr_pin:02x}",
                "pwr_pin_2": f"0x{sys.pwr_pin_2:02x}",
                "pwr_pin_3": f"0x{sys.pwr_pin_3:02x}",
                "reserved": "0x0",
            },
        }
    )

    # Manufacturer data (packet type 0x02 = 2)
    mfr = config.manufacturer
    packets.append(
        {
            "id": "2",  # Decimal string
            "name": "manufacturer_data",
            "fields": {
                "manufacturer_id": str(mfr.manufacturer_id),
                "board_type": str(mfr.board_type),
                "board_revision": f"0x{mfr.board_revision:x}",
                "reserved": "0x0",
            },
        }
    )

    # Power option (packet type 0x04 = 4)
    pwr = config.power

    battery_capacity = int.from_bytes(pwr.battery_capacity_mah[:3], "little")

    packets.append(
        {
            "id": "4",  # Decimal string
            "name": "power_option",
            "fields": {
                "power_mode": str(pwr.power_mode),
                "battery_capacity_mah": f"0x{battery_capacity:x}",
                "sleep_timeout_ms": f"0x{pwr.sleep_timeout_ms:x}",
                "tx_power": f"0x{pwr.tx_power:x}",
                "sleep_flags": f"0x{pwr.sleep_flags:x}",
                "battery_sense_pin": f"0x{pwr.battery_sense_pin:x}",
                "battery_sense_enable_pin": f"0x{pwr.battery_sense_enable_pin:x}",
                "battery_sense_flags": f"0x{pwr.battery_sense_flags:x}",
                "capacity_estimator": str(pwr.capacity_estimator),
                "voltage_scaling_factor": f"0x{pwr.voltage_scaling_factor:x}",
                "deep_sleep_current_ua": f"0x{pwr.deep_sleep_current_ua:x}",
                "deep_sleep_time_seconds": f"0x{pwr.deep_sleep_time_seconds:x}",
                "reserved": "0x0",
            },
        }
    )

    # Display configs (packet type 0x20 = 32)
    for display in config.displays:
        packets.append(
            {
                "id": "32",  # Decimal string
                "name": "display",
                "fields": {
                    "instance_number": f"0x{display.instance_number:x}",
                    "display_technology": str(display.display_technology),
                    "panel_ic_type": str(display.panel_ic_type),
                    "pixel_width": f"0x{display.pixel_width:x}",
                    "pixel_height": f"0x{display.pixel_height:x}",
                    "active_width_mm": f"0x{display.active_width_mm:x}",
                    "active_height_mm": f"0x{display.active_height_mm:x}",
                    "legacy_tagtype": f"0x{display.tag_type:x}",
                    "rotation": str(display.rotation),
                    "reset_pin": f"0x{display.reset_pin:x}",
                    "busy_pin": f"0x{display.busy_pin:x}",
                    "dc_pin": f"0x{display.dc_pin:x}",
                    "cs_pin": f"0x{display.cs_pin:02x}",
                    "data_pin": f"0x{display.data_pin:x}",
                    "partial_update_support": str(display.partial_update_support),
                    "color_scheme": str(display.color_scheme),
                    "transmission_modes": f"0x{display.transmission_modes:x}",
                    "clk_pin": f"0x{display.clk_pin:x}",
                    "reserved_pin_2": "0x0",
                    "reserved_pin_3": "0x0",
                    "reserved_pin_4": "0x0",
                    "reserved_pin_5": "0x0",
                    "reserved_pin_6": "0x0",
                    "reserved_pin_7": "0x0",
                    "reserved_pin_8": "0x0",
                    "full_update_mC": "0x0",
                    "reserved": "0x0",
                },
            }
        )

    # LED configs (packet type 0x21 = 33)
    for led in config.leds:
        packets.append(
            {
                "id": "33",  # Decimal string
                "name": "led",
                "fields": {
                    "instance_number": f"0x{led.instance_number:x}",
                    "led_type": str(led.led_type),
                    "led_1_r": f"0x{led.led_1_r:x}",
                    "led_2_g": f"0x{led.led_2_g:x}",
                    "led_3_b": f"0x{led.led_3_b:x}",
                    "led_4": f"0x{led.led_4:x}",
                    "led_flags": f"0x{led.led_flags:x}",
                    "reserved": "0x0",
                },
            }
        )

    # Sensor configs (packet type 0x23 = 35)
    for sensor in config.sensors:
        packets.append(
            {
                "id": "35",  # Decimal string
                "name": "sensor",
                "fields": {
                    "instance_number": f"0x{sensor.instance_number:x}",
                    "sensor_type": str(sensor.sensor_type),
                    "bus_id": f"0x{sensor.bus_id:x}",
                    "i2c_addr_7bit": f"0x{sensor.i2c_addr_7bit:02x}",
                    "msd_data_start_byte": f"0x{sensor.msd_data_start_byte:x}",
                    "reserved": "0x0",
                },
            }
        )

    # DataBus configs (packet type 0x24 = 36)
    for bus in config.data_buses:
        packets.append(
            {
                "id": "36",  # Decimal string
                "name": "databus",
                "fields": {
                    "instance_number": f"0x{bus.instance_number:x}",
                    "bus_type": str(bus.bus_type),
                    "pin_1": f"0x{bus.pin_1:x}",
                    "pin_2": f"0x{bus.pin_2:x}",
                    "pin_3": f"0x{bus.pin_3:x}",
                    "pin_4": f"0x{bus.pin_4:x}",
                    "pin_5": f"0x{bus.pin_5:x}",
                    "pin_6": f"0x{bus.pin_6:x}",
                    "pin_7": f"0x{bus.pin_7:x}",
                    "bus_speed_hz": f"0x{bus.bus_speed_hz:x}",
                    "bus_flags": f"0x{bus.bus_flags:x}",
                    "pullups": f"0x{bus.pullups:x}",
                    "pulldowns": f"0x{bus.pulldowns:x}",
                    "reserved": "0x0",
                },
            }
        )

    # BinaryInput configs (packet type 0x25 = 37)
    for binary_input in config.binary_inputs:
        packets.append(
            {
                "id": "37",  # Decimal string
                "name": "binary_input",
                "fields": {
                    "instance_number": f"0x{binary_input.instance_number:x}",
                    "input_type": str(binary_input.input_type),
                    "display_as": str(binary_input.display_as),
                    "input_flags": f"0x{binary_input.input_flags:x}",
                    "invert": f"0x{binary_input.invert:x}",
                    "pullups": f"0x{binary_input.pullups:x}",
                    "pulldowns": f"0x{binary_input.pulldowns:x}",
                    "button_data_byte_index": f"0x{binary_input.button_data_byte_index:x}",
                    "reserved": "0x0",
                },
            }
        )

    # WiFi config (packet type 0x26 = 38)
    if config.wifi_config is not None:
        wifi = config.wifi_config
        packets.append(
            {
                "id": "38",  # Decimal string
                "name": "wifi_config",
                "fields": {
                    "ssid": wifi.ssid_text,
                    "password": wifi.password_text,
                    "encryption_type": str(wifi.encryption_type),
                    "server_url": wifi.server_url_text,
                    "server_port": f"0x{wifi.server_port:x}",
                    "reserved": "0x0",
                },
            }
        )

    # Security config (packet type 0x27 = 39)
    if config.security_config is not None:
        sec = config.security_config
        packets.append(
            {
                "id": "39",
                "name": "security_config",
                "fields": {
                    "encryption_enabled": str(sec.encryption_enabled),
                    "encryption_key": sec.encryption_key.hex(),
                    "session_timeout_seconds": str(sec.session_timeout_seconds),
                    "flags": f"0x{sec.flags:02x}",
                    "reset_pin": f"0x{sec.reset_pin:02x}",
                    "reserved": "0x0",
                },
            }
        )

    # Touch controller configs (packet type 0x28 = 40)
    for tc in config.touch_controllers:
        packets.append(
            {
                "id": "40",
                "name": "touch_controller",
                "fields": {
                    "instance_number": f"0x{tc.instance_number:x}",
                    "touch_ic_type": str(tc.touch_ic_type),
                    "bus_id": f"0x{tc.bus_id:02x}",
                    "i2c_addr_7bit": f"0x{tc.i2c_addr_7bit:02x}",
                    "int_pin": f"0x{tc.int_pin:02x}",
                    "rst_pin": f"0x{tc.rst_pin:02x}",
                    "display_instance": f"0x{tc.display_instance:x}",
                    "flags": f"0x{tc.flags:02x}",
                    "poll_interval_ms": str(tc.poll_interval_ms),
                    "touch_data_start_byte": str(tc.touch_data_start_byte),
                    "reserved": "0x0",
                },
            }
        )

    # Passive buzzer configs (packet type 0x29 = 41)
    for bz in config.buzzers:
        packets.append(
            {
                "id": "41",
                "name": "passive_buzzer",
                "fields": {
                    "instance_number": f"0x{bz.instance_number:x}",
                    "drive_pin": f"0x{bz.drive_pin:x}",
                    "enable_pin": f"0x{bz.enable_pin:02x}",
                    "flags": f"0x{bz.flags:02x}",
                    "duty_percent": str(bz.duty_percent),
                    "reserved": "0x0",
                },
            }
        )

    return {
        "version": config.version,
        "minor_version": 1,  # JSON format version (not stored in device)
        "packets": packets,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "exported_by": "py-opendisplay",
    }


def config_from_json(data: dict[str, Any]) -> GlobalConfig:
    """Import GlobalConfig from JSON dict.

    Parses JSON format from the Open Display Config Builder web tool.

    Args:
        data: JSON data with version and packets

    Returns:
        GlobalConfig instance

    Raises:
        ValueError: If required packets (including at least one display) are missing
            or packet structure is invalid
    """
    system: SystemConfig | None = None
    manufacturer: ManufacturerData | None = None
    power: PowerOption | None = None
    displays: list[DisplayConfig] = []
    leds: list[LedConfig] = []
    sensors: list[SensorData] = []
    data_buses: list[DataBus] = []
    binary_inputs: list[BinaryInputs] = []
    wifi_config: WifiConfig | None = None
    security_config: SecurityConfig | None = None
    touch_controllers: list[TouchController] = []
    buzzers: list[PassiveBuzzer] = []

    version = data.get("version", 1)
    minor_version = data.get("minor_version", 0)

    for packet in data.get("packets", []):
        packet_id = int(packet.get("id"))  # Parse decimal string ID
        fields = packet.get("fields", {})

        if packet_id == 1:  # 0x01 = system_config
            system = SystemConfig(
                ic_type=_parse_int(fields.get("ic_type", "0")),
                communication_modes=_parse_int(fields.get("communication_modes", "0")),
                device_flags=_parse_int(fields.get("device_flags", "0")),
                pwr_pin=_parse_int(fields.get("pwr_pin", "0xff")),
                reserved=bytes(15),  # Fixed size
                pwr_pin_2=_parse_int(fields.get("pwr_pin_2", "0xff")),
                pwr_pin_3=_parse_int(fields.get("pwr_pin_3", "0xff")),
            )

        elif packet_id == 2:  # 0x02 = manufacturer_data
            manufacturer = ManufacturerData(
                manufacturer_id=_parse_int(fields.get("manufacturer_id", "0")),
                board_type=_parse_int(fields.get("board_type", "0")),
                board_revision=_parse_int(fields.get("board_revision", "0")),
                reserved=bytes(18),  # Fixed size
            )

        elif packet_id == 4:  # 0x04 = power_option
            power = PowerOption(
                power_mode=_parse_int(fields.get("power_mode", "0")),
                battery_capacity_mah=_parse_int(fields.get("battery_capacity_mah", "0")).to_bytes(3, "little"),
                sleep_timeout_ms=_parse_int(fields.get("sleep_timeout_ms", "0")),
                tx_power=_parse_int(fields.get("tx_power", "0")),
                sleep_flags=_parse_int(fields.get("sleep_flags", "0")),
                battery_sense_pin=_parse_int(fields.get("battery_sense_pin", "0xff")),
                battery_sense_enable_pin=_parse_int(fields.get("battery_sense_enable_pin", "0xff")),
                battery_sense_flags=_parse_int(fields.get("battery_sense_flags", "0")),
                capacity_estimator=_parse_int(fields.get("capacity_estimator", "0")),
                voltage_scaling_factor=_parse_int(fields.get("voltage_scaling_factor", "0")),
                deep_sleep_current_ua=_parse_int(fields.get("deep_sleep_current_ua", "0")),
                deep_sleep_time_seconds=_parse_int(fields.get("deep_sleep_time_seconds", "0")),
                reserved=bytes(10),  # Fixed size
            )

        elif packet_id == 32:  # 0x20 = display
            displays.append(
                DisplayConfig(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    display_technology=_parse_int(fields.get("display_technology", "0")),
                    panel_ic_type=_parse_int(fields.get("panel_ic_type", "0")),
                    pixel_width=_parse_int(fields.get("pixel_width", "0")),
                    pixel_height=_parse_int(fields.get("pixel_height", "0")),
                    active_width_mm=_parse_int(fields.get("active_width_mm", "0")),
                    active_height_mm=_parse_int(fields.get("active_height_mm", "0")),
                    tag_type=_parse_int(fields.get("legacy_tagtype", "0")),
                    rotation=_parse_int(fields.get("rotation", "0")),
                    reset_pin=_parse_int(fields.get("reset_pin", "0xff")),
                    busy_pin=_parse_int(fields.get("busy_pin", "0xff")),
                    dc_pin=_parse_int(fields.get("dc_pin", "0xff")),
                    cs_pin=_parse_int(fields.get("cs_pin", "0xff")),
                    data_pin=_parse_int(fields.get("data_pin", "0xff")),
                    partial_update_support=_parse_int(fields.get("partial_update_support", "0")),
                    color_scheme=_parse_int(fields.get("color_scheme", "0")),
                    transmission_modes=_parse_int(fields.get("transmission_modes", "0")),
                    clk_pin=_parse_int(fields.get("clk_pin", "0xff")),
                    reserved_pins=bytes(7),  # Fixed size
                    full_update_mC=_parse_int(fields.get("full_update_mC", "0")),
                    reserved=bytes(13),  # Fixed size
                )
            )

        elif packet_id == 33:  # 0x21 = led
            leds.append(
                LedConfig(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    led_type=_parse_int(fields.get("led_type", "0")),
                    led_1_r=_parse_int(fields.get("led_1_r", "0")),
                    led_2_g=_parse_int(fields.get("led_2_g", "0")),
                    led_3_b=_parse_int(fields.get("led_3_b", "0")),
                    led_4=_parse_int(fields.get("led_4", "0")),
                    led_flags=_parse_int(fields.get("led_flags", "0")),
                    reserved=bytes(15),  # Fixed size
                )
            )

        elif packet_id == 35:  # 0x23 = sensor
            sensors.append(
                SensorData(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    sensor_type=_parse_int(fields.get("sensor_type", "0")),
                    bus_id=_parse_int(fields.get("bus_id", "0")),
                    i2c_addr_7bit=_parse_int(fields.get("i2c_addr_7bit", "0")),
                    msd_data_start_byte=_parse_int(fields.get("msd_data_start_byte", "0")),
                    reserved=bytes(24),  # Fixed size
                )
            )

        elif packet_id == 36:  # 0x24 = databus
            data_buses.append(
                DataBus(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    bus_type=_parse_int(fields.get("bus_type", "0")),
                    pin_1=_parse_int(fields.get("pin_1", "0xff")),
                    pin_2=_parse_int(fields.get("pin_2", "0xff")),
                    pin_3=_parse_int(fields.get("pin_3", "0xff")),
                    pin_4=_parse_int(fields.get("pin_4", "0xff")),
                    pin_5=_parse_int(fields.get("pin_5", "0xff")),
                    pin_6=_parse_int(fields.get("pin_6", "0xff")),
                    pin_7=_parse_int(fields.get("pin_7", "0xff")),
                    bus_speed_hz=_parse_int(fields.get("bus_speed_hz", "0")),
                    bus_flags=_parse_int(fields.get("bus_flags", "0")),
                    pullups=_parse_int(fields.get("pullups", "0")),
                    pulldowns=_parse_int(fields.get("pulldowns", "0")),
                    reserved=bytes(14),  # Fixed size
                )
            )

        elif packet_id == 37:  # 0x25 = binary_input
            binary_inputs.append(
                BinaryInputs(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    input_type=_parse_int(fields.get("input_type", "0")),
                    display_as=_parse_int(fields.get("display_as", "0")),
                    reserved_pins=bytes(8),  # Fixed size
                    input_flags=_parse_int(fields.get("input_flags", "0")),
                    invert=_parse_int(fields.get("invert", "0")),
                    pullups=_parse_int(fields.get("pullups", "0")),
                    pulldowns=_parse_int(fields.get("pulldowns", "0")),
                    button_data_byte_index=_parse_int(fields.get("button_data_byte_index", "0")),
                    reserved=bytes(14),  # Fixed size
                )
            )

        elif packet_id == 38:  # 0x26 = wifi_config
            wifi_config = WifiConfig.from_strings(
                ssid=str(fields.get("ssid", "")),
                password=str(fields.get("password", "")),
                encryption_type=_parse_int(fields.get("encryption_type", "0")),
                server_url=str(fields.get("server_url", "")),
                server_port=_parse_int(fields.get("server_port", "2446")),
            )

        elif packet_id == 39:  # 0x27 = security_config
            key_hex = fields.get("encryption_key", "")
            try:
                key = bytes.fromhex(key_hex) if key_hex else b"\x00" * 16
            except ValueError:
                key = b"\x00" * 16
            security_config = SecurityConfig(
                encryption_enabled=_parse_int(fields.get("encryption_enabled", "0")),
                encryption_key=key[:16].ljust(16, b"\x00"),
                session_timeout_seconds=_parse_int(fields.get("session_timeout_seconds", "0")),
                flags=_parse_int(fields.get("flags", "0")),
                reset_pin=_parse_int(fields.get("reset_pin", "0xff")),
                reserved=bytes(43),
            )

        elif packet_id == 40:  # 0x28 = touch_controller
            touch_controllers.append(
                TouchController(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    touch_ic_type=_parse_int(fields.get("touch_ic_type", "0")),
                    bus_id=_parse_int(fields.get("bus_id", "0xff")),
                    i2c_addr_7bit=_parse_int(fields.get("i2c_addr_7bit", "0")),
                    int_pin=_parse_int(fields.get("int_pin", "0xff")),
                    rst_pin=_parse_int(fields.get("rst_pin", "0xff")),
                    display_instance=_parse_int(fields.get("display_instance", "0")),
                    flags=_parse_int(fields.get("flags", "0")),
                    poll_interval_ms=_parse_int(fields.get("poll_interval_ms", "0")),
                    touch_data_start_byte=_parse_int(fields.get("touch_data_start_byte", "0")),
                    reserved=bytes(21),
                )
            )

        elif packet_id == 41:  # 0x29 = passive_buzzer
            buzzers.append(
                PassiveBuzzer(
                    instance_number=_parse_int(fields.get("instance_number", "0")),
                    drive_pin=_parse_int(fields.get("drive_pin", "0")),
                    enable_pin=_parse_int(fields.get("enable_pin", "0xff")),
                    flags=_parse_int(fields.get("flags", "0")),
                    duty_percent=_parse_int(fields.get("duty_percent", "0")),
                    reserved=bytes(27),
                )
            )

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
        raise ValueError("Missing required packet(s): " + ", ".join(missing_required))

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
        version=version,
        minor_version=minor_version,
        loaded=True,
    )
