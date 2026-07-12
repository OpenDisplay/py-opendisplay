"""Client-flow tests for streamed partial uploads."""

from __future__ import annotations

import asyncio
import struct

import pytest
from epaper_dithering import ColorScheme
from PIL import Image
from test_pipe_write import (
    END_ACK,
    REFRESH_COMPLETE,
    REFRESH_TIMEOUT,
    ScriptedConn,
    data_ack,
    data_nack,
    start_ack,
    start_nack,
)
from test_pipe_write_sender import _make_session

from opendisplay import OpenDisplayDevice
from opendisplay.crypto import decrypt_response
from opendisplay.encoding.compression import zlib_window_bits
from opendisplay.exceptions import BLETimeoutError, RefreshTimeoutError
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.config import DisplayConfig, GlobalConfig, ManufacturerData, PowerOption, SystemConfig
from opendisplay.models.enums import PartialUpdateSupport, RefreshMode
from opendisplay.partial import ERR_ETAG_MISMATCH, PARTIAL_FLAG_COMPRESSED, PartialState


def _config(
    partial_update_support: int = 1,
    transmission_modes: int = 0x00,
    pixel_width: int = 16,
    pixel_height: int = 8,
) -> GlobalConfig:
    return GlobalConfig(
        system=SystemConfig(
            ic_type=0,
            communication_modes=0,
            device_flags=0,
            pwr_pin=0xFF,
            reserved=b"\x00" * 17,
        ),
        manufacturer=ManufacturerData(
            manufacturer_id=0,
            board_type=0,
            board_revision=0,
            reserved=b"\x00" * 18,
        ),
        power=PowerOption(
            power_mode=0,
            battery_capacity_mah=b"\x00\x00\x00",
            sleep_timeout_ms=0,
            tx_power=0,
            sleep_flags=0,
            battery_sense_pin=0xFF,
            battery_sense_enable_pin=0xFF,
            battery_sense_flags=0,
            capacity_estimator=0,
            voltage_scaling_factor=0,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            reserved=b"\x00" * 12,
        ),
        displays=[
            DisplayConfig(
                instance_number=0,
                display_technology=0,
                panel_ic_type=0,
                pixel_width=pixel_width,
                pixel_height=pixel_height,
                active_width_mm=10,
                active_height_mm=10,
                tag_type=0,
                rotation=0,
                reset_pin=0xFF,
                busy_pin=0xFF,
                dc_pin=0xFF,
                cs_pin=0xFF,
                data_pin=0,
                partial_update_support=partial_update_support,
                color_scheme=ColorScheme.MONO.value,
                transmission_modes=transmission_modes,
                clk_pin=0,
                reserved_pins=b"\x00" * 7,
                full_update_mC=0,
                reserved=b"\x00" * 13,
            )
        ],
    )


def _device(config: GlobalConfig | None = None) -> OpenDisplayDevice:
    return OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        config=config or _config(),
        capabilities=DeviceCapabilities(width=16, height=8, color_scheme=ColorScheme.MONO),
    )


def _image(changed: bool = False) -> Image.Image:
    img = Image.new("P", (16, 8), 0)
    if changed:
        img.putpixel((13, 3), 1)
    return img


def test_no_change_image_skips_transfer(monkeypatch):
    device = _device()
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)

    async def fail_write(data: bytes) -> None:
        raise AssertionError(f"unexpected write: {data!r}")

    monkeypatch.setattr(device, "_write", fail_write)

    outcome = asyncio.run(device._maybe_upload_partial(_image(), state, None))

    assert outcome == "no_change"
    assert state.etag == 0x01020304


def test_valid_partial_never_sends_0x70_and_uses_uncompressed_0x76(monkeypatch):
    device = _device()
    old = _image()
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=old.tobytes(), width=16, height=8, bytes_per_pixel=1)
    writes: list[bytes] = []
    responses = [b"\x00\x76", b"\x00\x72", b"\x00\x73"]

    async def capture_write(data: bytes) -> None:
        writes.append(data)

    async def read_response(timeout: float) -> bytes:
        return responses.pop(0)

    monkeypatch.setattr(device, "_write", capture_write)
    monkeypatch.setattr(device, "_read", read_response)

    outcome = asyncio.run(device._maybe_upload_partial(new, state, None))

    opcodes = [int.from_bytes(w[:2], "big") for w in writes]
    assert outcome == "success"
    assert 0x70 not in opcodes
    assert opcodes == [0x76, 0x72]
    assert writes[0][2] & PARTIAL_FLAG_COMPRESSED == 0
    assert int.from_bytes(writes[0][3:7], "big") == 0x01020304
    assert int.from_bytes(writes[0][7:11], "big") == state.etag
    assert len(writes[1]) == 3


