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

        try:
            await self._attempt_connect(use_services_cache=self.use_services_cache)
        except asyncio.TimeoutError as e:
            raise BLETimeoutError(f"Connection timeout after {self.timeout}s") from e
        except BLEConnectionError as e:
            # A stale Bluetooth-proxy GATT cache can make the expected service
            # appear missing — e.g. after the device was last seen in the DFU/OTA
            # bootloader, the proxy keeps serving the bootloader's GATT for this
            # MAC. Clear the proxy cache and retry once with fresh discovery
            # before giving up. (Only for the service-missing case; a plain
            # "device not found during scan" is re-raised unchanged.)
            if SERVICE_UUID.lower() not in str(e).lower():
                raise
            _LOGGER.debug(
                "Connect to %s failed (%s); clearing GATT cache and retrying once",
                self.mac_address,
                e,
            )
            await self._clear_cache_and_drop()
            try:
                await self._attempt_connect(use_services_cache=False)
            except asyncio.TimeoutError as e2:
                raise BLETimeoutError(f"Connection timeout after {self.timeout}s") from e2
            except Exception as e2:
                raise BLEConnectionError(f"Failed to connect: {e2}") from e2
        except Exception as e:
            raise BLEConnectionError(f"Failed to connect: {e}") from e

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
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._client = None

    async def disconnect(self) -> None:
        """Disconnect from device."""
        if self._client and self._client.is_connected:
            try:
                _LOGGER.debug("Disconnecting from %s", self.mac_address)
                await self._client.disconnect()
            except Exception as e:
                _LOGGER.warning("Error during disconnect: %s", e)
            finally:
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

    async def write_command(self, data: bytes) -> None:
        """Write command to device.

        Args:
            data: Command bytes to write

        Raises:
            BLEConnectionError: If not connected or write fails
        """
        if not self._client or not self._client.is_connected:
            raise BLEConnectionError("Not connected")

        if not self._notification_characteristic:
            raise BLEConnectionError("Notifications not set up")

        # Clear any stale/unsolicited frames so this command's response is read
        # from a clean queue (see drain_notifications).
        self.drain_notifications()

        try:
            await self._client.write_gatt_char(
                self._notification_characteristic,
                data,
                response=True,  # Wait for write confirmation
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
