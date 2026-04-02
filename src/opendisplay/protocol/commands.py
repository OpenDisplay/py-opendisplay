"""BLE protocol commands for OpenDisplay devices."""

from __future__ import annotations

from enum import IntEnum

from ..models.led_flash import LedFlashConfig


class CommandCode(IntEnum):
    """BLE command codes for OpenDisplay protocol."""

    # Configuration commands
    READ_CONFIG = 0x0040  # Read TLV configuration
    WRITE_CONFIG = 0x0041  # Write TLV configuration (chunked)
    WRITE_CONFIG_CHUNK = 0x0042  # Write config chunk (multi-chunk mode)

    # Firmware commands
    READ_FW_VERSION = 0x0043  # Read firmware version
    REBOOT = 0x000F  # Reboot device

    # Authentication command (firmware with encryption support)
    AUTHENTICATE = 0x0050  # Two-step challenge-response authentication

    # Image upload commands (direct write mode)
    DIRECT_WRITE_START = 0x0070  # Start direct write transfer
    DIRECT_WRITE_DATA = 0x0071  # Send image data chunk
    DIRECT_WRITE_END = 0x0072  # End transfer and trigger display refresh
    LED_ACTIVATE = 0x0073  # Host→device: trigger LED flash pattern (firmware 1.0+)
    DIRECT_WRITE_REFRESH_COMPLETE = (
        0x0073  # Device→host: refresh finished (same code as LED_ACTIVATE, different direction)
    )
    DIRECT_WRITE_REFRESH_TIMEOUT = 0x0074  # Device→host: refresh timed out


# Protocol constants
SERVICE_UUID = "00002446-0000-1000-8000-00805F9B34FB"
MANUFACTURER_ID = 0x2446  # 9286 decimal
RESPONSE_HIGH_BIT_FLAG = 0x8000  # High bit set in response codes indicates ACK

# Chunking constants
CHUNK_SIZE = 230  # Maximum data bytes per chunk (unencrypted)
ENCRYPTED_CHUNK_SIZE = 154  # Maximum data bytes per chunk when session is active
# Encrypted packet: cmd(2)+nonce(16)+len(1)+data(154)+tag(12) = 185 bytes
CONFIG_CHUNK_SIZE = 200  # Maximum config chunk size (verified from firmware)
PIPELINE_CHUNKS = 1  # Wait for ACK after each chunk

# Upload protocol constants
MAX_COMPRESSED_SIZE = 50 * 1024  # 50KB - firmware buffer limit for compressed uploads
MAX_START_PAYLOAD = 200  # Maximum bytes in START command (prevents MTU issues)


def build_read_config_command() -> bytes:
    """Build command to read device TLV configuration.

    Returns:
        Command bytes: 0x0040 (2 bytes, big-endian)
    """
    return CommandCode.READ_CONFIG.to_bytes(2, byteorder="big")


def build_read_fw_version_command() -> bytes:
    """Build command to read firmware version.

    Returns:
        Command bytes: 0x0043 (2 bytes, big-endian)
    """
    return CommandCode.READ_FW_VERSION.to_bytes(2, byteorder="big")


def build_reboot_command() -> bytes:
    """Build command to reboot device.

    The device will perform an immediate system reset and will NOT send
    an ACK response. The BLE connection will drop when the device resets.

    Returns:
        Command bytes: 0x000F (2 bytes, big-endian)
    """
    return CommandCode.REBOOT.to_bytes(2, byteorder="big")


