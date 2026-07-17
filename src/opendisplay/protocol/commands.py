"""BLE protocol commands for OpenDisplay devices."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from ..models.buzzer_activate import BuzzerActivateConfig
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
    DIRECT_WRITE_PARTIAL_START = 0x0076  # Start a partial update transfer (stream via 0x71)
    BUZZER_ACTIVATE = 0x0077  # Host→device: trigger buzzer pattern (firmware 1.61+)
    ENTER_DFU = 0x0051  # Trigger DFU bootloader mode (nRF only)
    DEEP_SLEEP = 0x0052  # Enter deep sleep now (ESP32 timer-wake / Silabs EM4; nRF unsupported)

    # Sliding-window image transfer (PIPE_WRITE, firmware 2.x+)
    PIPE_WRITE_START = 0x0080  # Start + negotiate a sliding-window transfer
    PIPE_WRITE_DATA = 0x0081  # Windowed data frame (seq + chunk); also device→host ACK/NACK opcode
    PIPE_WRITE_END = 0x0082  # End sliding-window transfer and trigger display refresh

    # NFC write endpoint (firmware NFC_ENDPOINT)
    NFC_ENDPOINT = 0x0083  # Read/write the NFC tag NDEF record (sub-opcode selects the operation)


# Protocol constants
SERVICE_UUID = "00002446-0000-1000-8000-00805F9B34FB"
MANUFACTURER_ID = 0x2446  # 9286 decimal
RESPONSE_HIGH_BIT_FLAG = 0x8000  # High bit set in response codes indicates ACK

# Chunking constants
CHUNK_SIZE = 230  # Maximum data bytes per chunk (unencrypted)
ENCRYPTED_CHUNK_SIZE = 154  # Maximum data bytes per chunk when session is active
# Encrypted packet: cmd(2)+nonce(16)+len(1)+data(154)+tag(12) = 185 bytes
CONFIG_CHUNK_SIZE = 200  # Maximum config chunk size (verified from firmware)

# Upload protocol constants
MAX_COMPRESSED_SIZE = 50 * 1024  # Standard firmware buffer (nRF, ~50KB)
MAX_START_PAYLOAD = 200  # Maximum bytes in START command (prevents MTU issues)

# Sliding-window PIPE_WRITE (0x0080-0x0082) constants
PIPE_VERSION = 1  # Protocol version carried in the 0x0080 request/response
PIPE_FLAG_COMPRESSED = 0x01  # 0x0080 flags bit0: streamed bytes are zlib-compressed
PIPE_FLAG_PARTIAL = 0x02  # 0x0080 flags bit1: transfer is a partial-region refresh
PIPE_FRAME_OVERHEAD = 3  # Plaintext 0x0081 header: cmd(2) + seq(1)
DEFAULT_MAX_FRAME = 244  # HA native GATT write ceiling (client_max_frame request)
# Seconds to wait for the 0x0080 START response. Pipe attempts are gated on the
# config advertising transmission_modes bit 0x10, so this is a normal command
# timeout, not a fast discovery probe: a gated-in device WILL answer, but on
# ESP32 the ACK is queued and only flushed after panel bring-up returns, which
# can take up to the color-panel busy cap (~30 s worst case). Silence still
# falls back to legacy — it now means a stale config bit (pipe-less firmware),
# paid at most once per connection thanks to the probe cache.
TIMEOUT_PIPE_START = 30.0
MAX_PTO = 3  # Consecutive silent probe timeouts before aborting a pipe transfer

# NFC write endpoint (0x0083) sub-opcodes and size limits
NFC_SUB_READ = 0x00  # Read the current NDEF record (not built here)
NFC_SUB_WRITE_INLINE = 0x01  # Single-shot write: rec_type + len + payload in one packet
NFC_SUB_WRITE_START = 0x10  # Begin a chunked write: rec_type + total_len
NFC_SUB_WRITE_DATA = 0x11  # Chunked write data frame
NFC_SUB_WRITE_END = 0x12  # Commit a chunked write
NFC_INLINE_MAX = 120  # Firmware policy: payloads above this size must use chunked write
NFC_CHUNK_SIZE = 120  # Maximum bytes per NFC_SUB_WRITE_DATA chunk
NFC_WRITE_MAX_TOTAL = 512  # Firmware hard limit on total NDEF payload size


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


def build_enter_dfu_command() -> bytes:
    """Build command to trigger DFU bootloader mode (nRF devices only).

    The device will disconnect BLE, set the Nordic GPREGRET magic byte 0xB1,
    disable the SoftDevice, and jump to the bootloader. No ACK is sent.

    Returns:
        Command bytes: 0x0051 (2 bytes, big-endian)
    """
    return CommandCode.ENTER_DFU.to_bytes(2, byteorder="big")


def build_deep_sleep_command() -> bytes:
    """Build command to put the device into deep sleep (command 0x0052).

    Supported on ESP32 (enters timer-wake deep sleep, or releases the D-FF power
    latch when one is configured) and Silabs Flex (arms EM4 button/NFC wake and
    sleeps once the BLE connection closes). nRF targets do not implement deep
    sleep and only log the command.

    The response behavior varies by target and is best-effort — callers should
    tolerate the connection dropping during or right after the command:
    - ESP32 with a power latch: replies 0x0052, then powers off after ~100 ms.
    - ESP32 without a power latch: enters deep sleep immediately with no ACK;
      the BLE connection drops.
    - Silabs Flex: replies 0x0052, then closes the link and enters EM4.
    - nRF: no response (deep sleep not supported).

    Returns:
        Command bytes: 0x0052 (2 bytes, big-endian)
    """
    return CommandCode.DEEP_SLEEP.to_bytes(2, byteorder="big")


def build_direct_write_start_compressed(
    uncompressed_size: int,
    compressed_data: bytes,
    max_start_payload: int = MAX_START_PAYLOAD,
) -> tuple[bytes, bytes]:
    """Build START command for compressed upload with chunking.

    To prevent BLE MTU issues, the START command plaintext is limited to
    max_start_payload bytes. Pass ENCRYPTED_CHUNK_SIZE when using an encrypted
    session so the plaintext fits within the encrypted packet size limit.

    Args:
        uncompressed_size: Original uncompressed image size in bytes
        compressed_data: Complete compressed image data
        max_start_payload: Max plaintext bytes for the full START command
            (default MAX_START_PAYLOAD=200 for unencrypted;
             pass ENCRYPTED_CHUNK_SIZE=154 when session is active)

    Returns:
        Tuple of (start_command, remaining_data):
        - start_command: 0x0070 + uncompressed_size (4 bytes) + first chunk
        - remaining_data: Compressed data not included in START (empty if all fits)
    """
    cmd = CommandCode.DIRECT_WRITE_START.to_bytes(2, byteorder="big")
    size = uncompressed_size.to_bytes(4, byteorder="little")

    # Header uses: 2 (cmd) + 4 (size) = 6 bytes
    max_data_in_start = max_start_payload - 6

    if len(compressed_data) <= max_data_in_start:
        return cmd + size + compressed_data, b""
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


def build_direct_write_partial_start(
    old_etag: int,
    new_etag: int,
    flags: int,
    x: int,
    y: int,
    width: int,
    height: int,
    stream_bytes: bytes = b"",
    max_start_payload: int = MAX_START_PAYLOAD,
) -> tuple[bytes, bytes]:
    """Build 0x76 partial START packet.

    Fixed payload is 17 bytes; optional initial stream bytes are appended up
    to max_start_payload total packet size (including the 2-byte command). Pass
    ENCRYPTED_CHUNK_SIZE when a session is active so the plaintext fits the
    encrypted packet budget, mirroring build_direct_write_start_compressed.

    Wire fixed payload:
      flags(1) + old_etag(4BE) + new_etag(4BE) + x(2BE) + y(2BE) +
      width(2BE) + height(2BE)

    Returns:
        (start_packet, remaining_stream_bytes) — send start_packet as the
        0x76 command, then remaining_stream_bytes via 0x71 DATA chunks.
    """
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"partial flags out of uint8 range: {flags}")
    if not 0 <= old_etag <= 0xFFFFFFFF:
        raise ValueError(f"old_etag must be uint32, got {old_etag}")
    if not 0 <= new_etag <= 0xFFFFFFFF:
        raise ValueError(f"new_etag must be uint32, got {new_etag}")

    fixed = (
        struct.pack(">B", flags)
        + struct.pack(">I", old_etag)
        + struct.pack(">I", new_etag)
        + struct.pack(">HHHH", x, y, width, height)
    )  # 1+4+4+2+2+2+2 = 17 bytes

    cmd = CommandCode.DIRECT_WRITE_PARTIAL_START.to_bytes(2, byteorder="big")
    max_initial = max_start_payload - 2 - len(fixed)  # e.g. 200 - 2 - 17 = 181 bytes
    initial = stream_bytes[:max_initial]
    remaining = stream_bytes[max_initial:]
    return cmd + fixed + initial, remaining


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


def build_direct_write_end_with_etag(refresh_mode: int, new_etag: int) -> bytes:
    """Build 0x72 END with a new_etag tail. Etag presence is by length only."""
    if not 0 <= new_etag <= 0xFFFFFFFF:
        raise ValueError(f"new_etag out of uint32 range: {new_etag}")
    cmd = CommandCode.DIRECT_WRITE_END.to_bytes(2, byteorder="big")
    return cmd + refresh_mode.to_bytes(1, byteorder="big") + new_etag.to_bytes(4, byteorder="big")


@dataclass(frozen=True)
class PipePartialRequest:
    """Partial-region geometry appended to a PIPE_WRITE_START (0x0080) request.

    All fields ride the wire little-endian, matching the rest of the pipe header
    (NOTE: the legacy 0x76 partial START packs the same geometry big-endian — that
    byte order is deliberately NOT copied here).

    Attributes:
        old_etag: uint32 currently displayed etag (nonzero; must equal the device's
            displayed_etag or the device NACKs 0x05).
        x: Rectangle origin x (uint16, must be a multiple of 8).
        y: Rectangle origin y (uint16).
        w: Rectangle width (uint16, nonzero, must be a multiple of 8).
        h: Rectangle height (uint16, nonzero).
    """

    old_etag: int
    x: int
    y: int
    w: int
    h: int


def build_pipe_write_start_command(
    compressed: bool,
    window: int,
    ack_every: int,
    max_frame: int,
    total_size: int,
    *,
    partial: PipePartialRequest | None = None,
) -> bytes:
    """Build a PIPE_WRITE_START (0x0080) start + negotiation command.

    One round trip carries both the transfer parameters and the negotiation
    request; the device replies with its own maxima (see parse_pipe_start_response).

    Wire (10-byte header, or 22-byte payload when ``partial`` is set):
        [0x00][0x80][ver:1][flags:1][req_window:1][req_ack_every:1]
                    [client_max_frame:2 LE][total_size:4 LE]
        --- appended iff flags bit1 (PIPE_FLAG_PARTIAL) is set ---
                    [old_etag:4 LE][x:2 LE][y:2 LE][w:2 LE][h:2 LE]
        - ver         = PIPE_VERSION (1)
        - flags bit0  = compressed (zlib, window_bits <= 9)
        - flags bit1  = partial-region refresh (0x02); other bits reserved 0
        - req_window  = requested "max queue size" W (tokens in flight), 1..32
        - req_ack_every = requested "blocks per ack" N, 1..32
        - client_max_frame = client MTU-derived frame ceiling (<= 244)
        - total_size  = decompressed panel byte total (partial: plane_size*2)

    The 12-byte partial extension is packed little-endian via ``struct.pack`` to
    match the rest of the pipe header; the legacy 0x76 START packs the same fields
    big-endian and that byte order is intentionally not reused. ``partial=None``
    yields the exact same bytes as before the extension was added.

    Unlike legacy compressed 0x70 START, this header is fixed-length and carries
    NO inline data — all payload flows via 0x0081 DATA frames.

    Args:
        compressed: Whether the streamed bytes are zlib-compressed.
        window: Requested window / max queue size (tokens in flight).
        ack_every: Requested ACK cadence (blocks per ack).
        max_frame: Client max frame size in bytes.
        total_size: Decompressed panel byte total.
        partial: Optional partial-region geometry (keyword-only). When set, flags
            bit1 is raised and the 12-byte geometry is appended.

    Returns:
        Command bytes for 0x0080.

    Raises:
        ValueError: If any field is outside its wire range.
    """
    if not 0 <= window <= 0xFF:
        raise ValueError(f"window out of uint8 range: {window}")
    if not 0 <= ack_every <= 0xFF:
        raise ValueError(f"ack_every out of uint8 range: {ack_every}")
    if not 0 <= max_frame <= 0xFFFF:
        raise ValueError(f"max_frame out of uint16 range: {max_frame}")
    if not 0 <= total_size <= 0xFFFFFFFF:
        raise ValueError(f"total_size out of uint32 range: {total_size}")

    cmd = CommandCode.PIPE_WRITE_START.to_bytes(2, byteorder="big")
    flags = PIPE_FLAG_COMPRESSED if compressed else 0
    if partial is not None:
        if not 1 <= partial.old_etag <= 0xFFFFFFFF:
            raise ValueError(f"partial old_etag must be a nonzero uint32, got {partial.old_etag}")
        for name, value in (("x", partial.x), ("y", partial.y), ("w", partial.w), ("h", partial.h)):
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"partial {name} out of uint16 range: {value}")
        flags |= PIPE_FLAG_PARTIAL
    header = bytes([PIPE_VERSION, flags, window, ack_every])
    packet = cmd + header + struct.pack("<H", max_frame) + struct.pack("<I", total_size)
    if partial is not None:
        packet += struct.pack("<IHHHH", partial.old_etag, partial.x, partial.y, partial.w, partial.h)
    return packet


def build_pipe_write_data_command(seq: int, chunk: bytes) -> bytes:
    """Build a PIPE_WRITE_DATA (0x0081) windowed data frame.

    Wire (plaintext): [0x00][0x81][seq:1][data]
        - seq  = chunk index mod 256, reset to 0 by each 0x0080
        - data = chunk bytes (<= frame_eff - PIPE_FRAME_OVERHEAD)

    When a session is active the frame is encrypted downstream (device._encrypt_frame):
    the standard CCM envelope wraps ``[seq][data]`` as the plaintext payload, so seq
    remains the first authenticated plaintext byte.

    Args:
        seq: Rolling sequence byte (0..255).
        chunk: Chunk payload bytes.

    Returns:
        Command bytes for 0x0081.

    Raises:
        ValueError: If seq is outside 0..255.
    """
    if not 0 <= seq <= 0xFF:
        raise ValueError(f"seq out of uint8 range: {seq}")
    return CommandCode.PIPE_WRITE_DATA.to_bytes(2, byteorder="big") + bytes([seq]) + chunk


def build_pipe_write_end_command(refresh_mode: int, new_etag: int | None = None) -> bytes:
    """Build a PIPE_WRITE_END (0x0082) command.

    Wire: [0x00][0x82][refresh:1] (+ [new_etag:4 BE] when provided).
    The etag tail mirrors build_direct_write_end_with_etag; presence is by length.

    Args:
        refresh_mode: Display refresh mode. Full-frame transfers: 0=full, 1=fast.
            Partial-negotiated transfers (PIPE_FLAG_PARTIAL): 0=full, 1=fast,
            2=partial; firmware defaults to partial when the byte is absent.
        new_etag: Optional uint32 etag to commit on the device.

    Returns:
        Command bytes for 0x0082.

    Raises:
        ValueError: If new_etag is outside uint32 range.
    """
    cmd = CommandCode.PIPE_WRITE_END.to_bytes(2, byteorder="big")
    if new_etag is None:
        return cmd + refresh_mode.to_bytes(1, byteorder="big")
    if not 0 <= new_etag <= 0xFFFFFFFF:
        raise ValueError(f"new_etag out of uint32 range: {new_etag}")
    return cmd + refresh_mode.to_bytes(1, byteorder="big") + new_etag.to_bytes(4, byteorder="big")


def build_nfc_write_inline_command(rec_type: int, payload: bytes) -> bytes:
    """Build an NFC_ENDPOINT inline write (sub-opcode 0x01).

    Wire: [0x00][0x83][0x01][rec_type:1][len:2 BE][payload]

    This builder only enforces the wire-level u16 length field. The firmware's
    120-byte inline-vs-chunked policy (NFC_INLINE_MAX) is the caller's concern
    (see device method) and is not validated here.

    Args:
        rec_type: NDEF record type (see NfcRecordType), 0..255.
        payload: Record payload bytes.

    Returns:
        Command bytes for 0x0083/0x01.

    Raises:
        ValueError: If payload is empty, payload exceeds 0xFFFF bytes, or
            rec_type is outside 0..255.
    """
    if not 0 <= rec_type <= 0xFF:
        raise ValueError(f"rec_type out of uint8 range: {rec_type}")
    if len(payload) == 0:
        raise ValueError("payload must not be empty")
    if len(payload) > 0xFFFF:
        raise ValueError(f"payload length {len(payload)} exceeds uint16 range")

    cmd = CommandCode.NFC_ENDPOINT.to_bytes(2, byteorder="big")
    return cmd + bytes([NFC_SUB_WRITE_INLINE, rec_type]) + len(payload).to_bytes(2, byteorder="big") + payload


def build_nfc_write_start_command(rec_type: int, total_len: int) -> bytes:
    """Build an NFC_ENDPOINT chunked-write start (sub-opcode 0x10).

    Wire: [0x00][0x83][0x10][rec_type:1][total_len:2 BE]

    Args:
        rec_type: NDEF record type (see NfcRecordType), 0..255.
        total_len: Total payload size the following DATA chunks will carry,
            1..NFC_WRITE_MAX_TOTAL (the firmware hard-rejects larger writes).

    Returns:
        Command bytes for 0x0083/0x10.

    Raises:
        ValueError: If total_len is outside 1..NFC_WRITE_MAX_TOTAL, or
            rec_type is outside 0..255.
    """
    if not 0 <= rec_type <= 0xFF:
        raise ValueError(f"rec_type out of uint8 range: {rec_type}")
    if not 1 <= total_len <= NFC_WRITE_MAX_TOTAL:
        raise ValueError(f"total_len must be 1..{NFC_WRITE_MAX_TOTAL}, got {total_len}")

    cmd = CommandCode.NFC_ENDPOINT.to_bytes(2, byteorder="big")
    return cmd + bytes([NFC_SUB_WRITE_START, rec_type]) + total_len.to_bytes(2, byteorder="big")


def build_nfc_write_data_command(chunk: bytes) -> bytes:
    """Build an NFC_ENDPOINT chunked-write data frame (sub-opcode 0x11).

    Wire: [0x00][0x83][0x11][bytes]

    Args:
        chunk: Chunk payload bytes, 1..NFC_CHUNK_SIZE.

    Returns:
        Command bytes for 0x0083/0x11.

    Raises:
        ValueError: If chunk is empty or exceeds NFC_CHUNK_SIZE.
    """
    if len(chunk) == 0:
        raise ValueError("chunk must not be empty")
    if len(chunk) > NFC_CHUNK_SIZE:
        raise ValueError(f"chunk size {len(chunk)} exceeds maximum {NFC_CHUNK_SIZE}")

    cmd = CommandCode.NFC_ENDPOINT.to_bytes(2, byteorder="big")
    return cmd + bytes([NFC_SUB_WRITE_DATA]) + chunk


def build_nfc_write_end_command() -> bytes:
    """Build an NFC_ENDPOINT chunked-write end / commit (sub-opcode 0x12).

    Wire: [0x00][0x83][0x12]

    Returns:
        Command bytes for 0x0083/0x12.
    """
    cmd = CommandCode.NFC_ENDPOINT.to_bytes(2, byteorder="big")
    return cmd + bytes([NFC_SUB_WRITE_END])


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


def build_buzzer_activate_command(buzzer_instance: int, config: BuzzerActivateConfig) -> bytes:
    """Build buzzer activate command (command 0x0077).

    Firmware command format: [cmd:2][instance:1][outer_repeats:1][n_patterns:1][patterns...]

    Args:
        buzzer_instance: Buzzer instance index (0-based)
        config: Typed buzzer activation config

    Returns:
        Command bytes for 0x0077
    """
    if not 0 <= buzzer_instance <= 0xFF:
        raise ValueError(f"Buzzer instance out of range: {buzzer_instance} (must be 0-255)")
    cmd = CommandCode.BUZZER_ACTIVATE.to_bytes(2, byteorder="big")
    return cmd + bytes([buzzer_instance]) + config.to_bytes()


def build_write_config_command(config_data: bytes) -> tuple[bytes, list[bytes]]:
    """Build WRITE_CONFIG command with chunking support.

    Protocol:
    - Single chunk (≤200 bytes): [0x00][0x41][config_data]
    - Multi-chunk (>200 bytes):
      - First: [0x00][0x41][total_size:2LE][first_200_bytes]
      - Rest: [0x00][0x42][chunk_data] (up to 200 bytes each)

    The first chunk carries a full 200 data bytes (payload = size(2) + 200 = 202
    bytes). Firmware only enters chunked mode when the payload length exceeds 200
    and expects exactly [total:2LE][200 data]; a 198-byte first chunk (200-byte
    payload) makes it take the single-chunk path and store the size header plus a
    truncated config, then NACK every following 0x42 chunk, and also breaks its
    ``expectedChunks = ceil(total / 200)`` accounting.

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
        # first_cmd: [0x00][0x41][total_size:2LE][first_200_bytes]
        # chunks: [[0x00][0x42][chunk_data], ...]
    """
    cmd_write = CommandCode.WRITE_CONFIG.to_bytes(2, byteorder="big")
    cmd_chunk = CommandCode.WRITE_CONFIG_CHUNK.to_bytes(2, byteorder="big")

    config_len = len(config_data)

    # Single chunk mode (≤200 bytes)
    if config_len <= CONFIG_CHUNK_SIZE:
        return cmd_write + config_data, []

    # Multi-chunk mode (>200 bytes)
    # First chunk: [cmd][total_size:2LE][first_200_bytes]
    total_size = config_len.to_bytes(2, byteorder="little")
    first_chunk_data_size = CONFIG_CHUNK_SIZE  # 200 bytes
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
