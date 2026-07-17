"""BLE response validation and parsing."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    AuthenticationSessionExistsError,
    InvalidResponseError,
    NfcWriteError,
)
from ..models.firmware import FirmwareVersion
from .commands import RESPONSE_HIGH_BIT_FLAG, CommandCode

# Status bytes returned in AUTHENTICATE responses
_AUTH_STATUS_OK = 0x00
_AUTH_STATUS_WRONG_KEY = 0x01
_AUTH_STATUS_ALREADY_AUTHENTICATED = 0x02
_AUTH_STATUS_ENCRYPTION_NOT_CONFIGURED = 0x03
_AUTH_STATUS_RATE_LIMITED = 0x04

# Default device ID used by old firmware (pre-23-byte challenge format)
_DEFAULT_DEVICE_ID = bytes([0x00, 0x00, 0x00, 0x01])


def unpack_command_code(data: bytes, offset: int = 0) -> int:
    """Extract 2-byte big-endian command code from response data.

    Args:
        data: Response data from device
        offset: Byte offset to read from (default: 0)

    Returns:
        Command code as integer

    Raises:
        InvalidResponseError: If fewer than 2 bytes are available at ``offset``.
    """
    if len(data) < offset + 2:
        raise InvalidResponseError(f"Response too short for a command code: {len(data)} bytes at offset {offset}")
    return int(struct.unpack(">H", data[offset : offset + 2])[0])


def strip_command_echo(data: bytes, expected_cmd: CommandCode) -> bytes:
    """Strip command echo from response data.

    Firmware echoes commands in responses, sometimes with high bit set.
    This function removes the 2-byte echo if present.

    Args:
        data: Response data from device
        expected_cmd: Expected command echo

    Returns:
        Data with echo stripped (if present), otherwise original data
    """
    if len(data) >= 2:
        echo = unpack_command_code(data)
        if echo in (expected_cmd, expected_cmd | RESPONSE_HIGH_BIT_FLAG):
            return data[2:]
    return data


def is_compressed_failure_frame(response: bytes) -> bool:
    """Return True if ``response`` is a firmware compressed-direct-write failure frame.

    Firmware signals that it cannot honor a compressed DIRECT_WRITE_START by
    replying with a 2-byte error frame. Older firmware sends the non-conformant
    ``{0xFF, 0xFF}``; spec-conformant firmware sends ``{0xFF, <cmd low byte>}``
    i.e. ``{0xFF, 0x70}``. Both mean "fall back to the uncompressed protocol",
    so the client accepts either form.
    """
    return (
        len(response) == 2
        and response[0] == 0xFF
        and response[1]
        in (
            0xFF,
            CommandCode.DIRECT_WRITE_START & 0xFF,
        )
    )


def check_response_type(response: bytes) -> tuple[CommandCode, bool]:
    """Check response type and whether it's an ACK.

    Args:
        response: Raw response data from device

    Returns:
        Tuple of (command_code, is_ack)
        - command_code: The command code (without high bit)
        - is_ack: True if response has high bit set (RESPONSE_HIGH_BIT_FLAG)
    """
    code = unpack_command_code(response)
    is_ack = bool(code & RESPONSE_HIGH_BIT_FLAG)
    raw = code & ~RESPONSE_HIGH_BIT_FLAG
    try:
        command = CommandCode(raw)
    except ValueError as e:
        # Firmware error frames (e.g. {0xFF,0xFF} compressed-failure, 4-byte
        # NACKs) carry codes that aren't in CommandCode; surface a typed protocol
        # error instead of a bare ValueError so upload loops can handle it.
        raise InvalidResponseError(f"Unknown command code in response: 0x{raw:04x}") from e
    return command, is_ack


def validate_ack_response(data: bytes, expected_command: int) -> None:
    """Validate ACK response from device.

    ACK responses echo the command code (sometimes with high bit set).

    Args:
        data: Raw response data
        expected_command: Command code that was sent

    Raises:
        InvalidResponseError: If response invalid or doesn't match command
    """
    if len(data) < 2:
        raise InvalidResponseError(f"ACK too short: {len(data)} bytes (need at least 2)")

    response_code = unpack_command_code(data)

    # Response can be exact echo or with high bit set (RESPONSE_HIGH_BIT_FLAG | cmd)
    valid_responses = {expected_command, expected_command | RESPONSE_HIGH_BIT_FLAG}

    if response_code not in valid_responses:
        raise InvalidResponseError(f"ACK mismatch: expected 0x{expected_command:04x}, got 0x{response_code:04x}")


def parse_authenticate_challenge(data: bytes) -> tuple[bytes, bytes]:
    """Parse step-1 AUTHENTICATE response and return the server nonce and device ID.

    Supports two formats:
    - Old (19 bytes): [0x0050][status:1][server_nonce:16]
    - New (23 bytes): [0x0050][status:1][server_nonce:16][device_id:4]

    Args:
        data: Raw BLE notification from device

    Returns:
        Tuple of (server_nonce: bytes[16], device_id: bytes[4]).
        device_id defaults to [0x00, 0x00, 0x00, 0x01] for old firmware.

    Raises:
        AuthenticationSessionExistsError: If device reports an existing session (status 0x02) —
            caller should retry to receive a fresh challenge.
        AuthenticationFailedError: If device returns an error status
        AuthenticationRequiredError: If encryption is not configured on the device
        InvalidResponseError: If response format is invalid
    """
    if len(data) < 2:
        raise InvalidResponseError(f"Auth challenge response too short: {len(data)} bytes")

    echo = unpack_command_code(data)
    if echo not in (0x0050, 0x0050 | RESPONSE_HIGH_BIT_FLAG):
        raise InvalidResponseError(f"Auth challenge echo mismatch: got 0x{echo:04x}")

    if len(data) < 3:
        raise InvalidResponseError("Auth challenge response missing status byte")

    status = data[2]
    if status == _AUTH_STATUS_ALREADY_AUTHENTICATED:
        raise AuthenticationSessionExistsError("Device has an active session; retry to get a fresh challenge")
    if status == _AUTH_STATUS_ENCRYPTION_NOT_CONFIGURED:
        raise AuthenticationRequiredError("Device does not have encryption configured")
    if status == _AUTH_STATUS_RATE_LIMITED:
        raise AuthenticationFailedError("Authentication rate limit exceeded — wait before retrying")
    if status != _AUTH_STATUS_OK:
        raise AuthenticationFailedError(f"Auth challenge failed with status 0x{status:02x}")

    if len(data) < 19:  # 2 echo + 1 status + 16 nonce
        raise InvalidResponseError(f"Auth challenge response too short for nonce: {len(data)} bytes (need 19)")

    server_nonce = data[3:19]
    device_id = data[19:23] if len(data) >= 23 else _DEFAULT_DEVICE_ID
    return server_nonce, device_id


def parse_authenticate_success(data: bytes) -> bytes:
    """Parse step-2 AUTHENTICATE response and return the server proof.

    Expected format: [0x0050][status:1][server_response:16]

    Args:
        data: Raw BLE notification from device

    Returns:
        16-byte server response (proof that device also knows the key)

    Raises:
        AuthenticationError: If device rejects the challenge response
        InvalidResponseError: If response format is invalid
    """
    if len(data) < 2:
        raise InvalidResponseError(f"Auth success response too short: {len(data)} bytes")

    echo = unpack_command_code(data)
    if echo not in (0x0050, 0x0050 | RESPONSE_HIGH_BIT_FLAG):
        raise InvalidResponseError(f"Auth success echo mismatch: got 0x{echo:04x}")

    if len(data) < 3:
        raise InvalidResponseError("Auth success response missing status byte")

    status = data[2]
    if status == _AUTH_STATUS_WRONG_KEY:
        raise AuthenticationFailedError("Authentication failed: wrong encryption key")
    if status == _AUTH_STATUS_RATE_LIMITED:
        raise AuthenticationFailedError("Authentication rate limit exceeded — wait before retrying")
    if status != _AUTH_STATUS_OK:
        raise AuthenticationFailedError(f"Authentication failed with status 0x{status:02x}")

    if len(data) < 19:  # 2 echo + 1 status + 16 server_response
        raise InvalidResponseError(f"Auth success response too short for server proof: {len(data)} bytes (need 19)")

    return data[3:19]


def parse_firmware_version(data: bytes) -> FirmwareVersion:
    """Parse firmware version response.

    Format: [echo:2][major:1][minor:1][shaLength:1][sha:variable]

    Args:
        data: Raw firmware version response

    Returns:
        FirmwareVersion dictionary with 'major', 'minor', and 'sha' fields

    Raises:
        InvalidResponseError: If response format invalid
    """
    if len(data) < 5:
        raise InvalidResponseError(f"Firmware version response too short: {len(data)} bytes (need at least 5)")

    # Validate echo
    echo = unpack_command_code(data)
    if echo not in (0x0043, 0x0043 | RESPONSE_HIGH_BIT_FLAG):
        raise InvalidResponseError(f"Firmware version echo mismatch: expected 0x0043, got 0x{echo:04x}")

    major = data[2]
    minor = data[3]
    sha_length = data[4]

    # SHA hash is always present in firmware responses
    if sha_length == 0:
        raise InvalidResponseError("Firmware version missing SHA hash (shaLength is 0)")

    # Validate sufficient bytes for SHA
    expected_total_length = 5 + sha_length
    if len(data) < expected_total_length:
        raise InvalidResponseError(
            f"Firmware version response incomplete: expected {expected_total_length} bytes "
            f"(5 header + {sha_length} SHA), got {len(data)}"
        )

    # Extract SHA bytes and decode as ASCII string
    sha_bytes = data[5 : 5 + sha_length]
    try:
        sha = sha_bytes.decode("ascii")
    except UnicodeDecodeError as e:
        raise InvalidResponseError(f"Invalid SHA hash encoding (expected ASCII): {e}") from e

    return {
        "major": major,
        "minor": minor,
        "sha": sha,
    }


# ─── PIPE_WRITE (0x0080-0x0082) sliding-window responses ──────────────────────

# 0x0080 START NACK error codes (Part 1 §1.1)
PIPE_START_NACK_BAD_PARAMS = 0x01  # bad version / params
PIPE_START_NACK_COMPRESSION = 0x02  # compression unsupported (retry uncompressed)
PIPE_START_NACK_SIZE = 0x03  # total_size mismatch vs panel config
PIPE_START_NACK_BUSY = 0x04  # busy / bad state
PIPE_START_NACK_ETAG_MISMATCH = 0x05  # partial: old_etag == 0 or != device displayed_etag
PIPE_START_NACK_PARTIAL_UNSUPPORTED = 0x06  # partial: bpp != 1 or unsupported driver
PIPE_START_NACK_RECT_INVALID = 0x07  # partial: rect zero-size / OOB / misaligned

# 0x0081 DATA NACK error codes (all fatal, Part 1 §1.3)
PIPE_DATA_NACK_DECOMPRESS = 0x02
PIPE_DATA_NACK_WRITE = 0x03
PIPE_DATA_NACK_STATE = 0x04

# Frame classifications returned by classify_pipe_frame().
PIPE_FRAME_ACK = "PIPE_ACK"
PIPE_FRAME_NACK = "PIPE_NACK"
PIPE_FRAME_END_ACK = "END_ACK"
PIPE_FRAME_END_NACK = "END_NACK"
PIPE_FRAME_OTHER = "OTHER"


@dataclass(frozen=True)
class PipeParams:
    """Effective sliding-window parameters after the min-rule negotiation.

    All values are the negotiated effective ones (min of client request and
    device maximum), computed identically on both sides.

    Attributes:
        window: W_eff — tokens in flight (1..32).
        ack_every: N_eff — ACK cadence, clamped to <= window.
        max_frame: frame_eff — effective frame size in bytes (<= 244).
        selective: Response flags bit0 — receiver buffers out-of-order chunks
            (selective repeat). When False the sender uses rewind-style recovery.
        compressed: Whether this transfer streams zlib-compressed bytes.
        partial: Whether this is a partial-region refresh (0x0080 flags bit1
            requested and confirmed by the device's ACK flags bit1). Partial
            transfers never auto-complete, so the sender always uses the explicit
            0x0082 END completion contract.
    """

    window: int
    ack_every: int
    max_frame: int
    selective: bool
    compressed: bool
    partial: bool = False


def parse_pipe_start_response(data: bytes) -> tuple[bool, object]:
    """Parse a PIPE_WRITE_START (0x0080) response.

    Both forms tolerate trailing bytes (future fields).

    ACK  (>= 8 B): [0x00][0x80][ver:1][dev_max_window:1][dev_max_ack_every:1]
                   [dev_max_frame:2 LE][flags:1]
    NACK (4 B):    [0xFF][0x80][err:1][0x00]

    Args:
        data: Decrypted response bytes (``_read`` yields the ``00 80`` / ``FF 80``
            prefix on both plaintext and encrypted links).

    Returns:
        (True, (ver, dev_max_window, dev_max_ack_every, dev_max_frame, flags)) on ACK,
        or (False, err_code) on NACK.

    Raises:
        InvalidResponseError: On a too-short/garbled ACK or an unexpected echo.
    """
    if len(data) < 2:
        raise InvalidResponseError(f"PIPE START response too short: {len(data)} bytes")

    if data[0] == 0xFF and data[1] == 0x80:
        err = data[2] if len(data) >= 3 else 0
        return False, err

    if data[0] == 0x00 and data[1] == 0x80:
        if len(data) < 8:
            raise InvalidResponseError(f"PIPE START ACK too short: {len(data)} bytes (need >= 8)")
        ver = data[2]
        dev_max_window = data[3]
        dev_max_ack_every = data[4]
        dev_max_frame = int(struct.unpack("<H", data[5:7])[0])
        flags = data[7]
        return True, (ver, dev_max_window, dev_max_ack_every, dev_max_frame, flags)

    raise InvalidResponseError(f"Unexpected PIPE START echo: 0x{data[0]:02x}{data[1]:02x}")


def parse_pipe_data_ack(data: bytes) -> tuple[int, int]:
    """Parse a PIPE_WRITE_DATA ACK: [0x00][0x81][highest_seen:1][ack_mask:4 LE].

    Trailing bytes tolerated.

    Returns:
        (highest_seen, ack_mask)

    Raises:
        InvalidResponseError: If the frame is not a >= 7-byte 0x0081 ACK.
    """
    if len(data) < 7 or data[0] != 0x00 or data[1] != 0x81:
        raise InvalidResponseError(f"Not a PIPE DATA ACK: {data[:8].hex()}")
    highest_seen = data[2]
    ack_mask = int(struct.unpack("<I", data[3:7])[0])
    return highest_seen, ack_mask


def parse_pipe_data_nack(data: bytes) -> tuple[int, int, int]:
    """Parse a PIPE_WRITE_DATA NACK: [0xFF][0x81][err:1][highest_seen:1][ack_mask:4 LE].

    Trailing bytes tolerated.

    Returns:
        (err, highest_seen, ack_mask)

    Raises:
        InvalidResponseError: If the frame is not a >= 8-byte 0xFF81 NACK.
    """
    if len(data) < 8 or data[0] != 0xFF or data[1] != 0x81:
        raise InvalidResponseError(f"Not a PIPE DATA NACK: {data[:8].hex()}")
    err = data[2]
    highest_seen = data[3]
    ack_mask = int(struct.unpack("<I", data[4:8])[0])
    return err, highest_seen, ack_mask


def unpack_ack_ranges(highest_seen: int, ack_mask: int, window_base: int) -> set[int]:
    """Expand a QUIC-style ACK into absolute acked chunk indexes.

    ``highest_seen`` is a rolling mod-256 seq; it is resolved against the sender's
    ``window_base`` (lowest unacked absolute index). Because the in-flight range is
    <= 32 « 256, the resolution is unambiguous — including when the ACK is stale
    (its highest_seen sits just below window_base after a superseding ACK already
    advanced the window).

    ``ack_mask`` bit i (LSB first) marks chunk ``highest_seen - 1 - i`` as received.

    Args:
        highest_seen: Highest received seq (mod 256), implicitly acked.
        ack_mask: 32-bit selective-ack bitmask.
        window_base: Sender's lowest unacked absolute chunk index.

    Returns:
        Set of absolute chunk indexes the ACK reports as received.
    """
    base_mod = window_base % 256
    delta = (highest_seen - base_mod) % 256
    if delta > 128:  # highest_seen is behind window_base (stale / fully contiguous)
        delta -= 256
    h_abs = window_base + delta

    acked: set[int] = set()
    if h_abs >= 0:
        acked.add(h_abs)
    for i in range(32):
        if ack_mask & (1 << i):
            idx = h_abs - 1 - i
            if idx >= 0:
                acked.add(idx)
    return acked


def classify_pipe_frame(data: bytes) -> str:
    """Classify an inbound frame during a pipe transfer by length + opcode.

    Returns one of PIPE_FRAME_ACK / PIPE_FRAME_NACK / PIPE_FRAME_END_ACK /
    PIPE_FRAME_END_NACK / PIPE_FRAME_OTHER. The more-specific (longer) ACK/NACK
    forms are checked before the 2-byte END forms.

    Note: pipe reads MUST use this rather than ``check_response_type`` — the
    device→host NACK opcode 0xFF81 is not a member of ``CommandCode`` and would
    raise there.
    """
    if len(data) >= 7 and data[0] == 0x00 and data[1] == 0x81:
        return PIPE_FRAME_ACK
    if len(data) >= 8 and data[0] == 0xFF and data[1] == 0x81:
        return PIPE_FRAME_NACK
    if len(data) >= 2 and data[0] == 0x00 and data[1] == 0x82:
        return PIPE_FRAME_END_ACK
    if len(data) >= 2 and data[0] == 0xFF and data[1] == 0x82:
        return PIPE_FRAME_END_NACK
    return PIPE_FRAME_OTHER


# ─── NFC_ENDPOINT (0x0083) responses ───────────────────────────────────────

# Status byte in the {0x00, 0x83, status} OK frame.
NFC_STATUS_WRITE_OK = 0x81  # Inline write committed, or chunked write end committed
NFC_STATUS_CHUNK_ACK = 0x82  # Chunk-stage ACK (chunk start accepted, chunk data accepted)

# Error codes carried in the {0xFF, 0x83, 0xFF, err} error frame. Error frames
# are always sent plaintext by firmware, even over an encrypted connection.
NFC_ERROR_MESSAGES: dict[int, str] = {
    1: "NFC write failed: malformed command",
    2: "NFC write failed: read failed",
    3: "NFC write failed: write failed (NFC disabled in config, IC error, or record too large for the NFC EEPROM)",
    4: "NFC write failed: unknown sub-command",
    5: "NFC write failed: invalid record type",
    6: "NFC write failed: invalid length",
    7: "NFC write failed: no active chunk session",
    8: "NFC write failed: chunk overflow",
    9: "NFC write failed: length mismatch at end",
}


def validate_nfc_response(data: bytes, expected_status: int) -> None:
    """Validate an NFC_ENDPOINT (0x0083) response frame.

    Unlike ``validate_ack_response``, this distinguishes the two OK statuses
    (0x81 write-committed vs. 0x82 chunk-stage ACK) and decodes the firmware's
    dedicated NFC error frame.

    Args:
        data: Decrypted response bytes (``cmd(2) + payload``).
        expected_status: NFC_STATUS_WRITE_OK or NFC_STATUS_CHUNK_ACK, whichever
            the caller's request should produce.

    Raises:
        NfcWriteError: If the device returned ``{0xFF, 0x83, 0xFF, err}``.
        InvalidResponseError: If the frame is too short, has an unexpected
            command echo, or an OK frame with a status other than
            ``expected_status``.
    """
    if len(data) >= 4 and data[0] == 0xFF and data[1] == 0x83 and data[2] == 0xFF:
        error_code = data[3]
        message = NFC_ERROR_MESSAGES.get(error_code, f"NFC write failed: unknown error code 0x{error_code:02x}")
        raise NfcWriteError(message, error_code=error_code)

    if len(data) >= 3 and data[0] == 0x00 and data[1] == 0x83:
        status = data[2]
        if status == expected_status:
            return
        raise InvalidResponseError(
            f"NFC response status mismatch: expected 0x{expected_status:02x}, got 0x{status:02x}"
        )

    raise InvalidResponseError(f"Unexpected NFC_ENDPOINT response: {data[:4].hex()}")
