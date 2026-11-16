"""Refusals firmware 3.0 gives a reason for: needs migration, panel busy, config write busy.

Frames follow Firmware_Unified ``opendisplay_protocol.h`` sections 4g and 4h: an upload START
NACK is ``[FF][70|76|80][err][00]`` with err 0x08 (no usable drive data) or 0x09 (panel busy);
a refused config write is ``[FF][41|42][01][00]`` while a display pass is running.
"""

from __future__ import annotations

import zlib

import pytest
from PIL import Image
from test_device_partial import _config as partial_config
from test_device_partial import _image
from test_pipe_write import ScriptedConn, make_config, make_device, make_region, start_nack

from opendisplay import OpenDisplayDevice, PartialState, RefreshMode
from opendisplay.exceptions import (
    ConfigWriteBusyError,
    InvalidResponseError,
    NeedsMigrationError,
    PanelBusyError,
)
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme

ZIP_ONLY = 0x02  # no pipe bit: uploads go straight to DIRECT_WRITE
ZIP_AND_PIPE = 0x12
ACK_START, ACK_DATA, ACK_END, ACK_REFRESH = b"\x00\x70", b"\x00\x71", b"\x00\x72", b"\x00\x73"
_COMPRESSED = zlib.compress(b"\x00" * 64)


def _opcodes(conn: ScriptedConn) -> list[int]:
    return [frame[1] for frame in conn.written]


# ─── DIRECT_WRITE START (0x70) ───────────────────────────────────────────────


async def test_direct_write_start_needs_migration() -> None:
    dev, conn = make_device([bytes.fromhex("ff700800")], transmission_modes=ZIP_ONLY)

    with pytest.raises(NeedsMigrationError):
        await dev._uploader.execute_upload(b"\x00" * 8, RefreshMode.FULL, use_compression=False)

    assert _opcodes(conn) == [0x70]


async def test_compressed_start_needs_migration_does_not_fall_back() -> None:
    dev, conn = make_device([bytes.fromhex("ff700800")], transmission_modes=ZIP_ONLY)

    with pytest.raises(NeedsMigrationError):
        await dev._uploader.execute_upload(
            b"\x00" * 64, RefreshMode.FULL, use_compression=True, compressed_data=_COMPRESSED, uncompressed_size=64
        )

    assert _opcodes(conn) == [0x70]  # no second, uncompressed START


async def test_direct_write_start_panel_busy() -> None:
    dev, _ = make_device([bytes.fromhex("ff700900")], transmission_modes=ZIP_ONLY)

    with pytest.raises(PanelBusyError):
        await dev._uploader.execute_upload(b"\x00" * 8, RefreshMode.FULL, use_compression=False)


async def test_unknown_start_refusal_keeps_the_compressed_fallback() -> None:
    dev, conn = make_device(
        [bytes.fromhex("ff700a00"), ACK_START, ACK_DATA, ACK_END, ACK_REFRESH], transmission_modes=ZIP_ONLY
    )

    await dev._uploader.execute_upload(
        b"\x00" * 64, RefreshMode.FULL, use_compression=True, compressed_data=_COMPRESSED, uncompressed_size=64
    )

    assert _opcodes(conn)[:2] == [0x70, 0x70]  # compressed START refused, uncompressed START follows
    assert len(conn.written[1]) < len(conn.written[0])  # the retry carries no compressed payload


async def test_unknown_uncompressed_start_refusal_is_still_an_invalid_response() -> None:
    dev, _ = make_device([bytes.fromhex("ff700a00")], transmission_modes=ZIP_ONLY)

    with pytest.raises(InvalidResponseError):
        await dev._uploader.execute_upload(b"\x00" * 8, RefreshMode.FULL, use_compression=False)


# ─── Partial START (0x76) ────────────────────────────────────────────────────


def _partial_device(reply: bytes) -> tuple[OpenDisplayDevice, ScriptedConn, PartialState, Image.Image]:
    """A 16x8 device whose config allows partial updates, with one frame already displayed."""
    dev = OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        config=partial_config(),
        capabilities=DeviceCapabilities(width=16, height=8, color_scheme=ColorScheme.MONO),
    )
    conn = ScriptedConn([reply])
    dev._attach(conn)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    return dev, conn, state, _image(changed=True)


async def test_partial_start_needs_migration() -> None:
    dev, conn, state, new = _partial_device(bytes.fromhex("ff760800"))

    with pytest.raises(NeedsMigrationError):
        await dev.upload_prepared_image((b"\x00" * 16, None, new), state=state)

    assert _opcodes(conn) == [0x76]  # no fallback to a full 0x70 upload


async def test_partial_start_panel_busy() -> None:
    dev, conn, state, new = _partial_device(bytes.fromhex("ff760900"))

    with pytest.raises(PanelBusyError):
        await dev.upload_prepared_image((b"\x00" * 16, None, new), state=state)

    assert _opcodes(conn) == [0x76]


# ─── PIPE START (0x80) ───────────────────────────────────────────────────────


async def test_pipe_start_needs_migration() -> None:
    dev, conn = make_device([start_nack(0x08)], transmission_modes=ZIP_AND_PIPE)

    with pytest.raises(NeedsMigrationError):
        await dev.upload_prepared_image((b"\x00" * 8, None, Image.new("P", (8, 8), 0)))

    assert _opcodes(conn) == [0x80]  # no fallback to DIRECT_WRITE


async def test_pipe_start_other_nack_still_falls_back_to_direct_write() -> None:
    dev, conn = make_device(
        [start_nack(0x04), ACK_START, ACK_DATA, ACK_END, ACK_REFRESH], transmission_modes=ZIP_AND_PIPE
    )

    await dev.upload_prepared_image((b"\x00" * 8, None, Image.new("P", (8, 8), 0)), compress=False)

    assert _opcodes(conn)[:2] == [0x80, 0x70]


async def test_pipe_partial_start_needs_migration() -> None:
    dev, conn = make_device([start_nack(0x08)], transmission_modes=ZIP_AND_PIPE)

    with pytest.raises(NeedsMigrationError):
        await dev._uploader.negotiate_pipe_partial(compressed=False, total_size=16, old_etag=0x1, region=make_region())

    assert _opcodes(conn) == [0x80]


async def test_pipe_start_0x09_is_not_panel_busy() -> None:
    """PIPE never sends "panel busy": it ACKs before the panel comes up. 0x09 there stays a plain NACK."""
    dev, conn = make_device(
        [start_nack(0x09), ACK_START, ACK_DATA, ACK_END, ACK_REFRESH], transmission_modes=ZIP_AND_PIPE
    )

    await dev.upload_prepared_image((b"\x00" * 8, None, Image.new("P", (8, 8), 0)), compress=False)

    assert _opcodes(conn)[:2] == [0x80, 0x70]  # fell back to DIRECT_WRITE as for any other pipe NACK


# ─── Config write (0x41 / 0x42) ──────────────────────────────────────────────


async def test_config_write_busy() -> None:
    dev, _ = make_device([bytes.fromhex("ff410100")])

    with pytest.raises(ConfigWriteBusyError):
        await dev.write_config(make_config())


async def test_config_write_other_refusal_is_unchanged() -> None:
    dev, _ = make_device([bytes.fromhex("ff410000")])

    with pytest.raises(InvalidResponseError):
        await dev.write_config(make_config())