def test_empty_state_falls_back_to_full(monkeypatch):
    device = _device()
    state = PartialState()
    full_uploads = 0
    refresh_modes: list[RefreshMode] = []

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        nonlocal full_uploads
        full_uploads += 1
        refresh_modes.append(refresh_mode)
        return True  # etag committed (END-with-etag sent)

    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    asyncio.run(
        device.upload_prepared_image((b"\x00" * 16, None, _image()), refresh_mode=RefreshMode.PARTIAL, state=state)
    )

    assert full_uploads == 1
    assert refresh_modes == [RefreshMode.FULL]
    assert state.etag != 0
    assert state.last_image == _image().tobytes()


def test_auto_completed_full_upload_does_not_commit_etag(monkeypatch):
    """If the firmware auto-completes the upload (no END-with-etag), partial
    state must be invalidated rather than storing an uncommitted etag (M2)."""
    device = _device()
    state = PartialState()

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        return False  # firmware auto-completed; etag not committed

    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, _image()), state=state))

    assert state.etag == 0
    assert state.last_image is None


def test_etag_mismatch_clears_state_and_retries_full_once(monkeypatch):
    device = _device()
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    full_uploads = 0
    refresh_modes: list[RefreshMode] = []

    async def capture_write(data: bytes) -> None:
        pass

    async def read_response(timeout: float) -> bytes:
        return bytes([0xFF, 0x76, ERR_ETAG_MISMATCH, 0x00])

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        nonlocal full_uploads
        full_uploads += 1
        refresh_modes.append(refresh_mode)
        return True  # etag committed (END-with-etag sent)

    monkeypatch.setattr(device, "_write", capture_write)
    monkeypatch.setattr(device, "_read", read_response)
    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, _image(changed=True)), state=state))

    assert full_uploads == 1
    assert refresh_modes == [RefreshMode.FULL]
    assert state.etag != 0
    assert state.last_image == _image(changed=True).tobytes()


def test_non_etag_start_nack_falls_back_to_full(monkeypatch):
    """Any pre-refresh 0x76 NACK (not just etag mismatch) must fall back to a
    full upload rather than raising ProtocolError (M1)."""
    from opendisplay.partial import ERR_RECT_ALIGN

    device = _device()
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    full_uploads = 0

    async def capture_write(data: bytes) -> None:
        pass

    async def read_response(timeout: float) -> bytes:
        return bytes([0xFF, 0x76, ERR_RECT_ALIGN, 0x00])

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        nonlocal full_uploads
        full_uploads += 1
        return True

    monkeypatch.setattr(device, "_write", capture_write)
    monkeypatch.setattr(device, "_read", read_response)
    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    # Must not raise; must fall back to a single full upload.
    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, _image(changed=True)), state=state))

    assert full_uploads == 1


