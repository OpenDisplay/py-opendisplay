"""Test that upload methods respect the device's supports_zip capability."""

from __future__ import annotations

import pytest
from epaper_dithering import ColorScheme
from PIL import Image

from opendisplay import OpenDisplayDevice
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.config import (
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.protocol.commands import MAX_COMPRESSED_SIZE


def _config(transmission_modes: int = 0x02, width: int = 2, height: int = 2) -> GlobalConfig:
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


def _make_device(config: GlobalConfig | None = None) -> OpenDisplayDevice:
    caps = DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO)
    return OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=config, capabilities=caps)


class TestUploadImageCompressionDecision:
    """upload_image() should use compressed protocol only when device supports it."""

    def _fake_prepare(self, raw: bytes, compressed: bytes | None):
        img = Image.new("P", (2, 2))
        return lambda *a, **kw: (raw, compressed, img)

    def _capture_execute(self) -> tuple[dict, object]:
        captured: dict = {}

        async def fake_execute(image_data, refresh_mode, use_compression=False, **kwargs):
            captured["use_compression"] = use_compression

        return captured, fake_execute

    @pytest.mark.asyncio
    async def test_uses_compression_when_device_supports_zip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x02))
        raw, compressed = b"\x01" * 100, b"\x02" * 10
        monkeypatch.setattr(device, "_prepare_image", self._fake_prepare(raw, compressed))
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_image(Image.new("RGB", (2, 2)))

        assert captured["use_compression"] is True

    @pytest.mark.asyncio
    async def test_skips_compression_when_device_does_not_support_zip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x00))
        raw, compressed = b"\x01" * 100, b"\x02" * 10
        monkeypatch.setattr(device, "_prepare_image", self._fake_prepare(raw, compressed))
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_image(Image.new("RGB", (2, 2)))

        assert captured["use_compression"] is False

    @pytest.mark.asyncio
    async def test_skips_compression_when_compress_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x02))
        raw = b"\x01" * 100
        monkeypatch.setattr(device, "_prepare_image", self._fake_prepare(raw, None))
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_image(Image.new("RGB", (2, 2)), compress=False)

        assert captured["use_compression"] is False

    @pytest.mark.asyncio
    async def test_skips_compression_when_data_exceeds_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x02))
        raw = b"\x01" * 100
        compressed = b"\x02" * (MAX_COMPRESSED_SIZE + 1)
        monkeypatch.setattr(device, "_prepare_image", self._fake_prepare(raw, compressed))
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_image(Image.new("RGB", (2, 2)))

        assert captured["use_compression"] is False

    @pytest.mark.asyncio
    async def test_defaults_to_compression_when_no_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a GlobalConfig, compression should be attempted (backward compat)."""
        device = _make_device(config=None)
        raw, compressed = b"\x01" * 100, b"\x02" * 10
        monkeypatch.setattr(device, "_prepare_image", self._fake_prepare(raw, compressed))
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_image(Image.new("RGB", (2, 2)))

        assert captured["use_compression"] is True


class TestUploadPreparedImageCompressionDecision:
    """upload_prepared_image() should use compressed protocol only when device supports it."""

    def _prepared(self, compressed: bytes | None) -> tuple[bytes, bytes | None, Image.Image]:
        return b"\x01" * 100, compressed, Image.new("P", (2, 2))

    def _capture_execute(self) -> tuple[dict, object]:
        captured: dict = {}

        async def fake_execute(image_data, refresh_mode, use_compression=False, **kwargs):
            captured["use_compression"] = use_compression

        return captured, fake_execute

    @pytest.mark.asyncio
    async def test_uses_compression_when_device_supports_zip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x02))
        prepared = self._prepared(compressed=b"\x02" * 10)
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_prepared_image(prepared)

        assert captured["use_compression"] is True

    @pytest.mark.asyncio
    async def test_skips_compression_when_device_does_not_support_zip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x00))
        prepared = self._prepared(compressed=b"\x02" * 10)
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_prepared_image(prepared)

        assert captured["use_compression"] is False

    @pytest.mark.asyncio
    async def test_skips_compression_when_compress_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x02))
        prepared = self._prepared(compressed=b"\x02" * 10)
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_prepared_image(prepared, compress=False)

        assert captured["use_compression"] is False

    @pytest.mark.asyncio
    async def test_skips_compression_when_data_exceeds_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_device(config=_config(transmission_modes=0x02))
        prepared = self._prepared(compressed=b"\x02" * (MAX_COMPRESSED_SIZE + 1))
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_prepared_image(prepared)

        assert captured["use_compression"] is False

    @pytest.mark.asyncio
    async def test_defaults_to_compression_when_no_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a GlobalConfig, compression should be attempted (backward compat)."""
        device = _make_device(config=None)
        prepared = self._prepared(compressed=b"\x02" * 10)
        captured, fake_execute = self._capture_execute()
        monkeypatch.setattr(device, "_execute_upload", fake_execute)

        await device.upload_prepared_image(prepared)

        assert captured["use_compression"] is True
