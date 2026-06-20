"""TCP transport: same BLE command/response bytes, length-prefixed (uint16 LE)."""

from __future__ import annotations

import asyncio
import logging

from ..exceptions import BLEConnectionError, BLETimeoutError

_LOGGER = logging.getLogger(__name__)

# Must match Firmware WIFI_LAN_MAX_PAYLOAD
LAN_MAX_FRAME_PAYLOAD = 4096


class LANConnection:
    """TCP client to ESP32 OpenDisplay LAN server (BLE-shaped frames)."""

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.device_name: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
        except TimeoutError as e:
            raise BLETimeoutError(f"LAN connect timeout after {self.timeout}s") from e
        except OSError as e:
            raise BLEConnectionError(f"LAN connect failed: {e}") from e
        self.device_name = self.host
        _LOGGER.debug("LAN connected to %s:%s", self.host, self.port)

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = None
        self._writer = None
        self.device_name = None

    async def clear_cache(self) -> bool:
        """No-op for LAN (GATT cache is BLE-only)."""
        return False

    async def write_command(self, data: bytes) -> None:
        if self._writer is None:
            raise BLEConnectionError("LAN not connected")
        if len(data) > 0xFFFF:
            raise BLEConnectionError("LAN command exceeds uint16 length")
        frame = len(data).to_bytes(2, "little") + data
        try:
            self._writer.write(frame)
            await self._writer.drain()
        except OSError as e:
            raise BLEConnectionError(f"LAN write failed: {e}") from e

    async def read_response(self, timeout: float = 5.0) -> bytes:
        if self._reader is None:
            raise BLEConnectionError("LAN not connected")
        try:
            hdr = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout)
        except TimeoutError as e:
            raise BLETimeoutError(f"No LAN response within {timeout}s") from e
        except asyncio.IncompleteReadError as e:
            raise BLEConnectionError(f"LAN connection closed ({e!r})") from e
        ln = int.from_bytes(hdr, "little")
        if ln == 0 or ln > LAN_MAX_FRAME_PAYLOAD:
            raise BLEConnectionError(f"Invalid LAN frame length: {ln}")
        try:
            return await asyncio.wait_for(self._reader.readexactly(ln), timeout=timeout)
        except TimeoutError as e:
            raise BLETimeoutError(f"LAN response body timeout ({ln} bytes)") from e
        except asyncio.IncompleteReadError as e:
            raise BLEConnectionError(f"LAN truncated response ({e!r})") from e

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()
