"""Tests for the upload wire protocol: timeouts, fallbacks, encrypted sessions.

These tests inject a fake BLE connection and exercise _execute_upload / _read
directly so that the correct firmware ACK bytes and timeouts are verified
without requiring real hardware.

ACK constants derived from display_service.cpp (sendResponse echoes plain
2-byte command code for all direct-write responses):
  0x0070 START ACK   0x0071 DATA ACK
  0x0072 END ACK     0x0073 REFRESH_COMPLETE
  0xFF,0xFF          error (no compressed buffer / overflow)
"""

from __future__ import annotations

import pytest
from epaper_dithering import ColorScheme

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import AuthenticationRequiredError
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.config import (
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.models.enums import FitMode, RefreshMode
from opendisplay.protocol.commands import ENCRYPTED_CHUNK_SIZE

# Firmware ACK bytes (2-byte plaintext command echoes, sent unencrypted)
ACK_START = b"\x00\x70"
ACK_DATA = b"\x00\x71"
ACK_END = b"\x00\x72"
ACK_REFRESH = b"\x00\x73"
ERR_FRAME = b"\xff\xff"


class _FakeConnection:
    """Replays scripted responses; records written commands and read timeouts."""

    def __init__(self, responses: list[bytes]) -> None:
        self.written: list[bytes] = []
        self._responses = list(responses)
        self.timeouts: list[float] = []

    async def write_command(self, data: bytes) -> None:
        self.written.append(data)

    async def read_response(self, timeout: float) -> bytes:
        self.timeouts.append(timeout)
        if not self._responses:
            raise RuntimeError("_FakeConnection: no responses left")
        return self._responses.pop(0)


def _make_config(transmission_modes: int = 0x02, width: int = 4, height: int = 4) -> GlobalConfig:
    return GlobalConfig(
        system=SystemConfig(ic_type=0, communication_modes=0, device_flags=0, pwr_pin=0xFF, reserved=b"\x00" * 17),
        manufacturer=ManufacturerData(manufacturer_id=0, board_type=0, board_revision=0, reserved=b"\x00" * 18),
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
                pixel_width=width,
                pixel_height=height,
                active_width_mm=10,
                active_height_mm=10,
                tag_type=0,
                rotation=0,
                reset_pin=0xFF,
                busy_pin=0xFF,
                dc_pin=0xFF,
                cs_pin=0xFF,
                data_pin=0,
                partial_update_support=0,
                color_scheme=ColorScheme.MONO.value,
                transmission_modes=transmission_modes,
                clk_pin=0,
                reserved_pins=b"\x00" * 7,
                full_update_mC=0,
                reserved=b"\x00" * 13,
            )
        ],
    )


def _make_device(transmission_modes: int = 0x02, width: int = 4, height: int = 4) -> OpenDisplayDevice:
    config = _make_config(transmission_modes=transmission_modes, width=width, height=height)
    caps = DeviceCapabilities(width=width, height=height, color_scheme=ColorScheme.MONO)
    return OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config, capabilities=caps)


# ─── _read(): encrypted session behaviour ────────────────────────────────────


@pytest.mark.asyncio
async def test_read_short_frame_passthrough_during_session() -> None:
    """Direct-write ACKs (2 bytes) are sent unencrypted even in authenticated sessions."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._session_key = b"\x00" * 16
    device._connection = _FakeConnection([ACK_START])
    result = await device._read(timeout=5.0)
    assert result == ACK_START


@pytest.mark.asyncio
async def test_read_error_frame_passthrough_during_session() -> None:
    """0xFF,0xFF error frame is 2 bytes and must not trigger a decryption attempt."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._session_key = b"\x00" * 16
    device._connection = _FakeConnection([ERR_FRAME])
    result = await device._read(timeout=5.0)
    assert result == ERR_FRAME


