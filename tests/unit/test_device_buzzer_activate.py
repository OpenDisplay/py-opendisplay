"""Test buzzer activate API on OpenDisplayDevice."""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import ProtocolError
from opendisplay.models.buzzer_activate import BuzzerActivateConfig


class _FakeConnection:
    def __init__(self, response: bytes | list[bytes]):
        if isinstance(response, list):
            self._responses = response[:]
        else:
            self._responses = [response]
        self.written: list[bytes] = []
        self.read_timeout: float | None = None

    async def write_command(self, cmd: bytes) -> None:
        self.written.append(cmd)

    async def read_response(self, timeout: float) -> bytes:
        self.read_timeout = timeout
        if not self._responses:
            raise RuntimeError("No fake responses left")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_activate_buzzer_sends_0075_and_validates_ack() -> None:
    """activate_buzzer should send 0x0075 and accept a normal ACK."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)
    fake = _FakeConnection(response=b"\x00\x75\x00\x00")
    device._connection = fake
    device._fw_version = {"major": 1, "minor": 0, "sha": "abc1234"}

    response = await device.activate_buzzer(buzzer_instance=0, config=config)

    expected_cmd = b"\x00\x75\x00" + config.to_bytes()
    assert fake.written == [expected_cmd]
    assert fake.read_timeout == device.TIMEOUT_REFRESH
    assert response == b"\x00\x75\x00\x00"


@pytest.mark.asyncio
async def test_activate_buzzer_custom_timeout() -> None:
    """activate_buzzer should use the supplied timeout."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    config = BuzzerActivateConfig.single_tone(frequency_hz=440, duration_ms=500)
    fake = _FakeConnection(response=b"\x80\x75")
    device._connection = fake
    device._fw_version = {"major": 1, "minor": 3, "sha": "def5678"}

    await device.activate_buzzer(buzzer_instance=1, config=config, timeout=5.0)

    assert fake.read_timeout == 5.0


@pytest.mark.asyncio
async def test_activate_buzzer_requires_connection() -> None:
    """activate_buzzer should raise RuntimeError when not connected."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)

    with pytest.raises(RuntimeError, match="not connected"):
        await device.activate_buzzer(buzzer_instance=0, config=config)


@pytest.mark.asyncio
async def test_activate_buzzer_blocks_legacy_firmware() -> None:
    """activate_buzzer should raise ProtocolError on firmware < 1.0."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    # Firmware version response for 0.68
    fake = _FakeConnection(response=b"\x00\x43\x00\x44\x07legacy1")
    device._connection = fake
    config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)

    with pytest.raises(ProtocolError, match="requires firmware >= 1.0"):
        await device.activate_buzzer(buzzer_instance=0, config=config)

    # Should only have sent READ_FW_VERSION (0x0043), not BUZZER_ACTIVATE (0x0075)
    assert fake.written == [b"\x00\x43"]
