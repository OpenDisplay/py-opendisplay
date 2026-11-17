"""mcumgr SMP over Bluetooth: upload an image, mark it for a test boot, reset, confirm.

Firmware 3.0 updates ESP32, nRF52840 and nRF54 this way (Firmware_Unified ``docs/UPGRADING.md``:
"mcumgr SMP upload of ``opendisplay-<board>-app.bin``"). The client is the library's own: the
five requests an update needs, over the link the library opens everywhere else
(``bleak-retry-connector`` to a given ``BLEDevice``, which Home Assistant and ESPHome Bluetooth
proxies need), with no dependency beyond bleak.

A frame is an 8-byte header (op, flags, length, group, sequence, command) and a CBOR map
(``_cbor``). A request is sent as writes without response of one MTU each, the only kind the SMP
characteristic offers; the reply arrives as notifications and is matched by its sequence number.

An image uploaded and marked "test" boots once; if it never confirms, the bootloader returns to
the old image. The 3.0 app confirms itself when it serves its first command, so ``update()``
confirms by sending one (``READ_IDENT``); :func:`confirm` is there for an explicit confirm.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from ..exceptions import OpenDisplayError
from . import _cbor

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

SMP_CHARACTERISTIC_UUID: Final = "da2e7828-fbce-4e01-ae9e-261174997c48"

_REQUEST_TIMEOUT_S: Final = 10.0
_FIRST_CHUNK_TIMEOUT_S: Final = 40.0  # the first write may erase the whole slot
_RESET_TIMEOUT_S: Final = 5.0
_HEADER: Final = 8
_OP_READ: Final = 0
_OP_WRITE: Final = 2
_GROUP_OS: Final = 0
_GROUP_IMAGE: Final = 1
_OS_RESET: Final = 5
_OS_PARAMS: Final = 6
_IMG_STATE: Final = 0
_IMG_UPLOAD: Final = 1
_DATA_HEAD_GROWTH: Final = 2  # an empty byte string's head is 1 byte; up to 65535 bytes it is 3
_MAX_STALLED_REPLIES: Final = 3

_RC_NAMES: Final = {
    1: "unknown",
    2: "no memory",
    3: "invalid",
    4: "timeout",
    5: "no entry",
    6: "bad state",
    7: "too large",
    8: "not supported",
    9: "corrupt",
    10: "busy",
    11: "denied",
}


class SmpError(OpenDisplayError):
    """An SMP exchange failed."""


class SmpRefusedError(SmpError):
    """The device answered with an error code (``rc``)."""

    def __init__(self, what: str, rc: int) -> None:
        super().__init__(f"The device refused {what}: rc {rc} ({_RC_NAMES.get(rc, 'unknown code')})")
        self.rc = rc


class SmpTransferError(SmpError):
    """The upload stopped part-way; the device keeps its old image."""


class SmpStallError(SmpTransferError):
    """The link stopped carrying the exchange (no reply, or a disconnect): worth starting over.

    An ESPHome Bluetooth proxy drops a burst of unacknowledged writes it cannot queue; the device
    then waits for the rest of a request and never replies. Not the device refusing anything.
    """


@dataclass(frozen=True)
class ImageState:
    """One image slot as the device reports it."""

    slot: int
    version: str
    hash: bytes | None
    active: bool
    confirmed: bool
    pending: bool
    bootable: bool


class SmpClient:
    """One SMP link to a display. Use through :func:`session`."""

    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._client: Any = None
        self._characteristic: Any = None
        self._replies: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._rx = bytearray()
        self._sequence = 0
        #: Bytes per write: the ATT MTU less 3.
        self.write_size = 20
        #: The largest frame a request may be; the device's ``buf_size`` unless capped.
        self.max_request = 20

    async def _link(self) -> Any:
        from bleak_retry_connector import (  # pylint: disable=import-outside-toplevel
            BleakClientWithServiceCache,
            establish_connection,
        )

        return await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._ble_device.name or self._ble_device.address,
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )

    async def connect(self) -> None:
        """Connect, find the SMP characteristic, subscribe, and read the device's buffer size.

        Raises:
            SmpError: The display has no SMP service (it does not run firmware 3.x).
        """
        client = await self._link()
        characteristic = client.services.get_characteristic(SMP_CHARACTERISTIC_UUID)
        if characteristic is None and await client.clear_cache():
            # A service table cached before the device ran 3.0 has no SMP service (an ESPHome
            # proxy keeps one per MAC; seen on the Bedroom proxy). Forget it and discover again.
            await client.disconnect()
            client = await self._link()
            characteristic = client.services.get_characteristic(SMP_CHARACTERISTIC_UUID)
        self._client = client
        if characteristic is None:
            await self.disconnect()
            raise SmpError("The display has no SMP service; updates over Bluetooth need firmware 3.x")
        self._characteristic = characteristic
        self.write_size = await self._write_size(client, characteristic)
        await client.start_notify(characteristic, self._on_notify)
        try:
            params = await self.request(_OP_READ, _GROUP_OS, _OS_PARAMS, {})
            self.max_request = int(params.get("buf_size") or self.write_size)
        except SmpRefusedError:  # a server without the parameters command: one write per request
            self.max_request = self.write_size

    @staticmethod
    async def _write_size(client: Any, characteristic: Any) -> int:
        if type(client._backend).__name__ == "BleakClientBlueZDBus":  # pylint: disable=protected-access
            # BlueZ reports 20 until the MTU exchange has been read (bleak's own private call,
            # as smpclient and bleak-retry-connector use it).
            await client._backend._acquire_mtu()  # pylint: disable=protected-access
            return int(client.mtu_size) - 3
        return int(characteristic.max_write_without_response_size)

    async def disconnect(self) -> None:
        """Close the link; never raises."""
        client, self._client = self._client, None
        if client is not None:
            with suppress(Exception):
                await client.disconnect()

    def _on_disconnect(self, client: Any) -> None:
        if client is self._client:
            self._replies.put_nowait(None)

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        self._rx.extend(data)
        while len(self._rx) >= _HEADER:
            want = _HEADER + int.from_bytes(self._rx[2:4], "big")
            if len(self._rx) < want:
                return
            self._replies.put_nowait(bytes(self._rx[:want]))
            del self._rx[:want]

    async def request(
        self, op: int, group: int, command: int, body: dict[str, Any], *, timeout: float = _REQUEST_TIMEOUT_S
    ) -> dict[str, Any]:
        """Send one request and return its reply's body.

        Raises:
            SmpStallError: No reply within ``timeout``, or the link dropped.
            SmpRefusedError: The device answered with an error code.
        """
        if self._client is None:
            raise SmpStallError("The SMP link is closed")
        sequence = self._sequence
        self._sequence = (sequence + 1) & 0xFF
        payload = _cbor.encode(body)
        frame = (
            bytes([op & 0x07, 0])
            + len(payload).to_bytes(2, "big")
            + group.to_bytes(2, "big")
            + bytes([sequence, command])
            + payload
        )
        try:
            for start in range(0, len(frame), self.write_size):
                await self._client.write_gatt_char(
                    self._characteristic, frame[start : start + self.write_size], response=False
                )
            return await asyncio.wait_for(self._reply(sequence, group, command), timeout)
        except TimeoutError as err:
            raise SmpStallError(f"No reply within {timeout:.0f} s") from err
        except SmpError:
            raise
        except Exception as err:  # bleak errors from the write, whatever the backend
            raise SmpStallError(f"The link failed: {err}") from err

    async def _reply(self, sequence: int, group: int, command: int) -> dict[str, Any]:
        while True:
            frame = await self._replies.get()
            if frame is None:
                raise SmpStallError("The display dropped the link")
            if frame[6] != sequence or int.from_bytes(frame[4:6], "big") != group or frame[7] != command:
                _LOGGER.debug("Ignoring an SMP reply to another request: %s", frame[:8].hex())
                continue
            body = _cbor.decode(frame[_HEADER:]) if len(frame) > _HEADER else {}
            if not isinstance(body, dict):
                raise SmpError(f"Malformed SMP reply: {body!r}")
            error = body.get("err")
            rc = error.get("rc", 0) if isinstance(error, dict) else body.get("rc", 0)
            if rc:
                raise SmpRefusedError(f"group {group} command {command}", int(rc))
            return body


@asynccontextmanager
async def session(ble_device: BLEDevice, *, burst_writes: int | None = None) -> AsyncIterator[SmpClient]:
    """An SMP client connected to ``ble_device``; disconnected on exit.

    Args:
        ble_device: The display.
        burst_writes: Cap each request at this many writes. By default a request fills the
            device's buffer (8 KB on ESP32: about 33 writes, sent without acknowledgement).
    """
    client = SmpClient(ble_device)
    try:
        await client.connect()
        if burst_writes is not None:
            client.max_request = min(client.max_request, burst_writes * client.write_size)
        yield client
    finally:
        await client.disconnect()


def _upload_body(image: bytes, offset: int, digest: bytes) -> dict[str, Any]:
    if offset == 0:
        return {"image": 0, "len": len(image), "off": 0, "sha": digest}
    return {"off": offset}


async def upload(client: SmpClient, image: bytes, *, on_progress: Callable[[int, int], None] | None = None) -> None:
    """Upload ``image`` to the update slot (image 0), one request per ``max_request`` bytes.

    Raises:
        SmpStallError: The link stopped carrying it; starting over may work.
        SmpTransferError: The device refused the image. Either way it keeps running its old image.
    """
    total = len(image)
    digest = hashlib.sha256(image).digest()
    offset = 0
    stalled = 0
    while offset < total:
        body = _upload_body(image, offset, digest)
        room = client.max_request - _HEADER - len(_cbor.encode({**body, "data": b""})) - _DATA_HEAD_GROWTH
        if room <= 0:
            raise SmpTransferError(f"A request of {client.max_request} bytes leaves no room for image data")
        body["data"] = image[offset : offset + room]
        timeout = _FIRST_CHUNK_TIMEOUT_S if offset == 0 else _REQUEST_TIMEOUT_S
        try:
            reply = await client.request(_OP_WRITE, _GROUP_IMAGE, _IMG_UPLOAD, body, timeout=timeout)
        except SmpRefusedError as err:
            raise SmpTransferError(f"The device refused the image at {offset} of {total} bytes: {err}") from err
        except SmpStallError as err:
            raise SmpStallError(f"Upload stopped at {offset} of {total} bytes: {err}") from err
        reached = reply.get("off")
        if not isinstance(reached, int) or reached > total:
            raise SmpTransferError(f"The device sent no usable offset at {offset} of {total} bytes: {reply!r}")
        stalled = stalled + 1 if reached <= offset else 0
        if stalled >= _MAX_STALLED_REPLIES:
            raise SmpStallError(f"Upload stopped at {offset} of {total} bytes: the device stopped advancing")
        offset = reached
        if on_progress is not None:
            on_progress(offset, total)


async def image_states(client: SmpClient) -> list[ImageState]:
    """The device's image slots."""
    reply = await client.request(_OP_READ, _GROUP_IMAGE, _IMG_STATE, {})
    return [
        ImageState(
            slot=int(image.get("slot", 0)),
            version=str(image.get("version", "")),
            hash=bytes(image["hash"]) if isinstance(image.get("hash"), bytes) else None,
            active=bool(image.get("active", False)),
            confirmed=bool(image.get("confirmed", False)),
            pending=bool(image.get("pending", False)),
            bootable=bool(image.get("bootable", False)),
        )
        for image in reply.get("images", [])
        if isinstance(image, dict)
    ]


