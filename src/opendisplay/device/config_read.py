"""Reading the stored config off the device: READ_CONFIG, its chunks, and nothing else.

Returns the assembled bytes as the device sent them; parsing is the caller's choice, so the
legacy ``interrogate()`` and the new ``Device`` can parse the same bytes strictly or leniently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..exceptions import NoStoredConfigError, OpenDisplayTimeoutError, ProtocolError, TruncatedConfigError
from ..protocol import CommandCode, build_read_config_command
from ..protocol.responses import describe_command_code, matches_command_echo, strip_command_echo, unpack_command_code
from .channel import SecureChannel

_LOGGER = logging.getLogger(__name__)


async def read_matching_echo(
    read: Callable[[float], Awaitable[bytes]],
    expected: CommandCode,
    timeout: float,
    max_stray: int,
    *,
    purpose: str,
    allow_nack: bool = False,
) -> bytes:
    """Read until a frame echoes ``expected``, ignoring frames from other commands.

    The notification queue has no request/response correlation, so a frame
    belonging to a different exchange — a duplicated response, or one the
    firmware held in its TX ring until notifications were enabled — can land
    mid-transfer. Skipping such frames keeps each command on its own stream.

    When ``allow_nack`` is true, a NACK form ``{0xFF, <cmd low byte>, ...}`` for
    ``expected`` is returned rather than skipped (config read uses this).

    Raises:
        OpenDisplayTimeoutError: If no frame arrives within ``timeout``
        ProtocolError: If more than ``max_stray`` foreign frames arrive first
    """
    for _ in range(max_stray + 1):
        response = await read(timeout)
        if matches_command_echo(response, expected):
            return response
        if allow_nack and len(response) >= 2 and response[0] == 0xFF and response[1] == (expected & 0xFF):
            return response
        _LOGGER.warning(
            "Ignoring stray %s frame (%d B) while reading %s",
            (
                describe_command_code(unpack_command_code(response))
                if len(response) >= 2
                else f"malformed {response.hex() or '<empty>'}"
            ),
            len(response),
            purpose,
        )
    raise ProtocolError(
        f"{purpose} read aborted: more than {max_stray} frames from other "
        f"commands arrived while waiting for a {purpose} response"
    )


async def _read_config_frame(channel: SecureChannel, timeout: float, max_stray: int) -> bytes:
    """Read the next READ_CONFIG frame, ignoring frames from other commands.

    The device's no-config error frame ({0xFF, 0x40, ...}) is passed through
    rather than skipped: it answers this command and the caller reports it.
    """
    return await read_matching_echo(
        channel.read,
        CommandCode.READ_CONFIG,
        timeout,
        max_stray,
        purpose="config",
        allow_nack=True,
    )


async def read_config_blob(
    channel: SecureChannel, *, first_timeout: float, chunk_timeout: float, max_stray: int
) -> bytes:
    """Send READ_CONFIG and assemble the stored config from its chunks.

    Returns:
        The config as stored: ``[length:2][version:1][packets...][crc:2]``.

    Raises:
        NoStoredConfigError: The device has no config.
        TruncatedConfigError: The device stopped sending before the announced length.
        ProtocolError: Too many frames from other commands arrived.
    """
    # Send read config command
    cmd = build_read_config_command()
    await channel.write(cmd)

    # Read first chunk
    response = await _read_config_frame(channel, first_timeout, max_stray)

    # Firmware answers a device-with-no-config with the 4-byte error frame
    # {0xFF, 0x40, 0x00, 0x00}. Without this check the {0x00,0x00} length
    # field is misread as a zero-length config instead of "no config".
    if len(response) == 4 and response[0] == 0xFF and response[1] == CommandCode.READ_CONFIG:
        raise NoStoredConfigError()

    chunk_data = strip_command_echo(response, CommandCode.READ_CONFIG)

    # Parse first chunk header
    total_length = int.from_bytes(chunk_data[2:4], "little")
    tlv_data = bytearray(chunk_data[4:])

    _LOGGER.debug("First chunk: %d bytes, total length: %d", len(chunk_data), total_length)

    # Read remaining chunks. Firmware caps config-read chunks, so a broken or
    # older firmware can stop sending before `total_length` is reached. Guard
    # against both a mid-transfer read timeout and a stalled transfer (empty
    # chunk making no progress) so we raise a typed error instead of hanging
    # or returning a partial config.
    while len(tlv_data) < total_length:
        try:
            next_response = await _read_config_frame(channel, chunk_timeout, max_stray)
        except OpenDisplayTimeoutError as err:
            raise TruncatedConfigError(
                f"Config read truncated: device stopped sending chunks at {len(tlv_data)}/{total_length} bytes"
            ) from err
        next_chunk_data = strip_command_echo(next_response, CommandCode.READ_CONFIG)

        # Skip chunk number field (2 bytes) and append data
        chunk_payload = next_chunk_data[2:]
        if not chunk_payload:
            raise TruncatedConfigError(
                f"Config read stalled: device sent an empty chunk at {len(tlv_data)}/{total_length} bytes"
            )
        tlv_data.extend(chunk_payload)

        _LOGGER.debug(
            "Received chunk, total: %d/%d bytes",
            len(tlv_data),
            total_length,
        )

    _LOGGER.info("Received complete TLV data: %d bytes", len(tlv_data))
    return bytes(tlv_data)