def test_non_mono_scheme_never_attempts_partial(monkeypatch):
    """Only MONO panels may attempt a partial update (M1)."""
    from opendisplay.partial import compute_partial_region

    state = PartialState(etag=0x01, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    for scheme in (ColorScheme.BWRY, ColorScheme.BWGBRY, ColorScheme.GRAYSCALE_16, ColorScheme.GRAYSCALE_4):
        result = compute_partial_region(_image(changed=True), state, _config(), scheme)
        assert result == "fallback_full"


def test_partial_start_respects_encrypted_payload_budget():
    """Encrypted partial START must cap its inline payload at the encrypted
    packet budget, like the compressed START (M10)."""
    from opendisplay.protocol.commands import ENCRYPTED_CHUNK_SIZE, build_direct_write_partial_start

    stream = b"\xab" * 500
    pkt_default, _ = build_direct_write_partial_start(0, 1, 0, 0, 0, 8, 8, stream_bytes=stream)
    pkt_enc, rem_enc = build_direct_write_partial_start(
        0, 1, 0, 0, 0, 8, 8, stream_bytes=stream, max_start_payload=ENCRYPTED_CHUNK_SIZE
    )
    assert len(pkt_enc) <= ENCRYPTED_CHUNK_SIZE
    assert len(pkt_enc) < len(pkt_default)


def test_partial_request_uses_partial_even_when_full_compressed_is_smaller(monkeypatch):
    device = _device(_config(transmission_modes=0x02))
    old = _image()
    new = Image.new("P", (16, 8), 1)
    state = PartialState(etag=0x01020304, last_image=old.tobytes(), width=16, height=8, bytes_per_pixel=1)
    writes: list[bytes] = []
    responses = [b"\x00\x76", b"\x00\x72", b"\x00\x73"]

    async def capture_write(data: bytes) -> None:
        writes.append(data)

    async def read_response(timeout: float) -> bytes:
        return responses.pop(0)

    async def fail_full_upload(*args, **kwargs) -> None:
        raise AssertionError("partial request unexpectedly fell back to full upload")

    monkeypatch.setattr(device, "_write", capture_write)
    monkeypatch.setattr(device, "_read", read_response)
    monkeypatch.setattr(device, "_execute_upload", fail_full_upload)

    asyncio.run(
        device.upload_prepared_image((b"\xff" * 16, b"\x01", new), refresh_mode=RefreshMode.PARTIAL, state=state)
    )

    opcodes = [int.from_bytes(w[:2], "big") for w in writes]
    assert opcodes == [0x76, 0x72]
    assert len(writes[-1]) == 3


def test_full_frame_support_expands_region_to_whole_panel():
    """partial_update_support=2 (FULL_FRAME) expands the rect to the whole panel.

    Firmware white-fills the controller RAM at partial start; on FULL_FRAME
    panels (e.g. EP426 / Seeed EN05, OpenDisplay/Firmware#80) a smaller rect
    would erase everything outside it.
    """
    from opendisplay.models.enums import PartialUpdateSupport
    from opendisplay.partial import PartialRegion, compute_partial_region

    state = PartialState(etag=0x01, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    region = compute_partial_region(
        _image(changed=True), state, _config(partial_update_support=PartialUpdateSupport.FULL_FRAME), ColorScheme.MONO
    )
    assert isinstance(region, PartialRegion)
    assert (region.rx, region.ry, region.rw, region.rh) == (0, 0, 16, 8)


def test_rect_support_keeps_minimal_region():
    """partial_update_support=1 keeps the minimal 8-aligned diff rect."""
    from opendisplay.partial import PartialRegion, compute_partial_region

    state = PartialState(etag=0x01, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    region = compute_partial_region(_image(changed=True), state, _config(partial_update_support=1), ColorScheme.MONO)
    assert isinstance(region, PartialRegion)
    # single changed pixel at (13,3) -> 8-aligned rect in the right half, not the full panel
    assert (region.rx, region.ry, region.rw, region.rh) == (8, 3, 8, 1)


def test_no_change_still_skips_transfer_on_full_frame_panels():
    """FULL_FRAME panels must still report no_change for identical frames."""
    from opendisplay.models.enums import PartialUpdateSupport
    from opendisplay.partial import compute_partial_region

    state = PartialState(etag=0x01, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    result = compute_partial_region(
        _image(), state, _config(partial_update_support=PartialUpdateSupport.FULL_FRAME), ColorScheme.MONO
    )
    assert result == "no_change"


# ─── Pipe-partial (0x0080 flags bit1) end-to-end flow ────────────────────────


def _pipe_device(
    config: GlobalConfig,
    *,
    width: int,
    height: int,
    max_queue_size: int = 16,
    blocks_per_ack: int = 8,
) -> OpenDisplayDevice:
    return OpenDisplayDevice(
        mac_address="AA:BB:CC:DD:EE:FF",
        config=config,
        capabilities=DeviceCapabilities(width=width, height=height, color_scheme=ColorScheme.MONO),
        max_queue_size=max_queue_size,
        blocks_per_ack=blocks_per_ack,
    )


def _mono(width: int, height: int, fill: int = 0) -> Image.Image:
    return Image.new("P", (width, height), fill)


def test_pipe_partial_happy_path_uncompressed():
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_ack(flags=0x03), data_ack({0}), END_ACK, REFRESH_COMPLETE])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), refresh_mode=RefreshMode.PARTIAL, state=state))

    starts = [w for w in conn.written if w[:2] == b"\x00\x80"]
    assert len(starts) == 1
    assert starts[0][3] & 0x02  # partial flag
    assert len(starts[0]) == 24  # extended packet
    assert any(w[:2] == b"\x00\x81" for w in conn.written)  # data rode the pipe
    assert all(w[:2] != b"\x00\x76" for w in conn.written)  # no legacy fallback
    ends = [w for w in conn.written if w[:2] == b"\x00\x82"]
    assert len(ends) == 1
    assert ends[0][2] == 2  # REFRESH_PARTIAL selector
    committed_etag = int.from_bytes(ends[0][3:7], "big")
    assert state.etag == committed_etag
    assert state.etag != 0x01020304  # a fresh etag was committed
    assert state.last_image == new.tobytes()


