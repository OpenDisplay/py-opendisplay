"""Test deep_sleep() (command 0x0053) and power_off() (command 0x0052)
on OpenDisplayDevice.

Protocol 2.1 split the old single 0x0052 "deep sleep" opcode in two:
POWER_OFF (0x0052) is a hard rail-cut via the D-FF power latch, and
DEEP_SLEEP (0x0053) is the timer-wake sleep this library used to send on
0x0052.

Firmware ground truth (verified against Firmware/src/device_control.cpp,
Firmware/src/communication.cpp, and Firmware_Silabs/opendisplay_pipe.c):
- ESP32 with a D-FF power latch: replies with the command's own opcode,
  then powers off after ~100 ms.
- ESP32 without a power latch: enters deep sleep immediately with no ACK
  (deep_sleep only; power_off NACKs on latch-less boards); link drops.
- Silabs Flex: replies with the command's own opcode, then closes the
  connection and enters EM4.
- nRF: does not support either command.

In every supported case the connection drops during or right after the
command, so a disconnect/missing ACK is treated as success.
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

    async def write_command(self, cmd: bytes, response: bool = True) -> None:
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


# ---------------------------------------------------------------------------
# deep_sleep() (0x0053)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_sleep_sends_0053_and_accepts_silabs_ack() -> None:
    """Silabs Flex replies with the 2-byte 0x0053 ACK before sleeping."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x53")
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x53"]
    assert fake.read_timeout == device.TIMEOUT_ACK


@pytest.mark.asyncio
async def test_deep_sleep_accepts_esp32_power_latch_ack() -> None:
    """ESP32 with a power latch replies with the 4-byte 0x0053 0x0000 ACK."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x53\x00\x00")
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x53"]


@pytest.mark.asyncio
async def test_deep_sleep_sends_optional_duration() -> None:
    """An explicit wake-timer duration is appended as a big-endian u16."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x53")
    device._connection = fake

    await device.deep_sleep(duration_seconds=90)  # must not raise

    assert fake.written == [b"\x00\x53\x00\x5a"]


@pytest.mark.asyncio
async def test_deep_sleep_raises_on_not_supported_nack() -> None:
    """A 0xFF53 error frame surfaces as ProtocolError (deep sleep not supported)."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\xff\x53")
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

    assert fake.written == [b"\x00\x53"]
    assert fake.read_called is False  # never got to reading a response


@pytest.mark.asyncio
async def test_deep_sleep_tolerates_read_timeout() -> None:
    """A device that sleeps silently after the write leaves the ACK read to time out;
    that is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(read_error=BLETimeoutError("No response received within 5s"))
    device._connection = fake

    await device.deep_sleep()  # must not raise

    assert fake.written == [b"\x00\x53"]
    assert fake.read_called is True


@pytest.mark.asyncio
async def test_deep_sleep_tolerates_read_disconnect() -> None:
    """A device that drops the link right after acking surfaces as a read-time
    connection error; that is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(read_error=BLEConnectionError("Not connected"))
    device._connection = fake

    await device.deep_sleep()  # must not raise


# ---------------------------------------------------------------------------
# power_off() (0x0052)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_power_off_sends_0052_and_accepts_ack() -> None:
    """A board with a power latch replies with the 2-byte 0x0052 ACK before
    cutting power."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x52")
    device._connection = fake

    await device.power_off()  # must not raise

    assert fake.written == [b"\x00\x52"]
    assert fake.read_timeout == device.TIMEOUT_ACK


@pytest.mark.asyncio
async def test_power_off_raises_on_not_supported_nack() -> None:
    """A 0xFF52 error frame surfaces as ProtocolError (no power latch)."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\xff\x52")
    device._connection = fake

    with pytest.raises(ProtocolError, match="power latch"):
        await device.power_off()


@pytest.mark.asyncio
async def test_power_off_tolerates_write_drop() -> None:
    """A board that tears down BLE before the write confirms as it powers
    off; that disconnect is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(write_error=BLEConnectionError("Write failed: ... error=133"))
    device._connection = fake

    await device.power_off()  # must not raise

    assert fake.written == [b"\x00\x52"]
    assert fake.read_called is False


@pytest.mark.asyncio
async def test_power_off_tolerates_read_disconnect() -> None:
    """A board that drops the link right after acking surfaces as a
    read-time connection error; that is expected and must not raise."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(read_error=BLEConnectionError("Not connected"))
    device._connection = fake

    await device.power_off()  # must not raise
