"""BLE connection management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from bleak import BleakClient, BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from ..exceptions import BLEConnectionError, BLETimeoutError
from ..protocol import SERVICE_UUID

if TYPE_CHECKING:
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

# Bounded number of clear-cache-and-rediscover retries on a stale-GATT-cache
# failure: 1 initial attempt + up to MAX_CACHE_RETRIES retries. Kept small so a
# genuinely broken link drops instead of looping.
MAX_CACHE_RETRIES = 2


class BLEConnection:
    """Manages BLE connection to OpenDisplay device.

    Features:
    - Automatic retry logic with bleak-retry-connector
    - Service caching for faster reconnections
    - Context manager for automatic cleanup
    - Notification queue for response handling
    """

    def __init__(
        self,
        mac_address: str,
        ble_device: BLEDevice | None = None,
        timeout: float = 10.0,
        max_attempts: int = 4,
        use_services_cache: bool = True,
        disconnected_callback: Callable[[], None] | None = None,
    ):
        """Initialize BLE connection manager.

        Args:
            mac_address: Device MAC address
            ble_device: Optional BLEDevice from Home Assistant bluetooth integration
            timeout: Connection timeout in seconds (default: 10)
            max_attempts: Maximum connection attempts for bleak-retry-connector (default: 4)
            use_services_cache: Enable GATT service caching for faster reconnections (default: True)
            disconnected_callback: Optional callback invoked when the link drops
                (either an unexpected disconnect or a graceful one).
        """
        self.mac_address = mac_address
        self.ble_device = ble_device
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.use_services_cache = use_services_cache
        self._disconnected_callback = disconnected_callback

        self._client: BleakClient | None = None
        self._notification_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._notification_characteristic: BleakGATTCharacteristic | None = None
        # Whether the command characteristic advertises Write Without Response.
        # Set during notification setup; used to safely enable/disable WNR writes.
        self._write_no_response_supported: bool = False
        self.device_name: str | None = None

    async def __aenter__(self) -> BLEConnection:
        """Connect to device (context manager entry)."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Disconnect from device (context manager exit)."""
        await self.disconnect()

    async def connect(self) -> None:
        """Establish BLE connection to device.

        Uses bleak-retry-connector for automatic retry logic and service caching.

        Raises:
            BLEConnectionError: If connection fails
            BLETimeoutError: If connection times out
        """
        if self._client and self._client.is_connected:
            return  # Already connected

        # A stale Bluetooth-proxy GATT cache can make a connection fail even though
        # the device is present: the proxy serves a cached GATT layout whose handles
        # no longer match the device (e.g. the expected service appears missing, or a
        # CCCD write during notify setup hits an ATT "Invalid handle"). Recover by
        # clearing the proxy cache and rediscovering with use_services_cache=False.
        # This is bounded (MAX_CACHE_RETRIES) so a genuinely broken link drops the
        # connection and raises instead of looping forever.
        last_error: Exception | None = None
        for attempt in range(MAX_CACHE_RETRIES + 1):
            use_cache = self.use_services_cache and attempt == 0
            try:
                await self._attempt_connect(use_services_cache=use_cache)
                return
            except asyncio.TimeoutError as e:
                await self._clear_cache_and_drop()
                raise BLETimeoutError(f"Connection timeout after {self.timeout}s") from e
            except Exception as e:  # noqa: BLE001 - classified below
                last_error = e
                if not self._is_stale_cache_error(e):
                    # Not a cache problem (e.g. device not found during scan) — drop
                    # the half-open link and fail fast; retrying won't help.
                    await self._clear_cache_and_drop()
                    raise BLEConnectionError(f"Failed to connect: {e}") from e
                _LOGGER.debug(
                    "Connect to %s failed (%s); clearing GATT cache and retrying (attempt %d/%d)",
                    self.mac_address,
                    e,
                    attempt + 1,
                    MAX_CACHE_RETRIES + 1,
                )
                # Clears the proxy cache AND disconnects, so the next iteration
                # rediscovers from scratch and we never keep a stale connection.
                await self._clear_cache_and_drop()

        # Bounded attempts exhausted: the connection was already dropped by
        # _clear_cache_and_drop() above.
        raise BLEConnectionError(
            f"Failed to connect after {MAX_CACHE_RETRIES + 1} attempts (last error: {last_error})"
        ) from last_error

    def _is_stale_cache_error(self, err: Exception) -> bool:
        """Return True for GATT failures a proxy cache-clear + rediscovery can fix.

        Covers a missing expected service AND handle-level mismatches (e.g. an
        ESPHome proxy serving a cached GATT layout whose CCCD handle no longer
        exists on the device -> ATT "Invalid handle" during notify setup).
        Deliberately EXCLUDES a plain "not found during scan", which is a device
        presence problem, not a cache one, and must not trigger cache-clear retries.
        """
        msg = str(err).lower()
        if "not found during scan" in msg:
            return False
        return (
            SERVICE_UUID.lower() in msg
            or "invalid handle" in msg
            or "invalid attribute" in msg
            or "attribute not found" in msg
        )

    async def _attempt_connect(self, *, use_services_cache: bool) -> None:
        """Resolve the device, establish a connection, and set up notifications."""
        _LOGGER.debug(
            "Connecting to %s with bleak-retry-connector (max_attempts=%d, use_services_cache=%s)",
            self.mac_address,
            self.max_attempts,
            use_services_cache,
        )

        # Resolve MAC to BLEDevice if not provided
        if self.ble_device:
            device = self.ble_device
        else:
            # For MAC-only usage, scan for the device
            found_device: BLEDevice | None = await BleakScanner.find_device_by_address(
                self.mac_address, timeout=self.timeout
            )
            if found_device is None:
                raise BLEConnectionError(f"Device {self.mac_address} not found during scan")
            device = found_device

        self.device_name = device.name

        # Establish connection with retry logic
        self._client = await establish_connection(
            client_class=BleakClientWithServiceCache,
            device=device,
            name=device.name or self.mac_address,
            max_attempts=self.max_attempts,
            use_services_cache=use_services_cache,
            timeout=self.timeout,
            disconnected_callback=self._on_disconnect,
        )

        _LOGGER.debug("Connected to %s", self.mac_address)

        # Start notifications
        await self._setup_notifications()

    async def _stop_notifications(self) -> None:
        """Best-effort release of the notification subscription (and its BlueZ
        watcher) for the current client.

        Called before every disconnect so the watcher is released even if the
        subsequent ACL disconnect raises or is flaky. A disconnect that fails
        while notifications are still registered is the watcher-leak precondition:
        stopping notifications first makes nulling the client reference afterwards
        safe. Never raises.
        """
        client = self._client
        char = self._notification_characteristic
        if client is None or char is None or not client.is_connected:
            return
        try:
            await client.stop_notify(char)
        except Exception as err:  # noqa: BLE001 - best-effort; link may be tearing down
            _LOGGER.debug("stop_notify during teardown failed: %s", err)

    async def _clear_cache_and_drop(self) -> None:
        """Clear the proxy GATT cache on the current client, then disconnect it."""
        if self._client is None:
            return
        clear = getattr(self._client, "clear_cache", None)
        if callable(clear):
            try:
                await clear()  # pylint: disable=not-callable
            except Exception as err:  # noqa: BLE001 - best-effort
                _LOGGER.debug("clear_cache during connect retry failed: %s", err)
        # Release the notification watcher BEFORE disconnecting so a flaky
        # disconnect can't strand it (the watcher-leak precondition). The retry
        # loop relies on _client being None afterwards, so we always null below.
        await self._stop_notifications()
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._notification_characteristic = None
        self._client = None

    async def disconnect(self) -> None:
        """Disconnect from device, releasing notifications first."""
        if self._client and self._client.is_connected:
            # Stop notifications before disconnecting so a disconnect that raises
            # can't leave the BlueZ watcher registered (the watcher-leak that
            # delivers stray/duplicate frames to a later connection).
            await self._stop_notifications()
            try:
                _LOGGER.debug("Disconnecting from %s", self.mac_address)
                await self._client.disconnect()
            except Exception as e:
                _LOGGER.warning("Error during disconnect: %s", e)
            finally:
                self._notification_characteristic = None
                self._client = None

    async def clear_cache(self) -> bool:
        """Clear the GATT services cache for this device.

        On an ESPHome Bluetooth proxy this clears the proxy's cached per-MAC
        GATT table so the next connection re-discovers services — needed when
        the device's GATT changes without changing address (e.g. rebooting into
        the Silabs AppLoader for OTA). Requires an active connection; the
        on-device clear only works if the proxy firmware supports it (otherwise
        only the in-memory cache is cleared).

        Returns:
            True if a cache was cleared, False if the backend has no cache
            support (e.g. direct BlueZ on a bleak build without ``clear_cache``).

        Raises:
            BLEConnectionError: If not connected.
        """
        if not self._client or not self._client.is_connected:
            raise BLEConnectionError("Not connected")
        # The concrete client (BleakClientWithServiceCache) has clear_cache, but
        # the declared BleakClient type does not — resolve it dynamically.
        clear_cache_fn = getattr(self._client, "clear_cache", None)
        if not callable(clear_cache_fn):
            return False
        # Guarded by callable() above; pylint can't infer through getattr.
        return bool(await clear_cache_fn())  # pylint: disable=not-callable

    async def _setup_notifications(self) -> None:
        """Set up BLE notifications for responses.

        Raises:
            BLEConnectionError: If service/characteristic not found
        """
        if not self._client or not self._client.is_connected:
            raise BLEConnectionError("Not connected")

        # Find the service
        services = self._client.services
        service = services.get_service(SERVICE_UUID)
        if not service:
            raise BLEConnectionError(f"Service {SERVICE_UUID} not found")

        # Get first characteristic (should be the only one)
        characteristics = service.characteristics
        if not characteristics:
            raise BLEConnectionError("No characteristics found")

        self._notification_characteristic = characteristics[0]

        # Record whether the characteristic advertises Write Without Response so
        # writes can opt into it (0x71 data chunks) and gracefully fall back to
        # write-with-response on devices/stacks that don't support it.
        props = getattr(self._notification_characteristic, "properties", []) or []
        self._write_no_response_supported = "write-without-response" in props
        _LOGGER.debug(
            "Command characteristic write-without-response supported: %s",
            self._write_no_response_supported,
        )

        # Start notifications
        await self._client.start_notify(
            self._notification_characteristic,
            self._notification_callback,
        )

        _LOGGER.debug("Notifications started")

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Handle an unexpected or graceful BLE disconnect.

        Notifies the owner (e.g. so it can drop stale encryption session state)
        via the registered ``disconnected_callback``.
        """
        _LOGGER.debug("BLE link to %s dropped", self.mac_address)
        if self._disconnected_callback is not None:
            try:
                self._disconnected_callback()
            except Exception:  # noqa: BLE001 - best-effort notification
                _LOGGER.debug("disconnected_callback raised", exc_info=True)

    def _notification_callback(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle incoming BLE notifications.

        Args:
            sender: Characteristic that sent notification
            data: Notification data
        """
        # Put notification in queue for processing
        self._notification_queue.put_nowait(bytes(data))

    def drain_notifications(self) -> int:
        """Discard any queued notifications and return how many were dropped.

        The queue has no request/response correlation, so a stale frame — e.g. a
        response that arrived just after its read timed out, or an unsolicited
        firmware frame — would otherwise be returned as the answer to the *next*
        command and desync every subsequent read by one. Draining before writing
        a command clears such leftovers; in healthy stop-and-wait operation the
        queue is already empty here, so this is a no-op.
        """
        dropped = 0
        while True:
            try:
                self._notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        if dropped:
            _LOGGER.warning("Discarded %d stale notification(s) before command", dropped)
        return dropped

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        """Write command to device.

        Args:
            data: Command bytes to write
            response: If True, use a BLE Write Request and wait for the ATT write
                confirmation. If False, use a Write Without Response (Write Command)
                to skip the ATT round-trip — used for bulk 0x71 image-data chunks,
                which are still flow-controlled by the application-layer ACK. Falls
                back to a Write Request if the characteristic does not advertise
                write-without-response.
            drain_stale: If True (default), discard any queued notifications before
                writing so this command's response reads from a clean queue. MUST be
                False for every write during a live PIPE_WRITE stream (0x81 data
                frames AND the 0x82 END): the sliding window keeps ACKs queued ahead
                of the sender, and draining would eat them.

        Raises:
            BLEConnectionError: If not connected or write fails
        """
        if not self._client or not self._client.is_connected:
            raise BLEConnectionError("Not connected")

        if not self._notification_characteristic:
            raise BLEConnectionError("Notifications not set up")

        # Clear any stale/unsolicited frames so this command's response is read
        # from a clean queue (see drain_notifications). Skipped mid-pipe-stream
        # where queued ACKs are expected and must be preserved.
        if drain_stale:
            self.drain_notifications()

        # Only skip the write confirmation when the caller opts out AND the
        # characteristic actually supports it; otherwise keep write-with-response.
        effective_response = response or not self._write_no_response_supported

        try:
            await self._client.write_gatt_char(
                self._notification_characteristic,
                data,
                response=effective_response,
            )
        except Exception as e:
            raise BLEConnectionError(f"Write failed: {e}") from e

    async def read_response(self, timeout: float = 5.0) -> bytes:
        """Read response from notification queue.

        Args:
            timeout: Read timeout in seconds (default: 5)

        Returns:
            Response data from device

        Raises:
            BLETimeoutError: If no response received within timeout
        """
        try:
            return await asyncio.wait_for(
                self._notification_queue.get(),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            # asyncio.wait_for can cancel queue.get() *after* an item was handed
            # to it, silently dropping that item. Re-check synchronously before
            # giving up so a response delivered during cancellation is not lost.
            try:
                return self._notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            raise BLETimeoutError(f"No response received within {timeout}s") from e

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to device."""
        return self._client is not None and self._client.is_connected
