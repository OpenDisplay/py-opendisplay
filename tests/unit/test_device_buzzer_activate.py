"""Test buzzer activate API on OpenDisplayDevice."""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.models.buzzer_activate import BuzzerActivateConfig


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
async def test_activate_buzzer_sends_0077_and_validates_ack() -> None:
    """activate_buzzer should send 0x0077 and accept a normal ACK."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)
    fake = _FakeConnection(response=b"\x00\x77\x00\x00")
    device._connection = fake

    response = await device.activate_buzzer(buzzer_instance=0, config=config)

    expected_cmd = b"\x00\x77\x00" + config.to_bytes()
    assert fake.written == [expected_cmd]
    assert fake.read_timeout == device.TIMEOUT_REFRESH
    assert response == b"\x00\x77\x00\x00"


@pytest.mark.asyncio
async def test_activate_buzzer_custom_timeout() -> None:
    """activate_buzzer should use the supplied timeout."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    config = BuzzerActivateConfig.single_tone(frequency_hz=440, duration_ms=500)
    fake = _FakeConnection(response=b"\x80\x77")
    device._connection = fake

    await device.activate_buzzer(buzzer_instance=1, config=config, timeout=5.0)

    assert fake.read_timeout == 5.0


@pytest.mark.asyncio
async def test_activate_buzzer_requires_connection() -> None:
    """activate_buzzer should raise RuntimeError when not connected."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)

    with pytest.raises(RuntimeError, match="not connected"):
        await device.activate_buzzer(buzzer_instance=0, config=config)
