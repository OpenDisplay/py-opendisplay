"""OpenDisplayDevice: connect, interrogate, commands and config. Uploads and sessions are delegated."""
# pylint: disable=too-many-public-methods

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, TypeVar

from epaper_dithering import DbsParams, DitherMode
from PIL import Image

from ..crypto import (
    aes_cmac,
)
from ..exceptions import (
    BLEConnectionError,
    ImageEncodingError,
    OpenDisplayConnectionError,
    OpenDisplayTimeoutError,
    ProtocolError,
    TruncatedConfigError,
)
from ..landing import build_landing_url
from ..models.advertisement import AdvertisementData, parse_advertisement
from ..models.buzzer_activate import BuzzerActivateConfig
from ..models.capabilities import DeviceCapabilities
from ..models.config import GlobalConfig
from ..models.enums import BoardManufacturer, ColorScheme, FitMode, NfcRecordType, RefreshMode, Rotation
from ..models.firmware import FirmwareVersion
from ..models.led_flash import LedFlashConfig
from ..partial import (
    PartialState,
)
from ..protocol import (
    OD_LAN_TCP_PORT,
    CommandCode,
    build_buzzer_activate_command,
    build_deep_sleep_command,
    build_enter_dfu_command,
    build_led_activate_command,
    build_power_off_command,
    build_read_config_command,
    build_read_fw_version_command,
    build_read_msd_command,
    build_reboot_command,
    build_write_config_command,
    parse_config_response,
    parse_firmware_version,
    serialize_config,
    validate_ack_response,
)
from ..protocol.responses import (
    describe_command_code,
    matches_command_echo,
    parse_read_msd,
    strip_command_echo,
    unpack_command_code,
)
from ..sensors import SensorReading, read_sensor_values
from ..transport import BLEConnection, TcpTransport, Transport
from .channel import MAX_STRAY_AUTH_FRAMES, SecureChannel
from .imaging import _capabilities_from_config, prepare_image
from .nfc import write_nfc_record
from .upload import Uploader, UploadTarget

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)


_T = TypeVar("_T")