def test_pipe_partial_happy_path_compressed_zlib_window9():
    # 64x64 FULL_FRAME + streaming decompression (0x01) + pipe (0x10) → compression engages.
    cfg = _config(
        partial_update_support=PartialUpdateSupport.FULL_FRAME,
        transmission_modes=0x11,
        pixel_width=64,
        pixel_height=64,
    )
    device = _pipe_device(cfg, width=64, height=64)
    new = _mono(64, 64)
    new.putpixel((10, 10), 1)
    state = PartialState(etag=0x0A0B0C0D, last_image=_mono(64, 64).tobytes(), width=64, height=64, bytes_per_pixel=1)
    conn = ScriptedConn([start_ack(flags=0x03), data_ack({0}), END_ACK, REFRESH_COMPLETE])
    device._connection = conn

    asyncio.run(
        device.upload_prepared_image((b"\x00" * (64 * 64), None, new), refresh_mode=RefreshMode.PARTIAL, state=state)
    )

    start = next(w for w in conn.written if w[:2] == b"\x00\x80")
    assert start[3] == 0x03  # compressed + partial
    # Reassemble the streamed bytes from the 0x0081 frames → confirm zlib window 9.
    data_frames = sorted((w for w in conn.written if w[:2] == b"\x00\x81"), key=lambda w: w[2])
    payload = b"".join(w[3:] for w in data_frames)
    assert zlib_window_bits(payload) == 9


def test_pipe_partial_mid_stream_nack_falls_back_to_full(monkeypatch):
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_ack(flags=0x03), data_nack(0x03, 0, 0x0)])
    device._connection = conn
    full_uploads = 0

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        nonlocal full_uploads
        full_uploads += 1
        return True

    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert any(w[:2] == b"\x00\x80" for w in conn.written)  # pipe-partial was attempted
    assert all(w[:2] != b"\x00\x82" for w in conn.written)  # aborted before END
    assert full_uploads == 1  # clean fallback to full
    # The aborted transfer clears PartialState (plan 1.3/6) before the fallback;
    # the successful full upload then re-baselines it with a FRESH etag. Either
    # way the stale pre-NACK etag (now cleared on the device too) must be gone.
    assert state.etag != 0x01020304


def test_pipe_partial_refresh_timeout_reraises():
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_ack(flags=0x03), data_ack({0}), END_ACK, REFRESH_TIMEOUT])
    device._connection = conn

    with pytest.raises(RefreshTimeoutError, match="refresh timed out"):
        asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))


def test_pipe_partial_ack_no_bit1_falls_back_to_0x76():
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    # ACK without the partial bit → None → legacy 0x76 flow.
    conn = ScriptedConn([start_ack(flags=0x01), b"\x00\x76", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert any(w[:2] == b"\x00\x80" for w in conn.written)
    assert any(w[:2] == b"\x00\x76" for w in conn.written)  # fell back to legacy partial
    assert device._pipe_partial_supported is False  # cached negative


def test_pipe_partial_nack_05_skips_0x76_goes_full(monkeypatch):
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_nack(0x05)])
    device._connection = conn
    full_uploads = 0

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        nonlocal full_uploads
        full_uploads += 1
        return True

    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert full_uploads == 1
    assert all(w[:2] != b"\x00\x76" for w in conn.written)  # etag mismatch skips legacy partial


