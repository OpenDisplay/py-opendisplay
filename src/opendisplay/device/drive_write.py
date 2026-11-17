"""An upload in a named drive mode: CMD_DRIVE_WRITE_START (0x0085), data over 0x71, END 0x72.

One transfer carries every pass of a multi-pass mode back to back (one zlib stream when
compressed); the device advances to the next pass at each frame boundary, and each boundary is
a panel refresh that can land on any data chunk. ``PIPELINED`` is never set. Timeouts follow the
design note (``2026-11-16-v3-3b-drive-modes-design.md``, the website's rule).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from ..exceptions import DriveModeSubstitutedError, DriveModeTemperatureError, InvalidResponseError
from ..models.enums import RefreshMode
from ..models.panel_drive import DisplayMode
from ..protocol import CommandCode, validate_ack_response
from ..protocol.commands import (
    build_direct_write_end_command,
    build_drive_write_start,
)
from ..protocol.responses import parse_drive_write_start
from .channel import SecureChannel
from .upload import Uploader

_LOGGER = logging.getLogger(__name__)

_START_HEAD: Final = 11  # [00][85][mode][refresh][flags][rsv:2][uncompressed_size:4]
_PASS_REFRESH_S: Final = 45.0  # budget for one pass boundary (a panel refresh, observed up to 35 s)
_MIN_ACK_S: Final = 90.0


def start_timeout(passes: int, *, inline_data: bool, first_chunk: float) -> float:
    """How long to wait for the START answer.

    A START that carries data can see every pass boundary but the last before it is answered.
    Measured on 2026-11-16: 0.9 s without inline data (OD58D951, BG22, colour16) and 1.1 s with
    it (E1002, ESP32, single pass, so no pass boundary before the answer).
    """
    if not inline_data:
        return first_chunk
    return 10.0 + max(_MIN_ACK_S, (passes - 1) * _PASS_REFRESH_S)


def end_timeout(passes: int) -> float:
    """How long to wait for the END acknowledgement: one refresh per pass."""
    return max(_MIN_ACK_S, passes * _PASS_REFRESH_S)


async def drive_write(  # pylint: disable=too-many-arguments
    uploader: Uploader,
    channel: SecureChannel,
    mode: DisplayMode,
    frames: bytes,
    *,
    stream: bytes | None = None,
    refresh: RefreshMode = RefreshMode.FULL,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int | None:
    """Upload ``frames`` (every pass, concatenated) in ``mode``; no CPU-heavy work here.

    Args:
        stream: The frames as one zlib stream, to send compressed; ``None`` sends them as they are.

    Returns:
        The panel temperature the device reported, or ``None`` when unknown.

    Raises:
        DriveModeSubstitutedError: The device runs another mode; nothing was sent after START.
        DriveModeTemperatureError, DriveSequenceUnsupportedError, PanelBusyError,
        NeedsMigrationError: The device refused the START.
        InvalidResponseError: Another refusal, or the device expects another pass count.
    """
    compressed = stream is not None
    if stream is not None:
        room = channel.start_payload_limit() - _START_HEAD
        start = build_drive_write_start(
            mode.mode_index, refresh.value, uncompressed_size=len(frames), first_chunk=stream[:room]
        )
        remaining = stream[room:]
    else:
        start = build_drive_write_start(mode.mode_index, refresh.value)
        remaining = frames

    await channel.write(start)
    answer = await channel.read(
        start_timeout(mode.passes, inline_data=compressed, first_chunk=Uploader.TIMEOUT_FIRST_CHUNK)
    )
    try:
        actual, passes, temp_c = parse_drive_write_start(answer)
    except DriveModeTemperatureError as err:
        raise DriveModeTemperatureError(mode.name, mode.temp_min_c, mode.temp_max_c) from err
    if actual != mode.mode_index:
        raise DriveModeSubstitutedError(mode.name, actual, temp_c)
    if passes != mode.passes:
        raise InvalidResponseError(
            f"The device expects {passes} passes for '{mode.name}', the config declares {mode.passes}; nothing sent"
        )
    _LOGGER.info("Drive write '%s': %d passes, panel at %s °C", mode.name, passes, temp_c)

    auto_completed = await uploader.send_data_chunks(remaining, progress_callback) if remaining else False
    if not auto_completed:
        await channel.write(build_direct_write_end_command(refresh.value))
        validate_ack_response(await channel.read(end_timeout(passes)), CommandCode.DIRECT_WRITE_END)
    await uploader.await_refresh_complete()
    return temp_c