def _serialized(
    func: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """Serialize a device command against all other commands on the same device.

    Holds the per-device command lock across the whole call so that no two
    command round-trips interleave — this prevents AES-CCM nonce reuse and
    notification-queue response mixups under concurrency. The lock is reentrant
    within a single task, so a command that internally triggers another
    ``@_serialized`` call (e.g. an upload re-authenticating mid-stream) does not
    deadlock.
    """

    @functools.wraps(func)
    async def wrapper(self: OpenDisplayDevice, *args: object, **kwargs: object) -> _T:
        # This decorator is part of OpenDisplayDevice's own machinery; the
        # "protected" transaction helper is intentionally used here.
        async with self._transaction():  # pylint: disable=protected-access
            return await func(self, *args, **kwargs)

    return wrapper


class OpenDisplayDevice:  # pylint: disable=too-many-instance-attributes
    """OpenDisplay BLE e-paper device.

    Main API for communicating with OpenDisplay BLE tags.

    Usage:
        # Auto-interrogate on first connect
        async with OpenDisplayDevice("AA:BB:CC:DD:EE:FF") as device:
            await device.upload_image(image)

        # Skip interrogation with cached config
        async with OpenDisplayDevice(mac, config=cached_config) as device:
            await device.upload_image(image)

        # Skip interrogation with minimal capabilities
        caps = DeviceCapabilities(296, 128, ColorScheme.BWR, 0)
        async with OpenDisplayDevice(mac, capabilities=caps) as device:
            await device.upload_image(image)

        # Use the scheme's idealised palette instead of a measured one
        async with OpenDisplayDevice(mac, use_measured_palettes=False) as device:
            await device.upload_image(image)
    """

    # Foreign frames tolerated while waiting for a config chunk before giving up.
    # Bounded so a device streaming unrelated frames fails with a clear error
    # instead of renewing the read timeout indefinitely.
    MAX_STRAY_CONFIG_FRAMES = 8

    # Step-1 challenge frames tolerated while waiting for the step-2 auth response.
    MAX_STRAY_AUTH_FRAMES = MAX_STRAY_AUTH_FRAMES

    # Operation timeouts (seconds). The upload ones are defined on Uploader.
    TIMEOUT_CONFIG_CHUNK = 2.0  # Subsequent config read chunks (interrogate)
    TIMEOUT_NFC_WRITE = 15.0  # NFC EEPROM commit (inline write / chunk end): slow I2C work
    TIMEOUT_FIRST_CHUNK = Uploader.TIMEOUT_FIRST_CHUNK
    TIMEOUT_ACK = Uploader.TIMEOUT_ACK
    TIMEOUT_DIRECT_WRITE_DATA_ACK = Uploader.TIMEOUT_DIRECT_WRITE_DATA_ACK
    TIMEOUT_UNCOMPRESSED_END_ACK = Uploader.TIMEOUT_UNCOMPRESSED_END_ACK
    TIMEOUT_COMPRESSED_END_ACK = Uploader.TIMEOUT_COMPRESSED_END_ACK
    TIMEOUT_REFRESH = Uploader.TIMEOUT_REFRESH
    TIMEOUT_PIPE_DATA_COMPRESSED = Uploader.TIMEOUT_PIPE_DATA_COMPRESSED
    TIMEOUT_PIPE_DATA_UNCOMPRESSED = Uploader.TIMEOUT_PIPE_DATA_UNCOMPRESSED
    TIMEOUT_PIPE_TAIL_FLUSH = Uploader.TIMEOUT_PIPE_TAIL_FLUSH

    def __init__(
        self,
        mac_address: str | None = None,
        device_name: str | None = None,
        ble_device: BLEDevice | None = None,
        transport: Transport | None = None,
        host: str | None = None,
        port: int | None = None,
        tls: bool = False,
        psk: bytes | None = None,
        config: GlobalConfig | None = None,
        capabilities: DeviceCapabilities | None = None,
        timeout: float = 10.0,
        discovery_timeout: float = 10.0,
        max_attempts: int = 4,
        use_services_cache: bool = True,
        use_measured_palettes: bool = True,
        encryption_key: bytes | None = None,
        blocks_per_ack: int = 8,
        max_queue_size: int = 16,
    ):
        """Initialize OpenDisplay device.

        Exactly one addressing mode must be given: an explicit ``transport``, a
        TCP/LAN ``host``, or a BLE address (``mac_address`` or ``device_name``).

        Args:
            mac_address: Device MAC address (mutually exclusive with device_name)
            device_name: Device name to resolve via BLE scan (mutually exclusive with mac_address)
            ble_device: Optional BLEDevice from HA bluetooth integration
            transport: Explicit pre-built transport (BLE or TCP). Takes precedence
                over host/mac addressing.
            host: Device IP/hostname for a TCP/LAN connection (WiFi transport).
            port: TCP port (default OD_LAN_TCP_PORT / 2446). TLS uses the derived
                port; pass it explicitly here when connecting over TLS.
            tls: Wrap the TCP connection in TLS-PSK (host mode only). Learned from
                the mDNS ``tls`` TXT flag.
            psk: Pre-shared key for the TLS-PSK handshake (host mode, tls=True).
            config: Optional full TLV config (skips interrogation)
            capabilities: Optional minimal device info (skips interrogation)
            timeout: BLE operation timeout in seconds (default: 10)
            discovery_timeout: Timeout for name resolution scan (default: 10)
            max_attempts: Maximum connection attempts for bleak-retry-connector (default: 4)
            use_services_cache: Enable GATT service caching for faster reconnections (default: True)
            use_measured_palettes: Use measured color palettes when available (default: True)
            encryption_key: 16-byte AES-128 master key for encrypted devices (optional).
            blocks_per_ack: Requested PIPE_WRITE ACK cadence N (blocks per ack), 1..32
                (default: 8). Negotiated down to the device maximum.
            max_queue_size: Requested PIPE_WRITE window W (tokens in flight), 1..32
                (default: 16). ``max_queue_size <= 1`` disables sliding-window fast
                transfer entirely — legacy stop-and-wait only, no 0x0080 probe.

        Raises:
            ValueError: If not exactly one addressing mode is provided
        """
        # Validation: exactly one addressing mode (explicit transport, TCP host,
        # or a BLE address). These groups are mutually exclusive.
        ble_addressed = bool(mac_address or device_name)
        modes_given = sum((transport is not None, host is not None, ble_addressed))
        if modes_given == 0:
            raise ValueError("Must provide an addressing mode: transport=, host=, mac_address=, or device_name=")
        if modes_given > 1:
            raise ValueError(
                "Provide exactly one addressing mode: transport=, host=, or a BLE address "
                "(mac_address=/device_name=) — not a combination"
            )
        if mac_address and device_name:
            raise ValueError("Provide either mac_address or device_name, not both")

        # Store for resolution in __aenter__
        self._mac_address_param = mac_address
        self._device_name = device_name
        self._transport_param = transport
        self._host = host
        self._port = port if port is not None else OD_LAN_TCP_PORT
        self._tls = tls
        self._psk = psk
        self._discovery_timeout = discovery_timeout
        self._ble_device = ble_device
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._use_services_cache = use_services_cache
        self._use_measured_palettes = use_measured_palettes

        # Will be set after resolution
        self.mac_address = mac_address or ""  # Resolved in __aenter__
        self._connection: Transport | None = None  # Created after MAC resolution

        self._config = config
        self._capabilities = capabilities
        self._fw_version: FirmwareVersion | None = None

        # Encryption session state (populated by authenticate())
        self._encryption_key = encryption_key
        self._attached_channel: SecureChannel | None = None  # created with the transport, in _attach()
        self._attached_uploader: Uploader | None = None  # created with the channel, in _attach()

        # Serializes command round-trips (see _serialized / _transaction).
        self._command_lock = asyncio.Lock()
        self._lock_owner: asyncio.Task[object] | None = None

        # Sliding-window (PIPE_WRITE) tuning + per-connection capability cache.
        self._blocks_per_ack = blocks_per_ack
        self._max_queue_size = max_queue_size

    async def __aenter__(self) -> OpenDisplayDevice:
        """Connect and optionally interrogate device."""

        # Resolve device name to MAC address if needed (BLE addressing only;
        # explicit-transport and host modes skip this block).
        if self._device_name:
            _LOGGER.debug("Resolving device name '%s' to MAC address", self._device_name)

            from ..discovery import discover_devices

            devices = await discover_devices(timeout=self._discovery_timeout)

            if self._device_name not in devices:
                raise BLEConnectionError(
                    f"Device '{self._device_name}' not found during discovery. "
                    f"Available devices: {list(devices.keys())}"
                )

            self.mac_address = devices[self._device_name]
            _LOGGER.info(
                "Resolved device name '%s' to MAC address %s",
                self._device_name,
                self.mac_address,
            )
        else:
            # MAC was provided directly — validated non-empty in __init__
            self.mac_address = self._mac_address_param or ""

        # Transport selection (mutually exclusive, validated in __init__):
        # explicit transport= > host=/port= (TCP) > BLE address (default).
        if self._transport_param is not None:
            transport: Transport = self._transport_param
        elif self._host is not None:
            # The TLS-PSK key is NOT the master key: firmware deriveTlsPsk() uses
            # AES-CMAC(master_key, "opendisplay-tls-psk") so the TLS channel never
            # reuses the key that protects the app-layer AES-CCM session. Derive it
            # here when the caller passed only encryption_key — handing OpenSSL an
            # empty PSK fails the handshake locally as PSK_IDENTITY_NOT_FOUND.
            psk = self._psk
            if psk is None and self._tls and self._encryption_key:
                psk = aes_cmac(self._encryption_key, b"opendisplay-tls-psk")
            transport = TcpTransport(
                self._host,
                self._port,
                timeout=self._timeout,
                tls=self._tls,
                psk=psk,
            )
        else:
            transport = BLEConnection(
                self.mac_address,
                self._ble_device,
                self._timeout,
                max_attempts=self._max_attempts,
                use_services_cache=self._use_services_cache,
                disconnected_callback=self._on_disconnect,
            )

        self._attach(transport)
        await self._conn.connect()

        # Over TCP the device gates AES-CCM decrypt on origin: TLS frames are
        # already-secure, plaintext frames are never decrypted. Running the
        # app-layer challenge/response would double-encrypt (and hit the 154 B
        # encrypted-chunk cap), so authentication is BLE-only.
        is_tcp = isinstance(self._connection, TcpTransport)

        # The link is now up with notifications registered. If any step below
        # raises, __aexit__ will NOT run (Python only calls it when __aenter__
        # returns) — so we must tear the link down here or leak the connection
        # and its BlueZ notification watcher. BaseException, not Exception: the
        # real triggers are AuthenticationRequiredError on a keyless probe AND
        # CancelledError from an outer asyncio.timeout() (e.g. the HA config-flow
        # connection probe).
        try:
            # Authenticate before any other commands if key provided (BLE only).
            if self._encryption_key is not None and not is_tcp:
                await self.authenticate(self._encryption_key)

            # Auto-interrogate if no config or capabilities provided
            if self._config is None and self._capabilities is None:
                _LOGGER.info("No config provided, auto-interrogating device")
                await self.interrogate()

            # Extract capabilities from config if available
            if self._config and not self._capabilities:
                self._capabilities = self._extract_capabilities_from_config()
        except BaseException:
            with suppress(Exception):
                await self._conn.disconnect()
            self._channel.reset()
            raise

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Disconnect from device."""
        if self._connection is not None:
            await self._conn.disconnect()
        # Forget the session so a reused device object re-authenticates cleanly.
        if self._attached_channel is not None:
            self._attached_channel.reset()

    @property
    def _conn(self) -> Transport:
        """Return the active transport, raising RuntimeError if not connected."""
        if self._connection is None:
            raise RuntimeError("Device not connected")
        return self._connection

    def _attach(self, transport: Transport) -> None:
        """Adopt ``transport`` as this device's link and build the channel and uploader on top of it."""
        self._connection = transport
        self._attached_channel = SecureChannel(
            transport,
            reauth_key=self._encryption_key,
            session_timeout=self._session_timeout_seconds,
        )
        previous = self._attached_uploader
        self._attached_uploader = Uploader(
            self._attached_channel,
            target=self._upload_target,
            blocks_per_ack=self._blocks_per_ack,
            max_queue_size=self._max_queue_size,
            timeouts=self,
        )
        if previous is not None:
            # What an earlier connection learned about PIPE_WRITE is dropped only by the
            # disconnect callback (_on_disconnect), not by connecting again.
            self._attached_uploader.pipe_probed = previous.pipe_probed
            self._attached_uploader.pipe_supported = previous.pipe_supported
            self._attached_uploader.pipe_partial_supported = previous.pipe_partial_supported

    @property
    def _channel(self) -> SecureChannel:
        """Return the secure channel, raising RuntimeError if not connected."""
        if self._attached_channel is None:
            raise RuntimeError("Device not connected")
        return self._attached_channel

    @property
    def _uploader(self) -> Uploader:
        """Return the uploader, raising RuntimeError if not connected."""
        if self._attached_uploader is None:
            raise RuntimeError("Device not connected")
        return self._attached_uploader

    def _upload_target(self) -> UploadTarget:
        """What the uploader needs to know about this device right now."""
        return UploadTarget(
            config=self._config,
            color_scheme=self._capabilities.color_scheme if self._capabilities else None,
            name=self.mac_address,
        )

    def _session_timeout_seconds(self) -> int:
        """The device's session timeout from its config, or 0 when unknown or disabled."""
        if self._config is None or self._config.security_config is None:
            return 0
        return self._config.security_config.session_timeout_seconds

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Hold the command lock for the duration of a command round-trip.

        Reentrant per task: if the current task already owns the lock (e.g. a
        re-authentication triggered from within an upload), the nested call runs
        without re-acquiring instead of deadlocking.
        """
        current = asyncio.current_task()
        if self._lock_owner is current:
            yield
            return
        async with self._command_lock:
            self._lock_owner = current
            try:
                yield
            finally:
                self._lock_owner = None

    def _on_disconnect(self) -> None:
        """Handle an unexpected link drop: forget the (now-dead) session and pipe state."""
        _LOGGER.debug("Link to %s dropped; clearing session state", self.mac_address)
        if self._attached_channel is not None:
            self._attached_channel.reset()
        if self._attached_uploader is not None:
            self._attached_uploader.reset_pipe_state()

    @_serialized
    async def authenticate(self, key: bytes) -> None:
        """Perform two-step challenge-response authentication with the device.

        After successful authentication, all subsequent commands and responses
        are transparently encrypted/decrypted by the secure channel.

        Args:
            key: 16-byte AES-128 master key

        Raises:
            AuthenticationFailedError: If the device rejects the key or is rate-limited
            InvalidResponseError: If device sends malformed response
        """
        _LOGGER.debug("Authenticating with device %s", self.mac_address)
        await self._channel.authenticate(key)

    def _ensure_capabilities(self) -> DeviceCapabilities:
        """Ensure device capabilities are available.

        Returns:
            DeviceCapabilities instance

        Raises:
            RuntimeError: If device not interrogated/configured
        """
        if not self._capabilities:
            raise RuntimeError("Device capabilities unknown - interrogate first or provide config/capabilities")
        return self._capabilities

    def _require_config(self) -> GlobalConfig:
        """Return the device config, raising RuntimeError if it is not known yet."""
        if not self._config:
            raise RuntimeError("Device config unknown - interrogate first or provide config")
        return self._config

    @property
    def config(self) -> GlobalConfig | None:
        """Get full device configuration (if interrogated)."""
        return self._config

    @property
    def is_flex(self) -> bool:
        """Return True if this device runs OpenDisplay Flex.

        Currently always True as the basic OpenDisplay standard is not yet
        implemented. This will be updated once the library can distinguish
        between Flex and the basic standard.
        """
        return True

    @property
    def device_name(self) -> str | None:
        """Get device BLE name, if available (requires active connection)."""
        return self._connection.device_name if self._connection else None

    @property
    def capabilities(self) -> DeviceCapabilities | None:
        """Get device capabilities (width, height, color scheme, rotation)."""
        return self._capabilities

    @property
    def width(self) -> int:
        """Get display width in pixels."""
        return self._ensure_capabilities().width

    @property
    def height(self) -> int:
        """Get display height in pixels."""
        return self._ensure_capabilities().height

    @property
    def color_scheme(self) -> ColorScheme:
        """Get display color scheme."""
        return self._ensure_capabilities().color_scheme

    @property
    def rotation(self) -> int:
        """Get display rotation in degrees."""
        return self._ensure_capabilities().rotation

    def get_board_manufacturer(self) -> BoardManufacturer | int:
        """Get board manufacturer from config.

        Requires config to be available via interrogation or constructor.

        Returns:
            Known values as BoardManufacturer enum.
            Unknown future values as raw int.

        Raises:
            RuntimeError: If config is missing.
        """
        return self._require_config().manufacturer.manufacturer_id_enum

    def get_board_type(self) -> int:
        """Get raw board type ID from config.

        Requires config to be available via interrogation or constructor.

        Raises:
            RuntimeError: If config is missing.
        """
        return self._require_config().manufacturer.board_type

    def get_board_type_name(self) -> str | None:
        """Get human-readable board type name from config, if known.

        Requires config to be available via interrogation or constructor.

        Raises:
            RuntimeError: If config is missing.
        """
        return self._require_config().manufacturer.board_type_name

    def landing_url(self) -> str:
        """Build the per-device configuration deep link (opendisplay.org/l/?...).

        Encodes the same 23-byte identity payload the firmware renders as an
        on-screen QR code: the display tag type, the device id (the "OD######"
        name), the AES key (or zeros if unknown), and the manufacturer id. See
        :mod:`opendisplay.landing` for the byte layout.

        Reads cached state (config, GAP name, key), so it works after the
        connection has closed -- but the device must have been interrogated, and
        connected at least once for the correct device id (see _device_id_bytes).

        Raises:
            RuntimeError: If config is missing.
        """
        config = self._require_config()
        tag_type = config.displays[0].tag_type if config.displays else 0
        return build_landing_url(
            tag_type,
            self._device_id_bytes(),
            self._encryption_key,
            config.manufacturer.manufacturer_id,
        )

    def _device_id_bytes(self) -> bytes:
        """Return the 3 identity bytes behind the "OD######" name.

        The firmware encodes the device's unique id here, which equals the GAP
        name -- and is *not* always the BLE MAC: nRF parts advertise a random
        static address (e.g. name OD5A2F4C on MAC E9:94:0D:B3:79:A6). Prefer the
        name; fall back to the MAC's lower 3 bytes only when no name is known.
        """
        name = self.device_name
        if name and len(name) == 8 and name[:2].upper() == "OD":
            try:
                return bytes.fromhex(name[2:])
            except ValueError:
                pass
        return bytes.fromhex(self.mac_address.replace(":", ""))[-3:]

    async def _read_config_frame(self, timeout: float) -> bytes:
        """Read the next READ_CONFIG frame, ignoring frames from other commands.

        The notification queue has no request/response correlation, so a frame
        belonging to a different exchange — a duplicated response, or one the
        firmware held in its TX ring until notifications were enabled — can land
        mid-transfer. It must not reach ``strip_command_echo``, which returns a
        non-matching frame *unchanged*: its 2-byte echo would then be consumed as
        the chunk-number field and its body spliced into the config, silently
        corrupting it (no length check catches this — the loop only counts bytes).
        Skipping such frames keeps the config read on its own command stream.

        The device's no-config error frame ({0xFF, 0x40, ...}) is passed through
        rather than skipped: it answers this command and the caller reports it.

        Raises:
            OpenDisplayTimeoutError: If no frame arrives within ``timeout``
            ProtocolError: If more than ``MAX_STRAY_CONFIG_FRAMES`` foreign frames
                arrive while waiting for this chunk
        """
        for _ in range(self.MAX_STRAY_CONFIG_FRAMES + 1):
            response = await self._channel.read(timeout)
            if matches_command_echo(response, CommandCode.READ_CONFIG):
                return response
            # NACK form of the same command: {0xFF, <cmd low byte>, ...}
            if len(response) >= 2 and response[0] == 0xFF and response[1] == CommandCode.READ_CONFIG:
                return response
            _LOGGER.warning(
                "Ignoring stray %s frame (%d B) while reading config",
                (
                    describe_command_code(unpack_command_code(response))
                    if len(response) >= 2
                    else f"malformed {response.hex() or '<empty>'}"
                ),
                len(response),
            )
        raise ProtocolError(
            f"Config read aborted: more than {self.MAX_STRAY_CONFIG_FRAMES} frames from other "
            "commands arrived while waiting for a config chunk"
        )

    @_serialized
    async def interrogate(self) -> GlobalConfig:
        """Read device configuration from device.

        Returns:
            GlobalConfig with complete device configuration

        Raises:
            ProtocolError: If interrogation fails
        """
        _LOGGER.debug("Interrogating device %s", self.mac_address)

        # Send read config command
        cmd = build_read_config_command()
        await self._channel.write(cmd)

        # Read first chunk
        response = await self._read_config_frame(self.TIMEOUT_FIRST_CHUNK)

        # Firmware answers a device-with-no-config with the 4-byte error frame
        # {0xFF, 0x40, 0x00, 0x00}. Without this check the {0x00,0x00} length
        # field is misread as a zero-length config instead of "no config".
        if len(response) == 4 and response[0] == 0xFF and response[1] == CommandCode.READ_CONFIG:
            raise ProtocolError("Device has no stored configuration (READ_CONFIG returned an error frame)")

        chunk_data = strip_command_echo(response, CommandCode.READ_CONFIG)

        # Parse first chunk header
        total_length = int.from_bytes(chunk_data[2:4], "little")
        tlv_data = bytearray(chunk_data[4:])

        _LOGGER.debug("First chunk: %d bytes, total length: %d", len(chunk_data), total_length)

        # Read remaining chunks. Firmware caps config-read chunks, so a broken or
        # older firmware can stop sending before `total_length` is reached. Guard
        # against both a mid-transfer read timeout and a stalled transfer (empty
        # chunk making no progress) so we raise a typed error instead of hanging
        # or returning a partial config.
        while len(tlv_data) < total_length:
            try:
                next_response = await self._read_config_frame(self.TIMEOUT_CONFIG_CHUNK)
            except OpenDisplayTimeoutError as err:
                raise TruncatedConfigError(
                    f"Config read truncated: device stopped sending chunks at {len(tlv_data)}/{total_length} bytes"
                ) from err
            next_chunk_data = strip_command_echo(next_response, CommandCode.READ_CONFIG)

            # Skip chunk number field (2 bytes) and append data
            chunk_payload = next_chunk_data[2:]
            if not chunk_payload:
                raise TruncatedConfigError(
                    f"Config read stalled: device sent an empty chunk at {len(tlv_data)}/{total_length} bytes"
                )
            tlv_data.extend(chunk_payload)

            _LOGGER.debug(
                "Received chunk, total: %d/%d bytes",
                len(tlv_data),
                total_length,
            )

        _LOGGER.info("Received complete TLV data: %d bytes", len(tlv_data))

        # Parse complete config response (handles wrapper strip)
        self._config = parse_config_response(bytes(tlv_data))
        self._capabilities = self._extract_capabilities_from_config()

        _LOGGER.info(
            "Interrogated device: %dx%d, %s, rotation=%d°",
            self.width,
            self.height,
            self.color_scheme.name,
            self._config.displays[0].rotation_enum,
        )

        return self._config

    @_serialized
    async def read_firmware_version(self) -> FirmwareVersion:
        """Read firmware version from device.

        Returns:
            FirmwareVersion dictionary with 'major', 'minor', and 'sha' fields
        """
        _LOGGER.debug("Reading firmware version")

        # Send read firmware version command
        cmd = build_read_fw_version_command()
        await self._conn.write_command(cmd)

        # Read response
        response = await self._conn.read_response(timeout=self.TIMEOUT_ACK)

        # Parse version (includes SHA hash)
        self._fw_version = parse_firmware_version(response)

        _LOGGER.info(
            "Firmware version: %d.%d.%d (SHA: %s...)",
            self._fw_version["major"],
            self._fw_version["minor"],
            self._fw_version["patch"],
            self._fw_version["sha"][:8],
        )

        return self._fw_version

    @_serialized
    async def read_msd(self) -> AdvertisementData:
        """Read the device's manufacturer-specific data record.

        Returns the same 16 bytes the device broadcasts in its advertisement,
        but over the open connection -- so it works on transports where no BLE
        advertisement is observable, and needs no scan.

        Returns:
            The parsed record, including the dynamic block that carries live
            sensor readings. Use :meth:`read_sensors` to decode those.

        Raises:
            InvalidResponseError: If the device returns a malformed record
        """
        _LOGGER.debug("Reading MSD from device %s", self.mac_address)

        await self._channel.write(build_read_msd_command())
        response = await self._channel.read(self.TIMEOUT_ACK)

        return parse_advertisement(parse_read_msd(response))

    async def read_sensors(self) -> list[SensorReading]:
        """Read live values from the device's configured sensors.

        Combines the device config (which sensors exist, and where each one's
        bytes sit in the MSD) with a fresh :meth:`read_msd`.

        Returns:
            One entry per sensor with a usable reading. Empty when the device
            has no sensors configured, or none currently has a valid reading.
        """
        if not self.config or not self.config.sensors:
            return []
        return read_sensor_values(self.config, await self.read_msd())

    @_serialized
    async def reboot(self) -> None:
        """Reboot the device.

        Sends a reboot command to the device, which will cause an immediate
        system reset. The device will NOT send an ACK response - it simply
        resets after a 100ms delay.

        Warning:
            The BLE connection will be forcibly terminated when the device
            resets. This is expected behavior. The device will restart and
            begin advertising again after the reset completes (typically
            within a few seconds).

        Raises:
            OpenDisplayConnectionError: If command cannot be sent
        """
        _LOGGER.debug("Sending reboot command to device %s", self.mac_address)

        # Build and send reboot command
        cmd = build_reboot_command()
        await self._channel.write(cmd)

        # Device will reset immediately - no ACK expected
        _LOGGER.info("Reboot command sent to %s - device will reset (connection will drop)", self.mac_address)

    @_serialized
    async def deep_sleep(self, duration_seconds: int | None = None) -> None:
        """Put the device into deep sleep (command 0x0053).

        Supported on ESP32 and Silabs Flex; nRF targets do not implement deep
        sleep. The command is sent encrypted when an active session exists.

        Protocol 2.1 split the old single 0x0052 opcode in two: this method
        now sends 0x0053 (CMD_DEEP_SLEEP). The hard rail-cut that used to
        share that opcode is :meth:`power_off` (0x0052).

        The firmware's exact behavior depends on the target, and this method
        tolerates all of them — in every supported case the BLE link drops during
        or right after the command, so a disconnect (or a missing ACK) is treated
        as success, mirroring reboot() and trigger_dfu_bootloader():

        - ESP32 with a D-FF power latch: firmware ACKs 0x0053, then powers off
          after ~100 ms (the link drops).
        - ESP32 without a power latch: firmware enters deep sleep immediately,
          tearing down BLE with no ACK (the write or read fails as the link drops).
        - Silabs Flex: firmware ACKs 0x0053, then closes the connection and enters
          EM4 (wake on button/NFC).

        Args:
            duration_seconds: Optional one-shot wake-timer duration. The
                firmware enforces a 60 second floor; this is not validated
                client-side.

        Raises:
            ProtocolError: If the device explicitly reports that deep sleep is not
                supported (protocol error frame 0xFF53).
        """
        _LOGGER.debug("Sending deep sleep command (0x0053) to device %s", self.mac_address)

        try:
            await self._channel.write(build_deep_sleep_command(duration_seconds))
        except OpenDisplayConnectionError as exc:
            # An ESP32 without a power latch tears down BLE synchronously as it
            # enters deep sleep, so the write-with-response confirmation can fail
            # (e.g. a GATT/disconnect error over a Bluetooth proxy) even though the
            # command was delivered. Treat that as the device having gone to sleep.
            _LOGGER.debug(
                "Deep sleep write did not confirm (expected — device sleeps before responding): %s",
                exc,
            )
            _LOGGER.info("Deep sleep command sent to %s — device is sleeping (connection dropped)", self.mac_address)
            return

        # Targets that ACK before sleeping (ESP32 power-latch, Silabs Flex) reply
        # with 0x0053 and then drop the link; a device that does not support the
        # command replies with the 0xFF53 error frame (protocol: 0xFF [command_low]).
        # A disconnect or timeout here means the device slept without acking.
        try:
            response = await self._channel.read(self.TIMEOUT_ACK)
        except (OpenDisplayConnectionError, OpenDisplayTimeoutError) as exc:
            _LOGGER.debug(
                "No deep sleep ACK (expected — device dropped the link or sleeps silently): %s",
                exc,
            )
            _LOGGER.info("Deep sleep command sent to %s — device is sleeping", self.mac_address)
            return

        if len(response) >= 2 and unpack_command_code(response) == 0xFF53:
            raise ProtocolError("Device reported deep sleep is not supported (command 0x0053)")

        validate_ack_response(response, CommandCode.DEEP_SLEEP)
        _LOGGER.info("Deep sleep command acknowledged by %s — device is sleeping", self.mac_address)

    @_serialized
    async def power_off(self) -> None:
        """Cut power via the device's D-FF power latch (command 0x0052).

        Only supported on boards with a power latch. This is a harder,
        one-way cut than :meth:`deep_sleep` — there is no wake timer, only a
        physical power button or reset can bring the device back. The
        command is sent encrypted when an active session exists.

        This shares 0x0052's old tolerate-the-disconnect behavior with
        :meth:`deep_sleep`, since a board with a power latch is expected to
        ACK and then drop the link as it cuts power.

        Raises:
            ProtocolError: If the device explicitly reports that it has no
                power latch (protocol error frame 0xFF52).
        """
        _LOGGER.debug("Sending power off command (0x0052) to device %s", self.mac_address)

        try:
            await self._channel.write(build_power_off_command())
        except OpenDisplayConnectionError as exc:
            _LOGGER.debug(
                "Power off write did not confirm (expected — device powers off before responding): %s",
                exc,
            )
            _LOGGER.info("Power off command sent to %s — device is powering off (connection dropped)", self.mac_address)
            return

        try:
            response = await self._channel.read(self.TIMEOUT_ACK)
        except (OpenDisplayConnectionError, OpenDisplayTimeoutError) as exc:
            _LOGGER.debug(
                "No power off ACK (expected — device dropped the link): %s",
                exc,
            )
            _LOGGER.info("Power off command sent to %s — device is powering off", self.mac_address)
            return

        if len(response) >= 2 and unpack_command_code(response) == 0xFF52:
            raise ProtocolError("Device reported it has no power latch (command 0x0052)")

        validate_ack_response(response, CommandCode.POWER_OFF)
        _LOGGER.info("Power off command acknowledged by %s — device is powering off", self.mac_address)

    @_serialized
    async def trigger_dfu_bootloader(self) -> None:
        """Trigger the DFU bootloader on nRF devices (command 0x0051).

        On nRF52840/nRF52811 devices this causes the firmware to disconnect BLE,
        write Nordic GPREGRET magic byte 0xB1, disable the SoftDevice, and jump
        directly to the bootloader. The device will reappear advertising the
        Nordic Legacy DFU GATT service (UUID 00001530-...) within a few seconds.

        The command is sent encrypted if an active session exists (required when
        the device has encryption enabled).

        No ACK response is sent — the firmware resets before it can respond.
        The BLE connection will drop immediately after this call returns.

        Raises:
            OpenDisplayConnectionError: If the command cannot be sent
        """
        _LOGGER.debug("Triggering DFU bootloader on device %s", self.mac_address)
        try:
            await self._channel.write(build_enter_dfu_command())
        except OpenDisplayConnectionError as exc:
            # The firmware resets before it can ACK this command — it has no time
            # to send a write response — so the confirmation never arrives. With a
            # write-with-response transport, especially over a Bluetooth proxy,
            # that surfaces as a GATT/disconnect error (e.g. error 133) even though
            # the command was delivered and the device is already entering DFU.
            # Treat a write failure here as expected rather than fatal; whether the
            # device actually entered DFU is determined by the subsequent scan for
            # the DFU-mode device.
            _LOGGER.debug(
                "DFU trigger write did not ACK (expected — device resets before responding): %s",
                exc,
            )
        _LOGGER.info(
            "DFU bootloader trigger sent to %s — device will disconnect and enter DFU mode",
            self.mac_address,
        )

    async def clear_gatt_cache(self) -> bool:
        """Clear the cached GATT table for this device on the active connection.

        Use this on the Silabs (EFR32BG22) OTA path *before* calling
        ``trigger_dfu_bootloader()``, while still connected in app mode: it
        clears an ESPHome Bluetooth proxy's stale per-MAC GATT cache so that the
        post-reboot connection to the AppLoader re-discovers the OTA service
        instead of returning the cached app-firmware table. The device keeps the
        same address across the reboot, so without this the proxy would serve
        the wrong GATT and the OTA characteristics would not be found.

        No-op (returns False) on backends without cache support (e.g. direct
        BlueZ on a bleak build lacking ``clear_cache``).

        Returns:
            True if a cache was cleared, False if the backend does not support
            it or the clear failed.

        Raises:
            OpenDisplayConnectionError: If the device is not connected.
        """
        return await self._conn.clear_cache()

    @_serialized
    async def activate_led(
        self,
        led_instance: int,
        flash_config: LedFlashConfig,
        timeout: float | None = None,
    ) -> bytes:
        """Activate LED flash behavior via firmware command 0x0073 (firmware 1.0+).

        Args:
            led_instance: LED instance index (0-based)
            flash_config: Typed flash config for this activation.
            timeout: Optional response timeout in seconds.
                Defaults to TIMEOUT_REFRESH because the firmware responds only after
                the LED routine finishes.

        Returns:
            Raw ACK response bytes from the device.

        Raises:
            RuntimeError: If device is not connected.
            ValueError: If command arguments are invalid.
            ProtocolError: If firmware version is too old for this command.
            ProtocolError: If firmware returns an LED activate error response.
            InvalidResponseError: If ACK response is malformed or mismatched.
        """
        if self._connection is None:
            raise RuntimeError("Device not connected")

        fw = self._fw_version
        if fw is None:
            fw = await self.read_firmware_version()
        if (fw["major"], fw["minor"]) < (1, 0):
            raise ProtocolError(f"LED activate requires firmware >= 1.0, got {fw['major']}.{fw['minor']}")

        cmd = build_led_activate_command(
            led_instance=led_instance,
            flash_config=flash_config,
        )
        await self._channel.write(cmd)

        response_timeout = self.TIMEOUT_REFRESH if timeout is None else timeout
        response = await self._channel.read(response_timeout)

        # Firmware LED errors use 0xFF73 + error code payload.
        if len(response) >= 2 and unpack_command_code(response) == 0xFF73:
            error_code = response[2] if len(response) >= 3 else None
            if error_code is None:
                raise ProtocolError("LED activate failed with malformed error response")
            raise ProtocolError(f"LED activate failed: firmware error code 0x{error_code:02x}")

        validate_ack_response(response, CommandCode.LED_ACTIVATE)
        return response

    @_serialized
    async def activate_buzzer(
        self,
        buzzer_instance: int,
        config: BuzzerActivateConfig,
        timeout: float | None = None,
    ) -> bytes:
        """Activate buzzer via firmware command 0x0077.

        Args:
            buzzer_instance: Buzzer instance index (0-based)
            config: Typed buzzer activation config
            timeout: Optional response timeout in seconds.

        Returns:
            Raw ACK response bytes from the device.

        Raises:
            RuntimeError: If device is not connected.
            ValueError: If command arguments are invalid.
            InvalidResponseError: If ACK response is malformed or mismatched.
        """
        if self._connection is None:
            raise RuntimeError("Device not connected")

        cmd = build_buzzer_activate_command(buzzer_instance=buzzer_instance, config=config)
        await self._channel.write(cmd)

        response_timeout = self.TIMEOUT_REFRESH if timeout is None else timeout
        response = await self._channel.read(response_timeout)
        validate_ack_response(response, CommandCode.BUZZER_ACTIVATE)
        return response

    @_serialized
    async def write_nfc(
        self,
        rec_type: NfcRecordType | int,
        payload: bytes,
        timeout: float | None = None,
    ) -> None:
        """Write an NDEF record to the device's NFC tag via NFC_ENDPOINT (0x0083).

        Payloads up to NFC_INLINE_MAX (120) bytes are sent as a single inline
        write. Larger payloads (up to NFC_WRITE_MAX_TOTAL, 512 bytes) are sent
        as a chunked write: a start frame, one or more NFC_CHUNK_SIZE (120)
        byte data frames, and an end frame that commits the write.

        Args:
            rec_type: NDEF record type (see NfcRecordType).
            payload: Record payload bytes, 1..NFC_WRITE_MAX_TOTAL.
            timeout: Optional override for the commit read (the inline write's
                response, or the chunked write's end response). Defaults to
                TIMEOUT_NFC_WRITE because the EEPROM commit is slow I2C work.
                Intermediate chunk-stage ACKs during a chunked write always
                use TIMEOUT_ACK.

        Raises:
            RuntimeError: If device is not connected.
            ValueError: If payload length is outside 1..NFC_WRITE_MAX_TOTAL.
            NfcWriteError: If firmware rejects the write with an error frame.
            NfcNotSupportedError: If the device does not respond to the first
                frame of the write sequence (firmware older than the NFC
                write feature stays silent on the unknown opcode).
            InvalidResponseError: If a response frame is malformed or carries
                an unexpected status.
        """
        if self._connection is None:
            raise RuntimeError("Device not connected")
        commit_timeout = self.TIMEOUT_NFC_WRITE if timeout is None else timeout
        await write_nfc_record(
            self._channel, rec_type, payload, commit_timeout=commit_timeout, ack_timeout=self.TIMEOUT_ACK
        )

    async def write_nfc_url(self, url: str, timeout: float | None = None) -> None:
        """Write a URI NDEF record containing the given URL.

        The URL is sent verbatim as UTF-8; firmware builds the NDEF URI
        record from the payload.

        Args:
            url: URL to write.
            timeout: Optional override; see write_nfc.
        """
        await self.write_nfc(NfcRecordType.URI, url.encode("utf-8"), timeout)

    async def write_nfc_text(self, text: str, timeout: float | None = None) -> None:
        """Write a TEXT NDEF record containing the given text.

        The text is sent verbatim as UTF-8.

        Args:
            text: Text to write.
            timeout: Optional override; see write_nfc.
        """
        await self.write_nfc(NfcRecordType.TEXT, text.encode("utf-8"), timeout)

    async def write_nfc_mime(
        self,
        mime_type: str,
        body: bytes | str,
        timeout: float | None = None,
    ) -> None:
        """Write a MIME NDEF record with a length-prefixed MIME type header.

        The host payload format is ``bytes([len(mt)]) + mt + body`` where
        ``mt`` is the MIME type encoded as UTF-8, confirmed against the web
        BLE tester and firmware MIME parser.

        Args:
            mime_type: MIME type string (e.g. "text/vcard"). Must encode to
                1..255 bytes as UTF-8.
            body: Record body, as bytes or a UTF-8 string.
            timeout: Optional override; see write_nfc.

        Raises:
            ValueError: If the encoded MIME type is outside 1..255 bytes.
        """
        mt = mime_type.encode("utf-8")
        if not 1 <= len(mt) <= 255:
            raise ValueError(f"mime_type must encode to 1..255 bytes, got {len(mt)}")
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        payload = bytes([len(mt)]) + mt + body_bytes
        await self.write_nfc(NfcRecordType.MIME, payload, timeout)

    @_serialized
    async def write_config(self, config: GlobalConfig) -> None:
        """Write configuration to device.

        Serializes the GlobalConfig to TLV binary format and writes it
        to the device using the WRITE_CONFIG (0x0041) command with
        automatic chunking for large configs.

        On encrypted devices this command is sent encrypted (normal flow).
        If the device has the ``rewrite_allowed`` flag set in its SecurityConfig,
        the firmware also accepts unencrypted WRITE_CONFIG — useful for
        provisioning without knowing the current key (connect with
        ``config=`` or ``capabilities=`` to skip interrogation).

        Args:
            config: GlobalConfig to write to device

        Raises:
            ValueError: If config serialization fails or exceeds size limit
            OpenDisplayConnectionError: If write fails
            ProtocolError: If device returns error response
        """
        _LOGGER.debug("Writing config to device %s", self.mac_address)

        # Defensive runtime validation for callers that bypass typing.
        if config.system is None or config.manufacturer is None or config.power is None:
            missing_packets = []
            if config.system is None:
                missing_packets.append("system")
            if config.manufacturer is None:
                missing_packets.append("manufacturer")
            if config.power is None:
                missing_packets.append("power")
            raise ValueError(f"Config missing required packets: {', '.join(missing_packets)}")

        if not config.displays:
            raise ValueError("Config must have at least one display")

        # Serialize config to binary
        config_data = serialize_config(config)

        _LOGGER.info(
            "Serialized config: %d bytes (chunking %s)",
            len(config_data),
            "required" if len(config_data) > 200 else "not needed",
        )

        # Build command with chunking
        first_cmd, chunk_cmds = build_write_config_command(config_data)

        # Send first command
        _LOGGER.debug("Sending first config chunk (%d bytes)", len(first_cmd))
        await self._channel.write(first_cmd)

        # Wait for ACK
        response = await self._channel.read(self.TIMEOUT_ACK)
        validate_ack_response(response, CommandCode.WRITE_CONFIG)

        # Send remaining chunks if needed
        for i, chunk_cmd in enumerate(chunk_cmds, start=1):
            _LOGGER.debug("Sending config chunk %d/%d (%d bytes)", i, len(chunk_cmds), len(chunk_cmd))
            await self._channel.write(chunk_cmd)

            # Wait for ACK after each chunk
            response = await self._channel.read(self.TIMEOUT_ACK)
            validate_ack_response(response, CommandCode.WRITE_CONFIG_CHUNK)

        _LOGGER.info("Config written successfully to %s", self.mac_address)

    def export_config_json(self, file_path: str) -> None:
        """Export device config to JSON file (Open Display Config Builder format).

        Raises:
            ValueError: If no config loaded
        """
        if not self._config:
            raise ValueError("No config loaded - interrogate device first")

        import json

        from ..models import config_to_json

        data = config_to_json(self._config)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        _LOGGER.info("Exported config to %s", file_path)

    @staticmethod
    def import_config_json(file_path: str) -> GlobalConfig:
        """Import config from JSON file (Open Display Config Builder format).

        Raises:
            FileNotFoundError: If file not found
            ValueError: If JSON invalid
        """
        import json

        from ..models import config_from_json

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        _LOGGER.info("Imported config from %s", file_path)
        return config_from_json(data)

    def _prepare_image(
        self,
        image: Image.Image,
        dither_mode: DitherMode,
        compress: bool,
        serpentine: bool = True,
        exposure: float = 1.0,
        saturation: float = 1.0,
        shadows: float = 0.0,
        highlights: float = 0.0,
        tone: float = 0.0,
        gamut: float = 0.0,
        fit: FitMode = FitMode.CONTAIN,
        rotate: Rotation = Rotation.ROTATE_0,
        dbs: DbsParams | bool | None = None,
    ) -> tuple[bytes, bytes | None, Image.Image]:
        """Prepare image for upload. Internal wrapper for the module-level prepare_image()."""
        return prepare_image(
            image,
            config=self._config,
            capabilities=self._ensure_capabilities(),
            use_measured_palettes=self._use_measured_palettes,
            dither_mode=dither_mode,
            compress=compress,
            serpentine=serpentine,
            exposure=exposure,
            saturation=saturation,
            shadows=shadows,
            highlights=highlights,
            tone=tone,
            gamut=gamut,
            fit=fit,
            rotate=rotate,
            dbs=dbs,
        )

    @_serialized
    async def upload_image(
        self,
        image: Image.Image,
        refresh_mode: RefreshMode = RefreshMode.FULL,
        dither_mode: DitherMode = DitherMode.BURKES,
        compress: bool = True,
        serpentine: bool = True,
        exposure: float = 1.0,
        saturation: float = 1.0,
        shadows: float = 0.0,
        highlights: float = 0.0,
        tone: float = 0.0,
        gamut: float = 0.0,
        fit: FitMode = FitMode.CONTAIN,
        rotate: Rotation = Rotation.ROTATE_0,
        progress_callback: Callable[[int, int], None] | None = None,
        state: PartialState | None = None,
        dbs: DbsParams | bool | None = None,
    ) -> Image.Image:
        """Upload image to device display.

        Automatically handles:
        - Image fitting to display dimensions
        - Dithering based on color scheme
        - Encoding to device format
        - Compression
        - Direct write protocol

        Args:
            image: PIL Image to display
            refresh_mode: Display refresh mode (default: FULL)
            dither_mode: Dithering algorithm (default: BURKES)
            compress: Enable zlib compression (default: True)
            serpentine: Alternate scan direction each row to reduce artifacts (default: True)
            exposure: Exposure multiplier, >1.0 brightens (default: 1.0)
            saturation: Saturation multiplier, >1.0 boosts (default: 1.0)
            shadows: Shadow lift in [0.0, 1.0] (default: 0.0)
            highlights: Highlight rolloff in [0.0, 1.0] (default: 0.0)
            tone: Black-point lift, 0.0–1.0 (default: 0.0)
            gamut: Gamut compression, 0.0–1.0 (default: 0.0)
            fit: How to map the image to display dimensions (default: CONTAIN).
            rotate: Source image rotation enum, applied before fit/encoding.
            dbs: Direct Binary Search refinement — ``None``/``False`` = off
                (default), ``True`` = on with the panel's pixel density, or
                explicit ``DbsParams``. Slow; runs off the event loop.

        Raises:
            RuntimeError: If device not interrogated/configured
            ProtocolError: If upload fails

        Returns:
            Processed image that matches what is sent to the display.
        """
        if not self._capabilities:
            raise RuntimeError("Device capabilities unknown - interrogate first or provide config/capabilities")

        _LOGGER.info(
            "Uploading image to %s (%dx%d, %s)",
            self.mac_address,
            self.width,
            self.height,
            self.color_scheme.name,
        )

        # Skip compression in _prepare_image when the device can't use it (wasted
        # CPU), and when a partial upload may succeed: full-frame compression is
        # pure waste if the partial path handles the update. dispatch_upload
        # compresses lazily on the full-upload fallback.
        prepare_compress = compress and self._uploader.supports_compression and state is None

        # Prepare image (fit, dither, encode, compress). CPU-bound — seconds with
        # DBS refinement — so run it in a worker thread instead of blocking the
        # caller's event loop (e.g. Home Assistant's).
        image_data, compressed_data, processed_image = await asyncio.to_thread(
            self._prepare_image,
            image,
            dither_mode,
            prepare_compress,
            serpentine=serpentine,
            exposure=exposure,
            saturation=saturation,
            shadows=shadows,
            highlights=highlights,
            tone=tone,
            gamut=gamut,
            fit=fit,
            rotate=rotate,
            dbs=dbs,
        )

        await self._uploader.upload_prepared(
            image_data, compressed_data, processed_image, refresh_mode, compress, progress_callback, state
        )
        return processed_image

    @_serialized
    async def upload_prepared_image(
        self,
        prepared_data: tuple[bytes, bytes | None, Image.Image],
        refresh_mode: RefreshMode = RefreshMode.FULL,
        compress: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
        state: PartialState | None = None,
    ) -> None:
        """Upload pre-computed image data to device.

        Accepts the output of prepare_image() and sends it over BLE
        without re-processing. Requires an active BLE connection
        (must be called within the async context manager).

        Args:
            prepared_data: Tuple from prepare_image()
                (uncompressed_data, compressed_data or None, processed_image)
            refresh_mode: Display refresh mode (default: FULL)
            compress: Whether to use compressed protocol if data is available
            progress_callback: Optional callback receiving (bytes_sent, total_bytes)
                after each chunk is written to the BLE transport.

        Raises:
            ProtocolError: If upload fails
        """
        image_data, compressed_data, processed_image = prepared_data
        await self._uploader.upload_prepared(
            image_data, compressed_data, processed_image, refresh_mode, compress, progress_callback, state
        )

    # ─── PIPE_WRITE (sliding-window) upload ──────────────────────────────────

    def _extract_capabilities_from_config(self) -> DeviceCapabilities:
        """Extract DeviceCapabilities from GlobalConfig.

        Returns:
            DeviceCapabilities with display info

        Raises:
            RuntimeError: If config missing or invalid
        """
        if not self._config:
            raise RuntimeError("No config available")

        try:
            return _capabilities_from_config(self._config)
        except ValueError as exc:
            display = self._config.displays[0]
            raise ImageEncodingError(
                f"Device uses unsupported color scheme value {display.color_scheme}. "
                "Reconfigure the device to a supported color scheme (0–8)."
            ) from exc
