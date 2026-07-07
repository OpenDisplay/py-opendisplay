"""Test deep_sleep() (command 0x0052) on OpenDisplayDevice.

Firmware ground truth (verified against Firmware/src/device_control.cpp,
Firmware/src/communication.cpp, and Firmware_Silabs/opendisplay_pipe.c):
- ESP32 with a D-FF power latch: replies 0x0052, then powers off after ~100 ms.
- ESP32 without a power latch: enters deep sleep immediately with no ACK; link drops.
- Silabs Flex: replies 0x0052, then closes the connection and enters EM4.
- nRF: does not support the command.

In every supported case the connection drops during or right after the command,
so a disconnect/missing ACK is treated as success.
"""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import BLEConnectionError, BLETimeoutError, ProtocolError


class _FakeConnection:
    def __init__(
        self,
        response: bytes | None = None,
        *,
        write_error: Exception | None = None,
        read_error: Exception | None = None,
    ):
        self._response = response
        self._write_error = write_error
        self._read_error = read_error
        self.written: list[bytes] = []
        self.read_timeout: float | None = None
        self.read_called = False

    async def write_command(self, cmd: bytes) -> None:
        self.written.append(cmd)
        if self._write_error is not None:
            raise self._write_error

    async def read_response(self, timeout: float) -> bytes:
        self.read_called = True
        self.read_timeout = timeout
        if self._read_error is not None:
            raise self._read_error
        assert self._response is not None
        return self._response


@pytest.mark.asyncio
async def test_deep_sleep_sends_0052_and_accepts_silabs_ack() -> None:
    """Silabs Flex replies with the 2-byte 0x0052 ACK before sleeping."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x52")
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x52"]
    assert fake.read_timeout == device.TIMEOUT_ACK


@pytest.mark.asyncio
async def test_deep_sleep_accepts_esp32_power_latch_ack() -> None:
    """ESP32 with a power latch replies with the 4-byte 0x0052 0x0000 ACK."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x52\x00\x00")
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x52"]


@pytest.mark.asyncio
async def test_deep_sleep_raises_on_not_supported_nack() -> None:
    """A 0xFF52 error frame surfaces as ProtocolError (deep sleep not supported)."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\xff\x52")
    device._connection = fake

    with pytest.raises(ProtocolError, match="not supported"):
        await device.deep_sleep()


@pytest.mark.asyncio
async def test_deep_sleep_tolerates_write_drop() -> None:
    """An ESP32 without a power latch tears down BLE before the write confirms;
    that disconnect is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(write_error=BLEConnectionError("Write failed: ... error=133"))
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x52"]
    assert fake.read_called is False  # never got to reading a response


@pytest.mark.asyncio
async def test_deep_sleep_tolerates_read_timeout() -> None:
    """A device that sleeps silently after the write leaves the ACK read to time out;
    that is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(read_error=BLETimeoutError("No response received within 5s"))
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x52"]
    assert fake.read_called is True


@pytest.mark.asyncio
async def test_deep_sleep_tolerates_read_disconnect() -> None:
    """A device that drops the link right after acking surfaces as a read-time
    connection error; that is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(read_error=BLEConnectionError("Not connected"))
    device._connection = fake

    await device.deep_sleep()  # must not raise
