"""Test LED activate API on OpenDisplayDevice."""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import ProtocolError
from opendisplay.models.led_flash import LedFlashConfig


class _FakeConnection:
    def __init__(self, response: bytes | list[bytes]):
        if isinstance(response, list):
            self._responses = response[:]
        else:
            self._responses = [response]
        self.written: list[bytes] = []
        self.read_timeout: float | None = None

    async def write_command(self, cmd: bytes, response: bool = True) -> None:
        self.written.append(cmd)

    async def read_response(self, timeout: float) -> bytes:
        self.read_timeout = timeout
        if not self._responses:
            raise RuntimeError("No fake responses left")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_activate_led_sends_0073_and_validates_ack() -> None:
    """activate_led should send 0x0073 and accept normal ACK response."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x73\x00\x00")
    device._connection = fake  # Inject fake connection
    device._fw_version = {"major": 1, "minor": 0, "sha": "f378685"}
    flash_config = LedFlashConfig.single(color=0xE0, flash_count=1, brightness=8)

    response = await device.activate_led(led_instance=2, flash_config=flash_config)

    assert fake.written == [b"\x00\x73\x02" + flash_config.to_bytes()]
    assert fake.read_timeout == device.TIMEOUT_REFRESH
    assert response == b"\x00\x73\x00\x00"


@pytest.mark.asyncio
async def test_activate_led_with_flash_config_payload() -> None:
    """activate_led should include optional typed flash config payload."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x80\x73")
    device._connection = fake  # Inject fake connection
    device._fw_version = {"major": 1, "minor": 0, "sha": "f378685"}
    flash_config = LedFlashConfig.single(color=0xE0, flash_count=2, brightness=8)

    await device.activate_led(led_instance=1, flash_config=flash_config, timeout=12.5)

    assert fake.written == [b"\x00\x73\x01" + flash_config.to_bytes()]
    assert fake.read_timeout == 12.5


@pytest.mark.asyncio
async def test_activate_led_maps_firmware_error_response() -> None:
    """Firmware LED errors (0xFF73) should raise ProtocolError with code."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\xff\x73\x02\x00")
    device._connection = fake  # Inject fake connection
    device._fw_version = {"major": 1, "minor": 0, "sha": "f378685"}
    flash_config = LedFlashConfig.single(color=0xE0)

    with pytest.raises(ProtocolError, match="error code 0x02"):
        await device.activate_led(led_instance=0, flash_config=flash_config)


@pytest.mark.asyncio
async def test_activate_led_requires_connection() -> None:
    """activate_led should fail fast when not connected."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")

    flash_config = LedFlashConfig.single(color=0xE0)

    with pytest.raises(RuntimeError, match="not connected"):
        await device.activate_led(led_instance=0, flash_config=flash_config)


@pytest.mark.asyncio
async def test_activate_led_blocks_legacy_firmware_before_sending_command() -> None:
    """activate_led should fail fast on firmware versions below 1.0."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    # First response is firmware version (0.68)
    fake = _FakeConnection(response=b"\x00\x43\x00\x44\x07legacy1")
    device._connection = fake  # Inject fake connection
    flash_config = LedFlashConfig.single(color=0xE0)

    with pytest.raises(ProtocolError, match="requires firmware >= 1.0"):
        await device.activate_led(led_instance=0, flash_config=flash_config)

    # Should only have sent READ_FW_VERSION (0x0043), not LED_ACTIVATE (0x0073)
    assert fake.written == [b"\x00\x43"]