def test_pipe_partial_nack_06_negative_cache_skips_0x76(monkeypatch):
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_nack(0x06)])
    device._connection = conn
    full_uploads = 0

    async def execute_upload(image_data, refresh_mode, **kwargs) -> bool:
        nonlocal full_uploads
        full_uploads += 1
        return True

    monkeypatch.setattr(device, "_execute_upload", execute_upload)

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert full_uploads == 1
    assert all(w[:2] != b"\x00\x76" for w in conn.written)
    assert device._pipe_partial_supported is False  # 0x06 caches negative


def test_pipe_partial_compressed_02_retry_then_0x76():
    cfg = _config(
        partial_update_support=PartialUpdateSupport.FULL_FRAME,
        transmission_modes=0x11,
        pixel_width=64,
        pixel_height=64,
    )
    device = _pipe_device(cfg, width=64, height=64)
    new = _mono(64, 64)
    new.putpixel((10, 10), 1)
    state = PartialState(etag=0x0A0B0C0D, last_image=_mono(64, 64).tobytes(), width=64, height=64, bytes_per_pixel=1)
    # compressed 0x02 → uncompressed-still-partial 0x02 → give up → legacy 0x76.
    conn = ScriptedConn([start_nack(0x02), start_nack(0x02), b"\x00\x76", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * (64 * 64), None, new), state=state))

    starts = [w for w in conn.written if w[:2] == b"\x00\x80"]
    assert len(starts) == 2
    assert starts[0][3] == 0x03  # compressed + partial
    assert starts[1][3] == 0x02  # uncompressed, still partial
    assert device._pipe_partial_supported is False
    assert any(w[:2] == b"\x00\x76" for w in conn.written)