async def test_and_reset(client: SmpClient) -> bytes:
    """Mark the uploaded image (slot 1) for one test boot, reset into it, and return its hash.

    The image is named by the hash the device reports for slot 1, not the file's SHA-256: ESP32
    uses ``esp_partition_get_sha256`` of the slot, MCUboot its own image hash. A reset whose reply
    never comes is not an error: the device may restart before it sends one.

    Raises:
        SmpError: The device reports no uploaded image in slot 1, or refuses.
    """
    uploaded = next((s for s in await image_states(client) if s.slot == 1 and s.hash), None)
    if uploaded is None or uploaded.hash is None:
        raise SmpError("The device reports no uploaded image in slot 1; nothing to boot")
    await client.request(_OP_WRITE, _GROUP_IMAGE, _IMG_STATE, {"hash": uploaded.hash, "confirm": False})
    try:
        await client.request(_OP_WRITE, _GROUP_OS, _OS_RESET, {}, timeout=_RESET_TIMEOUT_S)
    except SmpStallError as err:
        _LOGGER.debug("No reply to the reset (the device may already be restarting): %s", err)
    return uploaded.hash


async def confirm(client: SmpClient) -> None:
    """Make the running image permanent (the 3.0 app also does this on its first command)."""
    await client.request(_OP_WRITE, _GROUP_IMAGE, _IMG_STATE, {"confirm": True})
