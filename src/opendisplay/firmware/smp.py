"""mcumgr SMP over Bluetooth: upload an image, mark it for a test boot, reset, confirm.

Firmware 3.0 updates ESP32, nRF52840 and nRF54 this way (Firmware_Unified ``docs/UPGRADING.md``:
"mcumgr SMP upload of ``opendisplay-<board>-app.bin``"). The protocol is ``smpclient``'s (the
optional ``smp`` extra). The connection is the library's own: ``smpclient``'s transport finds
the device by scanning, which neither Home Assistant nor an ESPHome Bluetooth proxy can use, so
:class:`DeviceSMPTransport` connects to a given ``BLEDevice`` through ``bleak-retry-connector``,
as :class:`~opendisplay.transport.BLEConnection` does.

An image uploaded and marked "test" boots once; if it never confirms, the bootloader returns to
the old image. The 3.0 app confirms itself when it serves its first command, so ``update()``
confirms by sending one (``READ_IDENT``); :func:`confirm` is there for an explicit confirm.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from ..exceptions import OpenDisplayError

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from smpclient import SMPClient

_CONNECT_TIMEOUT_S: Final = 30.0
_REQUEST_TIMEOUT_S: Final = 10.0
_FIRST_CHUNK_TIMEOUT_S: Final = 40.0  # the first write may erase the whole slot


class SmpError(OpenDisplayError):
    """The device refused an SMP request, or the extra is missing."""


class SmpTransferError(SmpError):
    """The upload stopped part-way; the device keeps its old image."""


def _require_extra() -> None:
    try:
        import smpclient  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
    except ImportError as err:
        raise SmpError("Updating this device needs the 'smp' extra: pip install py-opendisplay[smp]") from err


def _is_error(reply: Any) -> bool:
    from smpclient.generics import error  # pylint: disable=import-outside-toplevel

    return bool(error(reply))


def _transport_class() -> type[Any]:
    from bleak_retry_connector import (  # pylint: disable=import-outside-toplevel
        BleakClientWithServiceCache,
        establish_connection,
    )
    from smpclient.transport import SMP_CHARACTERISTIC_UUID  # pylint: disable=import-outside-toplevel
    from smpclient.transport.ble import (  # pylint: disable=import-outside-toplevel
        SMPBLETransport,
        SMPBLETransportNotSMPServer,
    )

    class DeviceSMPTransport(SMPBLETransport):
        """``smpclient``'s BLE transport, connected to a given ``BLEDevice`` (HA, proxies)."""

        def __init__(self, ble_device: BLEDevice) -> None:
            super().__init__()
            self._ble_device = ble_device

        async def _connect(self, address: str, timeout_s: float) -> None:
            self._client = await establish_connection(  # pylint: disable=attribute-defined-outside-init
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self._ble_device.address,
                disconnected_callback=self._set_disconnected_event,
                max_attempts=3,
            )
            self._disconnected_event.clear()
            characteristic = self._client.services.get_characteristic(SMP_CHARACTERISTIC_UUID)
            if characteristic is None:
                raise SMPBLETransportNotSMPServer("The device has no SMP characteristic")
            self._max_write_without_response_size = characteristic.max_write_without_response_size
            if self._bluez_backend(self._client._backend):  # pylint: disable=protected-access
                await self._client._backend._acquire_mtu()  # pylint: disable=protected-access
                self._max_write_without_response_size = self._client.mtu_size - 3
            self._smp_characteristic = characteristic  # pylint: disable=attribute-defined-outside-init
            await self._await_or_disconnect(self._client.start_notify(SMP_CHARACTERISTIC_UUID, self._notify_callback))

    return DeviceSMPTransport


@asynccontextmanager
async def session(ble_device: BLEDevice) -> AsyncIterator[SMPClient]:
    """An SMP client connected to ``ble_device``; disconnected on exit."""
    _require_extra()
    from smpclient import SMPClient  # pylint: disable=import-outside-toplevel

    client = SMPClient(_transport_class()(ble_device), ble_device.address, timeout_s=_REQUEST_TIMEOUT_S)
    await client.connect(_CONNECT_TIMEOUT_S)
    try:
        yield client
    finally:
        await client.disconnect()


async def _request(client: SMPClient, request: Any) -> Any:
    reply = await client.request(request, timeout_s=_REQUEST_TIMEOUT_S)
    if _is_error(reply):
        raise SmpError(f"The device refused {type(request).__name__}: {reply}")
    return reply


async def upload(client: SMPClient, image: bytes, *, on_progress: Callable[[int, int], None] | None = None) -> None:
    """Upload ``image`` to the update slot (image 0).

    Raises:
        SmpTransferError: The upload stopped; the device keeps running its old image.
    """
    done = 0
    try:
        async for offset in client.upload(image, first_timeout_s=_FIRST_CHUNK_TIMEOUT_S):
            done = offset
            if on_progress is not None:
                on_progress(offset, len(image))
    except Exception as err:  # smpclient raises its own and asyncio/bleak errors alike
        raise SmpTransferError(f"Upload stopped at {done} of {len(image)} bytes: {err}") from err


async def test_and_reset(client: SMPClient) -> None:
    """Mark the uploaded image (slot 1) for one test boot, then reset into it.

    The image is named by the hash the device reports for slot 1, not the file's SHA-256: ESP32
    uses ``esp_partition_get_sha256`` of the slot, MCUboot its own image hash.

    Raises:
        SmpError: The device reports no uploaded image in slot 1, or refuses.
    """
    from smpclient.requests.image_management import ImageStatesWrite  # pylint: disable=import-outside-toplevel
    from smpclient.requests.os_management import ResetWrite  # pylint: disable=import-outside-toplevel

    uploaded = next((s for s in await image_states(client) if s.slot == 1 and s.hash), None)
    if uploaded is None:
        raise SmpError("The device reports no uploaded image in slot 1; nothing to boot")
    await _request(client, ImageStatesWrite(hash=bytes(uploaded.hash), confirm=False))
    await _request(client, ResetWrite())


async def confirm(client: SMPClient) -> None:
    """Make the running image permanent (the 3.0 app also does this on its first command)."""
    from smpclient.requests.image_management import ImageStatesWrite  # pylint: disable=import-outside-toplevel

    await _request(client, ImageStatesWrite(confirm=True))


async def image_states(client: SMPClient) -> list[Any]:
    """The device's image slots (``smp`` ``ImageState``: slot, version, hash, active, confirmed, pending)."""
    from smpclient.requests.image_management import ImageStatesRead  # pylint: disable=import-outside-toplevel

    reply = await _request(client, ImageStatesRead())
    return list(reply.images)