def build_direct_write_start_compressed(uncompressed_size: int, compressed_data: bytes) -> tuple[bytes, bytes]:
    """Build START command for compressed upload with chunking.

    To prevent BLE MTU issues, the START command is limited to MAX_START_PAYLOAD
    bytes. For large compressed payloads, this returns:
    - START command with header + first chunk of compressed data
    - Remaining compressed data (to be sent via DATA chunks)

    Args:
        uncompressed_size: Original uncompressed image size in bytes
        compressed_data: Complete compressed image data

    Returns:
        Tuple of (start_command, remaining_data):
        - start_command: 0x0070 + uncompressed_size (4 bytes) + first chunk
        - remaining_data: Compressed data not included in START (empty if all fits)

    Format of START command:
        [cmd:2][uncompressed_size:4][compressed_data:up to 194 bytes]
        - cmd: 0x0070 (big-endian)
        - uncompressed_size: Original size before compression (little-endian uint32)
        - compressed_data: First chunk of compressed data
    """
    cmd = CommandCode.DIRECT_WRITE_START.to_bytes(2, byteorder="big")
    size = uncompressed_size.to_bytes(4, byteorder="little")

    # Calculate max compressed data that fits in START
    # MAX_START_PAYLOAD = 200 total bytes
    # Header uses: 2 (cmd) + 4 (size) = 6 bytes
    # Remaining for compressed data: 200 - 6 = 194 bytes
    max_data_in_start = MAX_START_PAYLOAD - 6  # 194 bytes

    if len(compressed_data) <= max_data_in_start:
        # All compressed data fits in START command
        return cmd + size + compressed_data, b""
    # Split: first chunk in START, rest returned separately
    first_chunk = compressed_data[:max_data_in_start]
    remaining = compressed_data[max_data_in_start:]
    return cmd + size + first_chunk, remaining


def build_direct_write_start_uncompressed() -> bytes:
    """Build START command for uncompressed upload protocol.

    This protocol sends NO data in START - all data follows via 0x0071 chunks.

    Returns:
        Command bytes: 0x0070 (just the command, no data!)

    Format:
        [cmd:2]
        - cmd: 0x0070 (big-endian)
        - NO size, NO data - everything sent via 0x0071 DATA chunks
    """
    return CommandCode.DIRECT_WRITE_START.to_bytes(2, byteorder="big")


def build_direct_write_data_command(chunk_data: bytes) -> bytes:
    """Build command to send image data chunk.

    Args:
        chunk_data: Image data chunk (max CHUNK_SIZE bytes)

    Returns:
        Command bytes: 0x0071 + chunk_data

    Format:
        [cmd:2][data:230]
        - cmd: 0x0071 (big-endian)
        - data: Image data chunk
    """
    if len(chunk_data) > CHUNK_SIZE:
        raise ValueError(f"Chunk size {len(chunk_data)} exceeds maximum {CHUNK_SIZE}")

    cmd = CommandCode.DIRECT_WRITE_DATA.to_bytes(2, byteorder="big")
    return cmd + chunk_data


def build_direct_write_end_command(refresh_mode: int = 0) -> bytes:
    """Build command to end image transfer and refresh display.

    Args:
        refresh_mode: Display refresh mode
            0 = FULL (default)
            1 = FAST/PARTIAL (if supported)

    Returns:
        Command bytes: 0x0072 + refresh_mode

    Format:
        [cmd:2][refresh:1]
        - cmd: 0x0072 (big-endian)
        - refresh: Refresh mode (0=full, 1=fast)
    """
    cmd = CommandCode.DIRECT_WRITE_END.to_bytes(2, byteorder="big")
    refresh = refresh_mode.to_bytes(1, byteorder="big")
    return cmd + refresh


def build_led_activate_command(
    led_instance: int,
    flash_config: LedFlashConfig,
) -> bytes:
    """Build LED activate command (firmware 1.0+).

    Firmware command format:
    - With config: [cmd:2][instance:1][flash_config:12]

    Args:
        led_instance: LED instance index (0-based)
        flash_config: Typed LED flash config payload

    Returns:
        Command bytes for 0x0073

    Raises:
        TypeError: If flash_config is not a LedFlashConfig instance
        ValueError: If led_instance out of uint8 range
    """
    if not 0 <= led_instance <= 0xFF:
        raise ValueError(f"LED instance out of range: {led_instance} (must be 0-255)")

    cmd = CommandCode.LED_ACTIVATE.to_bytes(2, byteorder="big")
    payload = bytes([led_instance])

    if not isinstance(flash_config, LedFlashConfig):
        raise TypeError(
            "flash_config must be LedFlashConfig (use LedFlashConfig.from_bytes(...) if you have raw bytes)"
        )

    return cmd + payload + flash_config.to_bytes()


