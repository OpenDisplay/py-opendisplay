"""TCP/LAN transport for OpenDisplay devices (WiFi).

Implements the SECTION 9 (protocol 2.2) LAN wire format: a length-prefixed
stream of ``[len:2 LE][payload]`` frames over a single TCP connection, plaintext
on ``OD_LAN_TCP_PORT`` (configured ``server_port``) or TLS-PSK on the derived
``server_port + 1``. One client at a time; request+response over the same pipe.

Structurally satisfies :class:`~opendisplay.transport.base.Transport`. Unlike
BLE there is no notification queue and no GATT cache, so ``drain_notifications``
is a no-op returning 0 and ``clear_cache`` a no-op returning False. Over TLS the
application-layer AES-CCM MUST NOT run (the device gates decrypt on origin), so
the caller (``OpenDisplayDevice``) skips authentication on this transport.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from contextlib import suppress

from .. import redraw
from ..exceptions import OpenDisplayConnectionError, OpenDisplayTimeoutError
from ..protocol import OD_LAN_MAX_PAYLOAD

_LOGGER = logging.getLogger(__name__)

# PSK identity presented to the device during the TLS-PSK handshake. Matches the
# firmware's expected identity; the shared key itself is the ``psk`` argument.
_PSK_IDENTITY = "opendisplay"

# Length prefix is a 2-byte little-endian unsigned integer.
_LEN_PREFIX = 2


class TcpTransport:
    """Length-prefixed TCP (optionally TLS-PSK) command/response link.

    Args:
        host: Device IP address or hostname.
        port: TCP port (plaintext ``server_port`` or the derived TLS port).
        timeout: Connect / per-frame read timeout in seconds (default 10).
        tls: When True, wrap the socket in TLS-PSK. Learn this from the mDNS
            ``tls`` TXT flag — port numbers are configurable and unreliable.
        psk: Pre-shared key bytes for the TLS-PSK handshake. Required when
            ``tls=True``; ignored otherwise.
    """

    #: LAN frame payload ceiling — OD_LAN_MAX_PAYLOAD (4094), keeping the wire
    #: frame (2-byte len prefix + payload) within OD_LAN_MAX_FRAME (4096).
    max_frame: int = OD_LAN_MAX_PAYLOAD
    #: TCP writes are always reliable; there is no write-without-response.
    supports_write_without_response: bool = False

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 10.0,
        tls: bool = False,
        psk: bytes | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = tls
        self._psk = psk
        #: Identities of this display in ``opendisplay.redraw``; a read waits out a noted redraw.
        self.redraw_keys: tuple[str, ...] = (host,)
        # LAN has no advertised name; kept for Transport structural conformance.
        self.device_name: str | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    # ── connection lifecycle ────────────────────────────────────────────────

    def _build_ssl_context(self) -> ssl.SSLContext:
        """Build an ECDHE-PSK client context.

        PSK is the sole authentication (no certificates), so hostname/cert
        verification is disabled. The device serves a TLS-1.2-style ECDHE-PSK
        ciphersuite (mbedTLS), so the version is pinned to 1.2 where
        ``set_psk_client_callback`` + PSK ciphers negotiate cleanly.
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        # Restrict to PSK ciphersuites (includes ECDHE-PSK) so the handshake
        # never expects a certificate.
        with suppress(ssl.SSLError):
            context.set_ciphers("PSK")
        psk = self._psk or b""

        def _psk_client_callback(_hint: str | None) -> tuple[str, bytes]:
            return (_PSK_IDENTITY, psk)

        context.set_psk_client_callback(_psk_client_callback)
        return context

    async def connect(self) -> None:
        """Open the TCP (and optionally TLS) connection.

        Raises:
            OpenDisplayConnectionError: on connect / TLS handshake failure.
            OpenDisplayTimeoutError: if the connect exceeds ``timeout``.
        """
        if self.is_connected:
            return

        ssl_context = self._build_ssl_context() if self.tls else None
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=ssl_context),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError as err:
            raise OpenDisplayTimeoutError(
                f"Connection to {self.host}:{self.port} timed out after {self.timeout}s"
            ) from err
        except (OSError, ssl.SSLError) as err:
            raise OpenDisplayConnectionError(f"Failed to connect to {self.host}:{self.port}: {err}") from err
        _LOGGER.debug("Connected to %s:%d (tls=%s)", self.host, self.port, self.tls)

    async def disconnect(self) -> None:
        """Close the connection. Best-effort; never raises."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
            # A half-open/aborted TLS or TCP link raises OSError on close; the
            # link is going away regardless, so swallow it.
            with suppress(OSError, ssl.SSLError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=self.timeout)
        except Exception as err:  # noqa: BLE001 - best-effort teardown
            _LOGGER.debug("Error during TCP disconnect: %s", err)

    @property
    def is_connected(self) -> bool:
        """Whether the link is up (writer present and not closing)."""
        return self._writer is not None and not self._writer.is_closing()

    # ── framing ─────────────────────────────────────────────────────────────

    async def write_command(  # pylint: disable=unused-argument
        self, data: bytes, response: bool = True, drain_stale: bool = True
    ) -> None:
        """Send one ``[len:2 LE][payload]`` frame.

        ``response`` and ``drain_stale`` are accepted for Transport conformance
        and ignored: every TCP write is reliable and there is no stale queue.
        """
        if self._writer is None:
            raise OpenDisplayConnectionError("Not connected")
        if not 0 < len(data) <= self.max_frame:
            raise ValueError(f"Frame payload length {len(data)} out of range (1..{self.max_frame})")
        frame = len(data).to_bytes(_LEN_PREFIX, "little") + data
        try:
            self._writer.write(frame)
            await self._writer.drain()
        except (OSError, ssl.SSLError) as err:
            raise OpenDisplayConnectionError(f"Write failed: {err}") from err

    async def _read_header(self, reader: asyncio.StreamReader, timeout: float) -> bytes:
        """The length prefix; past ``timeout``, keep waiting while the display is noted redrawing."""
        read = asyncio.ensure_future(reader.readexactly(_LEN_PREFIX))
        try:
            return await asyncio.wait_for(asyncio.shield(read), timeout=timeout)
        except asyncio.TimeoutError:
            pending = redraw.remaining(*self.redraw_keys)
            if not pending:
                read.cancel()
                raise
            redraw.notify_waiting()
            return await asyncio.wait_for(read, timeout=pending)

    async def read_response(self, timeout: float = 5.0) -> bytes:
        """Read exactly one length-prefixed frame.

        Header and body get independent timeouts: once a length prefix arrives
        the peer is committed to sending that many bytes, so the body read gets
        a fresh deadline. A truncated frame (peer closed mid-body) surfaces as a
        connection error rather than a timeout.

        Raises:
            OpenDisplayTimeoutError: no frame within ``timeout``.
            OpenDisplayConnectionError: link closed, or a protocol violation
                (zero-length or oversize frame — the connection is dropped).
        """
        if self._reader is None:
            raise OpenDisplayConnectionError("Not connected")
        reader = self._reader
        try:
            header = await self._read_header(reader, timeout)
        except asyncio.TimeoutError as err:
            raise OpenDisplayTimeoutError(f"No response within {timeout}s") from err
        except (asyncio.IncompleteReadError, OSError, ssl.SSLError) as err:
            raise OpenDisplayConnectionError(f"Connection closed while reading frame header: {err}") from err

        length = int.from_bytes(header, "little")
        if length == 0 or length > OD_LAN_MAX_PAYLOAD:
            # Protocol error: drop the connection (SECTION 9 rule).
            await self.disconnect()
            raise OpenDisplayConnectionError(f"Invalid LAN frame length {length} (expected 1..{OD_LAN_MAX_PAYLOAD})")

        try:
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        except asyncio.TimeoutError as err:
            raise OpenDisplayTimeoutError(f"Frame body ({length} bytes) not received within {timeout}s") from err
        except (asyncio.IncompleteReadError, OSError, ssl.SSLError) as err:
            raise OpenDisplayConnectionError(f"Connection closed mid-frame ({length} bytes expected): {err}") from err
        return payload

    def drain_notifications(self) -> int:
        """No notification queue on TCP; nothing to drain."""
        return 0

    async def clear_cache(self) -> bool:
        """TCP has no GATT cache."""
        return False
