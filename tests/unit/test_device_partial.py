"""Client-flow tests for streamed partial uploads."""

from __future__ import annotations

import asyncio

from epaper_dithering import ColorScheme
from PIL import Image

from opendisplay import OpenDisplayDevice
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.config import DisplayConfig, GlobalConfig, ManufacturerData, PowerOption, SystemConfig
from opendisplay.models.enums import RefreshMode
from opendisplay.partial import ERR_ETAG_MISMATCH, PARTIAL_FLAG_COMPRESSED, PartialState


def _config(partial_update_support: int = 1, transmission_modes: int = 0x00) -> GlobalConfig:
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
                pixel_width=16,
                pixel_height=8,
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
