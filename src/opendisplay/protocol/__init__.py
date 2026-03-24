"""BLE protocol implementation."""

from .commands import (
    CHUNK_SIZE,
    MANUFACTURER_ID,
    MAX_COMPRESSED_SIZE,
    PIPELINE_CHUNKS,
    SERVICE_UUID,
    CommandCode,
    build_authenticate_step1,
    build_authenticate_step2,
    build_direct_write_data_command,
    build_direct_write_end_command,
    build_direct_write_start_compressed,
    build_direct_write_start_uncompressed,
    build_led_activate_command,
    build_read_config_command,
    build_read_fw_version_command,
    build_reboot_command,
    build_write_config_command,
)
from .config_parser import parse_config_response
from .config_serializer import (
    calculate_config_crc,
    serialize_config,
)
from .responses import (
    parse_firmware_version,
    validate_ack_response,
)

__all__ = [
    "CommandCode",
    "build_authenticate_step1",
    "build_authenticate_step2",
    "SERVICE_UUID",
    "MANUFACTURER_ID",
    "CHUNK_SIZE",
    "PIPELINE_CHUNKS",
    "MAX_COMPRESSED_SIZE",
    "build_read_config_command",
    "build_read_fw_version_command",
    "build_reboot_command",
    "build_write_config_command",
    "build_direct_write_start_compressed",
    "build_direct_write_start_uncompressed",
    "build_direct_write_data_command",
    "build_direct_write_end_command",
    "build_led_activate_command",
    "parse_config_response",
    "serialize_config",
    "calculate_config_crc",
    "validate_ack_response",
    "parse_firmware_version",
]
