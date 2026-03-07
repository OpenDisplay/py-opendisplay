"""BLE connection management."""

from __future__ import annotations

import asyncio
import logging
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
    ):
        """Initialize BLE connection manager.

        Args:
            mac_address: Device MAC address
            ble_device: Optional BLEDevice from Home Assistant bluetooth integration
            timeout: Connection timeout in seconds (default: 10)
            max_attempts: Maximum connection attempts for bleak-retry-connector (default: 4)
            use_services_cache: Enable GATT service caching for faster reconnections (default: True)
        """
        self.mac_address = mac_address
        self.ble_device = ble_device
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.use_services_cache = use_services_cache

        self._client: BleakClient | None = None
        self._notification_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._notification_characteristic: BleakGATTCharacteristic | None = None

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
            _LOGGER.debug(
                "Connecting to %s with bleak-retry-connector (max_attempts=%d)",
                self.mac_address,
                self.max_attempts
            )

            # Resolve MAC to BLEDevice if not provided
            if self.ble_device:
                device = self.ble_device
            else:
                # For MAC-only usage, scan for the device
                found_device: BLEDevice | None = await BleakScanner.find_device_by_address(
                    self.mac_address,
                    timeout=self.timeout
                )
                if found_device is None:
                    raise BLEConnectionError(
                        f"Device {self.mac_address} not found during scan"
                    )
                device = found_device

            # Establish connection with retry logic
            self._client = await establish_connection(
                client_class=BleakClientWithServiceCache,
                device=device,
                name=device.name or self.mac_address,
                max_attempts=self.max_attempts,
                use_services_cache=self.use_services_cache,
                timeout=self.timeout,
            )

            _LOGGER.debug("Connected to %s", self.mac_address)

            # Start notifications
            await self._setup_notifications()

        except asyncio.TimeoutError as e:
            raise BLETimeoutError(
                f"Connection timeout after {self.timeout}s"
            ) from e
        except Exception as e:
            raise BLEConnectionError(
                f"Failed to connect: {e}"
            ) from e

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
            raise BLEConnectionError(
                f"Service {SERVICE_UUID} not found"
            )

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

    def _notification_callback(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle incoming BLE notifications.

        Args:
            sender: Characteristic that sent notification
            data: Notification data
        """
        # Put notification in queue for processing
        self._notification_queue.put_nowait(bytes(data))

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
            raise BLETimeoutError(
                f"No response received within {timeout}s"
            ) from e

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to device."""
        return self._client is not None and self._client.is_connected