def test_pipe_partial_silence_falls_back_to_0x76():
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([BLETimeoutError, b"\x00\x76", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert any(w[:2] == b"\x00\x76" for w in conn.written)
    assert device._pipe_supported is False


def test_pipe_partial_negative_cache_skips_second_probe():
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8)
    device._pipe_partial_supported = False  # negative cached this connection
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([b"\x00\x76", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    # No partial-flagged 0x0080 is ever written after the negative cache.
    assert all(not (w[:2] == b"\x00\x80" and w[3] & 0x02) for w in conn.written)
    assert conn.written[0][:2] == b"\x00\x76"


# ─── Pipe-partial gates ──────────────────────────────────────────────────────


def test_gate_no_pipe_write_bit_skips_0x0080():
    device = _pipe_device(_config(transmission_modes=0x00), width=16, height=8)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([b"\x00\x76", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert all(w[:2] != b"\x00\x80" for w in conn.written)  # supports_pipe_write clear → no probe
    assert conn.written[0][:2] == b"\x00\x76"


def test_gate_max_queue_size_one_skips_0x0080():
    device = _pipe_device(_config(transmission_modes=0x10), width=16, height=8, max_queue_size=1)
    new = _image(changed=True)
    state = PartialState(etag=0x01020304, last_image=_image().tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([b"\x00\x76", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), state=state))

    assert all(w[:2] != b"\x00\x80" for w in conn.written)  # pipe disabled → no probe
    assert conn.written[0][:2] == b"\x00\x76"


def test_gate_full_frame_whole_panel_rect_and_total_size():
    cfg = _config(
        partial_update_support=PartialUpdateSupport.FULL_FRAME,
        transmission_modes=0x10,
        pixel_width=16,
        pixel_height=8,
    )
    device = _pipe_device(cfg, width=16, height=8)
    new = _mono(16, 8)
    new.putpixel((3, 3), 1)
    state = PartialState(etag=0x01020304, last_image=_mono(16, 8).tobytes(), width=16, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_ack(flags=0x03), data_ack({0}), END_ACK, REFRESH_COMPLETE])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * 16, None, new), refresh_mode=RefreshMode.PARTIAL, state=state))

    start = next(w for w in conn.written if w[:2] == b"\x00\x80")
    total_size = struct.unpack("<I", start[8:12])[0]
    assert total_size == 2 * ((16 + 7) // 8) * 8  # plane_size*2 = 32
    _old_etag, x, y, w, h = struct.unpack("<IHHHH", start[12:24])
    assert (x, y, w, h) == (0, 0, 16, 8)  # rect spans the whole panel


# ─── Pipe-partial under an encrypted session (Part 1 §1.7) ───────────────────


def test_pipe_partial_encrypted_session_happy_path():
    # 128x64 FULL_FRAME, uncompressed → 2048-byte logical stream chunked at 212.
    cfg = _config(
        partial_update_support=PartialUpdateSupport.FULL_FRAME,
        transmission_modes=0x10,
        pixel_width=128,
        pixel_height=64,
    )
    device = _pipe_device(cfg, width=128, height=64)
    _make_session(device)
    new = _mono(128, 64)
    new.putpixel((10, 10), 1)
    state = PartialState(etag=0x0A0B0C0D, last_image=_mono(128, 64).tobytes(), width=128, height=64, bytes_per_pixel=1)
    # 2048 / 212 = 10 chunks (indexes 0..9), all inside one W=16 window.
    conn = ScriptedConn([start_ack(flags=0x03), data_ack(set(range(10))), END_ACK, REFRESH_COMPLETE])
    device._connection = conn

    asyncio.run(
        device.upload_prepared_image((b"\x00" * (128 * 64), None, new), refresh_mode=RefreshMode.PARTIAL, state=state)
    )

    # Extended 0x0080 request rides as a single encrypted frame; 24 B plaintext.
    start_frame = next(w for w in conn.written if w[:2] == b"\x00\x80")
    cmd, payload = decrypt_response(device._session_key, start_frame)
    assert cmd == 0x0080
    assert len(payload) == 22  # 2 opcode + 22 = 24 B plaintext
    assert 2 + len(payload) <= 154  # ENCRYPTED_CHUNK_SIZE budget, single frame
    # 0x0081 partial data frames are sized 212 B at frame 244 (encrypted).
    data_sizes = []
    for f in (w for w in conn.written if w[:2] == b"\x00\x81"):
        _c, p = decrypt_response(device._session_key, f)
        data_sizes.append(len(p) - 1)  # minus the seq byte
    assert max(data_sizes) == 212
    # Encrypted END carries the REFRESH_PARTIAL selector + new etag.
    end_frame = next(w for w in conn.written if w[:2] == b"\x00\x82")
    _c, ep = decrypt_response(device._session_key, end_frame)
    assert ep[0] == 2
    assert int.from_bytes(ep[1:5], "big") == state.etag


def test_pipe_partial_encrypted_0x76_fallback_respects_budget():
    # No compression (0x10): an uncompressed-partial 0x02 rejects the partial flag,
    # then the encrypted 0x76 rung must cap its inline payload at the encrypted budget.
    cfg = _config(transmission_modes=0x10, pixel_width=128, pixel_height=8)
    device = _pipe_device(cfg, width=128, height=8)
    _make_session(device)
    old = _mono(128, 8, 0)
    new = _mono(128, 8, 1)  # whole panel changes → 256-byte logical stream
    state = PartialState(etag=0x01020304, last_image=old.tobytes(), width=128, height=8, bytes_per_pixel=1)
    conn = ScriptedConn([start_nack(0x02), b"\x00\x76", b"\x00\x71", b"\x00\x72", b"\x00\x73"])
    device._connection = conn

    asyncio.run(device.upload_prepared_image((b"\x00" * (128 * 8), None, new), state=state))

    assert device._pipe_partial_supported is False
    start76 = next(w for w in conn.written if w[:2] == b"\x00\x76")
    cmd, payload = decrypt_response(device._session_key, start76)
    assert cmd == 0x0076
    assert 2 + len(payload) <= 154  # capped at ENCRYPTED_CHUNK_SIZE
    assert any(w[:2] == b"\x00\x71" for w in conn.written)  # budget forced a follow-up chunk