def build_write_config_command(config_data: bytes) -> tuple[bytes, list[bytes]]:
    """Build WRITE_CONFIG command with chunking support.

    Protocol:
    - Single chunk (≤200 bytes): [0x00][0x41][config_data]
    - Multi-chunk (>200 bytes):
      - First: [0x00][0x41][total_size:2LE][first_198_bytes]
      - Rest: [0x00][0x42][chunk_data] (up to 200 bytes each)

    Args:
        config_data: Complete serialized config data

    Returns:
        Tuple of (first_command, remaining_chunks):
        - first_command: 0x0041 command with first chunk
        - remaining_chunks: List of 0x0042 commands for subsequent chunks

    Example:
        # Small config (≤200 bytes)
        first_cmd, chunks = build_write_config_command(small_config)
        # first_cmd: [0x00][0x41][config_data]
        # chunks: []

        # Large config (>200 bytes)
        first_cmd, chunks = build_write_config_command(large_config)
        # first_cmd: [0x00][0x41][total_size:2LE][first_198_bytes]
        # chunks: [[0x00][0x42][chunk_data], ...]
    """
    cmd_write = CommandCode.WRITE_CONFIG.to_bytes(2, byteorder="big")
    cmd_chunk = CommandCode.WRITE_CONFIG_CHUNK.to_bytes(2, byteorder="big")

    config_len = len(config_data)

    # Single chunk mode (≤200 bytes)
    if config_len <= CONFIG_CHUNK_SIZE:
        return cmd_write + config_data, []

    # Multi-chunk mode (>200 bytes)
    # First chunk: [cmd][total_size:2LE][first_198_bytes]
    total_size = config_len.to_bytes(2, byteorder="little")
    first_chunk_data_size = CONFIG_CHUNK_SIZE - 2  # 198 bytes
    first_chunk = cmd_write + total_size + config_data[:first_chunk_data_size]

    # Remaining chunks: [cmd][chunk_data] (up to 200 bytes each)
    remaining_data = config_data[first_chunk_data_size:]
    chunks = []

    while remaining_data:
        chunk_data = remaining_data[:CONFIG_CHUNK_SIZE]
        chunks.append(cmd_chunk + chunk_data)
        remaining_data = remaining_data[CONFIG_CHUNK_SIZE:]

    return first_chunk, chunks


def build_authenticate_step1() -> bytes:
    """Build step-1 auth command: request a server nonce.

    Returns:
        Command bytes: [0x0050][0x00]
    """
    cmd = CommandCode.AUTHENTICATE.to_bytes(2, byteorder="big")
    return cmd + b"\x00"


def build_authenticate_step2(client_nonce: bytes, challenge_response: bytes) -> bytes:
    """Build step-2 auth command: prove knowledge of the master key.

    Args:
        client_nonce: 16 random bytes generated by the client
        challenge_response: AES-CMAC(master_key, server_nonce || client_nonce || device_id)

    Returns:
        Command bytes: [0x0050][client_nonce:16][challenge_response:16]
    """
    if len(client_nonce) != 16:
        raise ValueError(f"client_nonce must be 16 bytes, got {len(client_nonce)}")
    if len(challenge_response) != 16:
        raise ValueError(f"challenge_response must be 16 bytes, got {len(challenge_response)}")

    cmd = CommandCode.AUTHENTICATE.to_bytes(2, byteorder="big")
    return cmd + client_nonce + challenge_response
