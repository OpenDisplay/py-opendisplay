"""Device flows must treat a TCP/LAN timeout or link drop like the BLE one.

``TcpTransport`` raises the transport-neutral ``OpenDisplayTimeoutError`` /
``OpenDisplayConnectionError``; only ``BLEConnection`` raises the ``BLE*``
subclasses. Each recovery path in ``OpenDisplayDevice`` has to catch the base
class, or it only works over BLE.
"""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import (
    NfcNotSupportedError,
    OpenDisplayConnectionError,
    OpenDisplayTimeoutError,
    TruncatedConfigError,
)
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme, NfcRecordType


def _device(transport: object) -> OpenDisplayDevice:
    device = OpenDisplayDevice(
        transport=transport,  # type: ignore[arg-type]
        capabilities=DeviceCapabilities(width=8, height=8, color_scheme=ColorScheme.MONO),
    )
    device._connection = transport  # type: ignore[assignment]
    return device


async def test_interrogate_reports_truncated_config_on_neutral_timeout(fake_transport) -> None:
    # First chunk announces 100 bytes but carries 10; then the link goes quiet.
    first_chunk = b"\x00\x40" + b"\x00\x00" + (100).to_bytes(2, "little") + b"\x01" * 10
    device = _device(fake_transport([first_chunk, OpenDisplayTimeoutError("quiet")]))

    with pytest.raises(TruncatedConfigError):
        await device.interrogate()


async def test_deep_sleep_tolerates_neutral_timeout_instead_of_ack(fake_transport) -> None:
    transport = fake_transport([OpenDisplayTimeoutError("asleep")])
    device = _device(transport)

    await device.deep_sleep()

    assert transport.written[0][:2] == b"\x00\x53"


async def test_power_off_tolerates_neutral_link_drop_instead_of_ack(fake_transport) -> None:
    transport = fake_transport([OpenDisplayConnectionError("closed")])
    device = _device(transport)

    await device.power_off()

    assert transport.written[0][:2] == b"\x00\x52"


@pytest.mark.parametrize("method", ["deep_sleep", "power_off", "trigger_dfu_bootloader"])
async def test_link_drop_during_write_is_not_an_error(fake_transport, method: str) -> None:
    transport = fake_transport()

    async def dropping_write(data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        raise OpenDisplayConnectionError("link dropped while writing")

    transport.write_command = dropping_write
    device = _device(transport)

    await getattr(device, method)()


async def test_nfc_write_reports_unsupported_on_neutral_timeout(fake_transport) -> None:
    device = _device(fake_transport([OpenDisplayTimeoutError("silent")]))

    with pytest.raises(NfcNotSupportedError):
        await device.write_nfc(NfcRecordType.URI, b"https://opendisplay.org")