@pytest.mark.asyncio
async def test_read_3byte_0xfe_raises_authentication_required() -> None:
    """3-byte response ending in 0xFE signals authentication required."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._connection = _FakeConnection([b"\x00\x43\xfe"])
    with pytest.raises(AuthenticationRequiredError):
        await device._read(timeout=5.0)


@pytest.mark.asyncio
async def test_read_long_frame_decrypted_during_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames ≥31 bytes are decrypted; result is reconstructed as cmd + payload."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._session_key = b"\xab" * 16
    long_frame = b"\x00" * 31  # len >= _ENCRYPTED_RESPONSE_MIN_LEN
    device._connection = _FakeConnection([long_frame])
    monkeypatch.setattr("opendisplay.device.decrypt_response", lambda key, raw: (0x0070, b""))
    result = await device._read(timeout=5.0)
    assert result == ACK_START


# ─── Uncompressed upload path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uncompressed_data_chunks_use_90s_timeout() -> None:
    """DATA chunk ACKs must use TIMEOUT_UNCOMPRESSED_DATA_ACK (90s) — not the 5s general ACK."""
    image_data = b"\x00" * 10
    device = _make_device()
    fake = _FakeConnection([ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(image_data, RefreshMode.FULL, use_compression=False)
    assert fake.timeouts[0] == device.TIMEOUT_FIRST_CHUNK
    assert fake.timeouts[1] == device.TIMEOUT_UNCOMPRESSED_DATA_ACK
    assert fake.timeouts[2] == device.TIMEOUT_ACK
    assert fake.timeouts[3] == device.TIMEOUT_REFRESH


@pytest.mark.asyncio
async def test_uncompressed_auto_completed_skips_end_write() -> None:
    """When device sends 0x72 instead of 0x71, no explicit END should be written."""
    image_data = b"\x00" * 10
    device = _make_device()
    # Device replies with ACK_END to the DATA chunk (buffer full → auto-refresh)
    fake = _FakeConnection([ACK_START, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(image_data, RefreshMode.FULL, use_compression=False)
    # Only START write + 1 DATA write; no END write
    assert len(fake.written) == 2
    assert fake.written[0][:2] == b"\x00\x70"  # START
    assert fake.written[1][:2] == b"\x00\x71"  # DATA


@pytest.mark.asyncio
async def test_uncompressed_full_sequence() -> None:
    """Full uncompressed upload: START → DATA → END → REFRESH."""
    image_data = b"\x00" * 10
    device = _make_device()
    fake = _FakeConnection([ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(image_data, RefreshMode.FULL, use_compression=False)
    assert len(fake.written) == 3
    assert fake.written[0][:2] == b"\x00\x70"  # START
    assert fake.written[1][:2] == b"\x00\x71"  # DATA
    assert fake.written[2][:2] == b"\x00\x72"  # END


# ─── Compressed upload path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compressed_end_ack_uses_90s_timeout() -> None:
    """Compressed END triggers blocking SPI write (~60s on Spectra/ACeP); ACK must wait 90s."""
    image_data = b"\x00" * 100
    compressed = b"\xff" * 5  # Tiny — fits entirely in START payload
    device = _make_device()
    fake = _FakeConnection([ACK_START, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(
        image_data,
        RefreshMode.FULL,
        use_compression=True,
        compressed_data=compressed,
        uncompressed_size=len(image_data),
    )
    # Timeouts: [TIMEOUT_FIRST_CHUNK, TIMEOUT_COMPRESSED_END_ACK, TIMEOUT_REFRESH]
    assert fake.timeouts[1] == device.TIMEOUT_COMPRESSED_END_ACK
    assert fake.timeouts[1] == 90.0


@pytest.mark.asyncio
async def test_compressed_data_chunk_acks_use_5s_timeout() -> None:
    """Compressed DATA chunk ACKs use TIMEOUT_ACK (5s) — data is buffered, not written to SPI."""
    image_data = b"\x00" * 500
    # 300B > 194B (MAX_START_PAYLOAD - 6) → START gets 194B, remaining 106B sent as DATA chunk
    compressed = b"\xff" * 300
    device = _make_device()
    fake = _FakeConnection([ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(
        image_data,
        RefreshMode.FULL,
        use_compression=True,
        compressed_data=compressed,
        uncompressed_size=len(image_data),
    )
    # Timeouts: [FIRST_CHUNK, ACK (data), COMPRESSED_END_ACK, REFRESH]
    assert fake.timeouts[1] == device.TIMEOUT_ACK
    assert fake.timeouts[2] == device.TIMEOUT_COMPRESSED_END_ACK


@pytest.mark.asyncio
async def test_compressed_start_payload_capped_at_encrypted_chunk_size_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With active session, START payload must not exceed ENCRYPTED_CHUNK_SIZE (154B)."""
    # 190B: fits in 200B limit (no overflow), but overflows 154B limit (42B remaining)
    image_data = b"\x00" * 500
    compressed = b"\xff" * 190
    device = _make_device()
    device._session_key = b"\x00" * 16
    monkeypatch.setattr("opendisplay.device.encrypt_command", lambda key, data: data)
    fake = _FakeConnection([ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(
        image_data,
        RefreshMode.FULL,
        use_compression=True,
        compressed_data=compressed,
        uncompressed_size=len(image_data),
    )
    # Without session: 190B fits in START (194 slots) → only 2 writes (START + END)
    # With session: only 148 slots in START → 42B overflow → 3 writes (START + DATA + END)
    assert len(fake.written) == 3
    start_cmd = fake.written[0]
    # cmd(2) + size(4) + 148B data = 154B exactly
    assert len(start_cmd) == ENCRYPTED_CHUNK_SIZE


@pytest.mark.asyncio
async def test_compressed_full_sequence_small_payload() -> None:
    """Full compressed upload where all data fits in START: START → END → REFRESH."""
    image_data = b"\x00" * 100
    compressed = b"\xff" * 5
    device = _make_device()
    fake = _FakeConnection([ACK_START, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(
        image_data,
        RefreshMode.FULL,
        use_compression=True,
        compressed_data=compressed,
        uncompressed_size=len(image_data),
    )
    assert len(fake.written) == 2
    assert fake.written[0][:2] == b"\x00\x70"  # START (with size + compressed data)
    assert fake.written[1][:2] == b"\x00\x72"  # END


# ─── Compression fallback ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compressed_start_rejected_retries_uncompressed() -> None:
    """When device rejects compressed START (0xFF,0xFF), fall back to bare 0x0070 START."""
    image_data = b"\x00" * 10
    compressed = b"\xff" * 5
    device = _make_device()
    # Device rejects compressed START, then accepts bare START
    fake = _FakeConnection([ERR_FRAME, ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(
        image_data,
        RefreshMode.FULL,
        use_compression=True,
        compressed_data=compressed,
        uncompressed_size=len(image_data),
    )
    # First written cmd: compressed START (has size + data embedded)
    assert len(fake.written[0]) > 2
    assert fake.written[0][:2] == b"\x00\x70"
    # Second written cmd: bare uncompressed START (just 2 bytes)
    assert fake.written[1] == b"\x00\x70"


@pytest.mark.asyncio
async def test_after_fallback_data_chunks_use_uncompressed_timeout() -> None:
    """After fallback to uncompressed, DATA chunk ACKs must use 90s timeout."""
    image_data = b"\x00" * 10
    compressed = b"\xff" * 5
    device = _make_device()
    fake = _FakeConnection([ERR_FRAME, ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._connection = fake
    await device._execute_upload(
        image_data,
        RefreshMode.FULL,
        use_compression=True,
        compressed_data=compressed,
        uncompressed_size=len(image_data),
    )
    # timeouts: [FIRST_CHUNK(err), FIRST_CHUNK(retry), UNCOMPRESSED_DATA_ACK, ACK, REFRESH]
    assert device.TIMEOUT_UNCOMPRESSED_DATA_ACK in fake.timeouts


# ─── _dispatch_upload: ZIPXL extended size limit ──────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_zipxl_accepts_large_compressed_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """ZIPXL devices (bit 0x01) use a 512KB limit instead of 50KB."""
    from opendisplay.protocol.commands import MAX_COMPRESSED_SIZE, MAX_COMPRESSED_SIZE_ZIPXL

    # transmission_modes=0x03 → ZIPXL (bit 0) + ZIP (bit 1)
    device = _make_device(transmission_modes=0x03)
    # 60KB: larger than MAX_COMPRESSED_SIZE (50KB) but within MAX_COMPRESSED_SIZE_ZIPXL (512KB)
    compressed_size = MAX_COMPRESSED_SIZE + 10 * 1024
    assert compressed_size < MAX_COMPRESSED_SIZE_ZIPXL
    captured: dict = {}

    async def fake_execute(image_data, refresh_mode, use_compression=False, **kwargs):
        captured["use_compression"] = use_compression

    monkeypatch.setattr(device, "_execute_upload", fake_execute)
    image_data = b"\x00" * 100
    compressed_data = b"\xff" * compressed_size
    await device._dispatch_upload(image_data, RefreshMode.FULL, True, compressed_data, None)
    assert captured["use_compression"] is True


# ─── _execute_upload: error paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_upload_raises_valueerror_when_compression_args_missing() -> None:
    """use_compression=True without compressed_data/uncompressed_size must raise ValueError."""
    device = _make_device()
    device._connection = _FakeConnection([])
    with pytest.raises(ValueError, match="uncompressed_size and compressed_data are required"):
        await device._execute_upload(b"\x00" * 10, RefreshMode.FULL, use_compression=True)


@pytest.mark.asyncio
async def test_execute_upload_reraises_when_uncompressed_start_rejected() -> None:
    """InvalidResponseError from an uncompressed START is not swallowed (no fallback to try)."""
    from opendisplay.exceptions import InvalidResponseError

    device = _make_device()
    device._connection = _FakeConnection([ERR_FRAME])
    with pytest.raises(InvalidResponseError):
        await device._execute_upload(b"\x00" * 10, RefreshMode.FULL, use_compression=False)


@pytest.mark.asyncio
async def test_execute_upload_raises_on_refresh_timeout_response() -> None:
    """Device sending 0x74 (refresh timed out) must surface as ProtocolError."""
    from opendisplay.exceptions import ProtocolError

    image_data = b"\x00" * 10
    device = _make_device()
    # Minimal sequence up to the refresh wait, then device sends 0x74
    fake = _FakeConnection([ACK_START, ACK_DATA, ACK_END, b"\x00\x74"])
    device._connection = fake
    with pytest.raises(ProtocolError, match="refresh timed out"):
        await device._execute_upload(image_data, RefreshMode.FULL, use_compression=False)


@pytest.mark.asyncio
async def test_execute_upload_raises_on_unexpected_refresh_response() -> None:
    """Any non-0x73 / non-0x74 response while waiting for refresh must surface as ProtocolError."""
    from opendisplay.exceptions import ProtocolError

    image_data = b"\x00" * 10
    device = _make_device()
    fake = _FakeConnection([ACK_START, ACK_DATA, ACK_END, b"\x00\x43"])  # READ_FW_VERSION echo
    device._connection = fake
    with pytest.raises(ProtocolError, match="Unexpected response"):
        await device._execute_upload(image_data, RefreshMode.FULL, use_compression=False)


# ─── DisplayConfig.supports_raw alias ────────────────────────────────────────


def test_display_config_supports_raw_aliases_supports_zipxl() -> None:
    """supports_raw is an alias for supports_zipxl (bit 0x01)."""
    config = _make_config(transmission_modes=0x01)
    display_cfg = config.displays[0]
    assert display_cfg.supports_zipxl is True
    assert display_cfg.supports_raw is True

    config_no = _make_config(transmission_modes=0x02)
    display_cfg_no = config_no.displays[0]
    assert display_cfg_no.supports_zipxl is False
    assert display_cfg_no.supports_raw is False


# ─── _prepare_image: default FitMode ─────────────────────────────────────────


def test_prepare_image_default_fit_is_contain(monkeypatch: pytest.MonkeyPatch) -> None:
    """_prepare_image() must default to CONTAIN (letterbox), not STRETCH (distort)."""
    import inspect

    from opendisplay.device import OpenDisplayDevice as _Dev

    sig = inspect.signature(_Dev._prepare_image)
    default_fit = sig.parameters["fit"].default
    assert default_fit is FitMode.CONTAIN, (
        f"_prepare_image default fit is {default_fit!r}, expected FitMode.CONTAIN. "
        "STRETCH would distort non-matching aspect ratios."
    )


def test_prepare_image_defaults_tone_and_gamut_off() -> None:
    """_prepare_image() should leave tone/gamut compression disabled unless requested."""
    import inspect

    from opendisplay.device import OpenDisplayDevice as _Dev

    sig = inspect.signature(_Dev._prepare_image)
    assert sig.parameters["tone"].default == 0.0
    assert sig.parameters["gamut"].default == 0.0
