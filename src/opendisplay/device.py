"""Main OpenDisplay BLE device class."""
# pylint: disable=too-many-lines,too-many-public-methods

from __future__ import annotations

import logging
import time
import zlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from epaper_dithering import ColorScheme, DitherMode, dither_image
from PIL import Image

from .crypto import (
    compute_challenge_response,
    decrypt_response,
    derive_session_id,
    derive_session_key,
    encrypt_command,
    generate_client_nonce,
)
from .display_palettes import PANELS_4GRAY, get_gray4_codes, get_palette_for_display
from .encoding import (
    compress_image_data,
    encode_bitplanes,
    encode_gray4_bitplanes,
    encode_image,
    fit_image,
)
from .exceptions import (
    AuthenticationRequiredError,
    AuthenticationSessionExistsError,
    BLETimeoutError,
    ImageEncodingError,
    InvalidResponseError,
    ProtocolError,
)
from .landing import build_landing_url
from .models.buzzer_activate import BuzzerActivateConfig
from .models.capabilities import DeviceCapabilities
from .models.config import GlobalConfig
from .models.enums import BoardManufacturer, FitMode, RefreshMode, Rotation
from .models.firmware import FirmwareVersion
from .models.led_flash import LedFlashConfig
from .partial import (
    ERR_ETAG_MISMATCH,
    PARTIAL_FLAG_COMPRESSED,
    PartialState,
    _generate_etag,
    build_partial_logical_stream,
    compute_partial_region,
    encode_segment_wire,
    parse_nack,
)
from .protocol import (
    CHUNK_SIZE,
    ENCRYPTED_CHUNK_SIZE,
    MAX_COMPRESSED_SIZE,
    MAX_COMPRESSED_SIZE_ZIPXL,
    MAX_START_PAYLOAD,
    CommandCode,
    build_authenticate_step1,
    build_authenticate_step2,
    build_buzzer_activate_command,
    build_direct_write_data_command,
    build_direct_write_end_command,
    build_direct_write_end_with_etag,
    build_direct_write_partial_start,
    build_direct_write_start_compressed,
    build_direct_write_start_uncompressed,
    build_enter_dfu_command,
    build_led_activate_command,
    build_read_config_command,
    build_read_fw_version_command,
    build_reboot_command,
    build_write_config_command,
    parse_config_response,
    parse_firmware_version,
    serialize_config,
    validate_ack_response,
)
from .protocol.responses import (
    check_response_type,
    parse_authenticate_challenge,
    parse_authenticate_success,
    strip_command_echo,
    unpack_command_code,
)
from .transport import BLEConnection

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

_INDEX_TO_ROTATION: dict[int, Rotation] = {
    0: Rotation.ROTATE_0,
    1: Rotation.ROTATE_90,
    2: Rotation.ROTATE_180,
    3: Rotation.ROTATE_270,
}


def _capabilities_rotation(raw: int) -> Rotation:
    """Convert a DeviceCapabilities.rotation int to a Rotation enum.

    Tolerates both degree values (0/90/180/270) stored by current code and
    raw firmware indices (0/1/2/3) that may exist in older serialized data.
    """
    try:
        return Rotation(raw)
    except ValueError:
        return _INDEX_TO_ROTATION.get(raw, Rotation.ROTATE_0)


def _rotate_source_image(image: Image.Image, rotate: Rotation) -> Image.Image:
    """Rotate source image by enum value before fitting.

    Rotation uses clockwise semantics for API ergonomics.
    """
    if not isinstance(rotate, Rotation):
        raise TypeError(f"rotate must be Rotation, got {type(rotate).__name__}")

    if rotate == Rotation.ROTATE_0:
        return image
    if rotate == Rotation.ROTATE_90:
        return image.transpose(Image.Transpose.ROTATE_270)
    if rotate == Rotation.ROTATE_180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if rotate == Rotation.ROTATE_270:
        return image.transpose(Image.Transpose.ROTATE_90)
    return image


def prepare_image(
    image: Image.Image,
    config: GlobalConfig | None = None,
    capabilities: DeviceCapabilities | None = None,
    use_measured_palettes: bool = True,
    panel_ic_type: int | None = None,
    dither_mode: DitherMode = DitherMode.BURKES,
    compress: bool = True,
    serpentine: bool = True,
    exposure: float = 1.0,
    saturation: float = 1.0,
    shadows: float = 0.0,
    highlights: float = 0.0,
    tone: float | str = 0.0,
    gamut: float | str = 0.0,
    fit: FitMode = FitMode.CONTAIN,
    rotate: Rotation = Rotation.ROTATE_0,
) -> tuple[bytes, bytes | None, Image.Image]:
    """Prepare image for display without requiring a BLE connection.

    Standalone function that processes an image (rotate, fit, dither, encode)
    using only the device configuration. No device instance or BLE connection
    needed.

    Args:
        image: PIL Image to prepare
        config: Device configuration (GlobalConfig from interrogation)
        capabilities: Optional explicit capabilities. If None, extracted
            from config.
        use_measured_palettes: Use measured color palettes when available
        panel_ic_type: Panel IC type for palette lookup. If None, extracted
            from config.
        dither_mode: Dithering algorithm to use (default: BURKES)
        compress: Whether to compress the image data (default: True)
        serpentine: Alternate scan direction each row to reduce artifacts (default: True)
        exposure: Exposure multiplier, >1.0 brightens (default: 1.0)
        saturation: Saturation multiplier, >1.0 boosts (default: 1.0)
        shadows: Shadow lift in [0.0, 1.0] (default: 0.0)
        highlights: Highlight rolloff in [0.0, 1.0] (default: 0.0)
        tone: Dynamic range compression — "auto", "off", or 0.0–1.0 (default: 0.0)
        gamut: Gamut compression — "auto", "off", or 0.0–1.0 (default: 0.0)
        fit: How to map the image to display dimensions (default: CONTAIN)
        rotate: Source image rotation enum (0/90/180/270)

    Returns:
        Tuple of (uncompressed_data, compressed_data or None, processed_image)

    Raises:
        RuntimeError: If config has no display information
    """
    if capabilities is None:
        if config is None or not config.displays:
            raise RuntimeError("Config has no display information")
        display = config.displays[0]
        r = display.rotation_enum
        capabilities = DeviceCapabilities(
            width=display.pixel_width,
            height=display.pixel_height,
            color_scheme=ColorScheme.from_value(display.color_scheme),
            rotation=r.value if isinstance(r, Rotation) else 0,
        )

    if panel_ic_type is None and config is not None and config.displays:
        panel_ic_type = config.displays[0].panel_ic_type

    target_size = (capabilities.width, capabilities.height)
    base = _capabilities_rotation(capabilities.rotation)
    effective = Rotation((base.value + rotate.value) % 360)
    image = _rotate_source_image(image, effective)

    if image.size != target_size:
        _LOGGER.info(
            "Fitting image %dx%d -> %dx%d (mode: %s)",
            image.width,
            image.height,
            capabilities.width,
            capabilities.height,
            fit.name,
        )
        image = fit_image(image, target_size, fit)

    color_scheme = capabilities.color_scheme
    if color_scheme == ColorScheme.GRAYSCALE_4 and panel_ic_type is not None and panel_ic_type not in PANELS_4GRAY:
        _LOGGER.warning(
            "Panel IC 0x%04x is not a known 4-gray panel. GRAYSCALE_4 encoding may not display correctly.",
            panel_ic_type,
        )

    palette = get_palette_for_display(panel_ic_type, color_scheme, use_measured_palettes)
    dithered = dither_image(
        image,
        palette,
        mode=dither_mode,
        serpentine=serpentine,
        exposure=exposure,
        saturation=saturation,
        shadows=shadows,
        highlights=highlights,
        tone=tone,
        gamut=gamut,
    )

    # Encode to device format
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        plane1, plane2 = encode_bitplanes(dithered, color_scheme)
        image_data = plane1 + plane2
    elif color_scheme == ColorScheme.GRAYSCALE_4:
        # Two pre-split 1-bit planes concatenated; firmware streams the halves to PLANE_0/PLANE_1.
        image_data = b"".join(encode_gray4_bitplanes(dithered, get_gray4_codes(panel_ic_type)))
    else:
        image_data = encode_image(dithered, color_scheme)

    # Optionally compress
    compressed_data = None
    if compress:
        compressed_data = compress_image_data(image_data, level=6)

    return image_data, compressed_data, dithered


class OpenDisplayDevice:
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

        # Use theoretical ColorScheme instead of measured palettes
        async with OpenDisplayDevice(mac, use_measured_palettes=False) as device:
            await device.upload_image(image)
    """

    _ENCRYPTED_RESPONSE_MIN_LEN = 31  # cmd(2) + nonce(16) + payload(1) + tag(12)

    # BLE operation timeouts (seconds)
    TIMEOUT_FIRST_CHUNK = 10.0  # First chunk may take longer
    TIMEOUT_CONFIG_CHUNK = 2.0  # Subsequent config read chunks (interrogate)
    TIMEOUT_ACK = 5.0  # Command acknowledgments
    TIMEOUT_UNCOMPRESSED_DATA_ACK = 90.0  # Uncompressed DATA: bbepWriteData() blocks SPI on Spectra/ACeP (~60s max)
    TIMEOUT_UNCOMPRESSED_END_ACK = 90.0  # Uncompressed END: some firmware variants refresh before replying (~60s max)
    TIMEOUT_COMPRESSED_END_ACK = 90.0  # Compressed END: decompression + full SPI write to IC (~60s on Spectra/ACeP)
    TIMEOUT_REFRESH = 90.0  # Display refresh (firmware spec: up to 60s)

    def __init__(
        self,
        mac_address: str | None = None,
        device_name: str | None = None,
        ble_device: BLEDevice | None = None,
        config: GlobalConfig | None = None,
        capabilities: DeviceCapabilities | None = None,
        timeout: float = 10.0,
        discovery_timeout: float = 10.0,
        max_attempts: int = 4,
        use_services_cache: bool = True,
        use_measured_palettes: bool = True,
        encryption_key: bytes | None = None,
    ):
        """Initialize OpenDisplay device.

        Args:
            mac_address: Device MAC address (mutually exclusive with device_name)
            device_name: Device name to resolve via BLE scan (mutually exclusive with mac_address)
            ble_device: Optional BLEDevice from HA bluetooth integration
            config: Optional full TLV config (skips interrogation)
            capabilities: Optional minimal device info (skips interrogation)
            timeout: BLE operation timeout in seconds (default: 10)
            discovery_timeout: Timeout for name resolution scan (default: 10)
            max_attempts: Maximum connection attempts for bleak-retry-connector (default: 4)
            use_services_cache: Enable GATT service caching for faster reconnections (default: True)
            use_measured_palettes: Use measured color palettes when available (default: True)
            encryption_key: 16-byte AES-128 master key for encrypted devices (optional).

        Raises:
            ValueError: If neither or both mac_address and device_name provided
        """
        # Validation: exactly one of mac_address or device_name must be provided
        if mac_address and device_name:
            raise ValueError("Provide either mac_address or device_name, not both")
        if not mac_address and not device_name:
            raise ValueError("Must provide either mac_address or device_name")

        # Store for resolution in __aenter__
        self._mac_address_param = mac_address
        self._device_name = device_name
        self._discovery_timeout = discovery_timeout
        self._ble_device = ble_device
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._use_services_cache = use_services_cache
        self._use_measured_palettes = use_measured_palettes

        # Will be set after resolution
        self.mac_address = mac_address or ""  # Resolved in __aenter__
        self._connection: BLEConnection | None = None  # Created after MAC resolution

        self._config = config
        self._capabilities = capabilities
        self._fw_version: FirmwareVersion | None = None

        # Encryption session state (populated by authenticate())
        self._encryption_key = encryption_key
        self._session_key: bytes | None = None
        self._session_id: bytes | None = None
        self._nonce_counter: int = 0
        self._auth_time: float | None = None  # monotonic timestamp of last successful auth

    async def __aenter__(self) -> OpenDisplayDevice:
        """Connect and optionally interrogate device."""

        # Resolve device name to MAC address if needed
        if self._device_name:
            _LOGGER.debug("Resolving device name '%s' to MAC address", self._device_name)

            from .discovery import discover_devices
            from .exceptions import BLEConnectionError

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

        # Create connection with resolved MAC
        self._connection = BLEConnection(
            self.mac_address,
            self._ble_device,
            self._timeout,
            max_attempts=self._max_attempts,
            use_services_cache=self._use_services_cache,
        )

        await self._conn.connect()

        # Authenticate before any other commands if key provided
        if self._encryption_key is not None:
            await self.authenticate(self._encryption_key)

        # Auto-interrogate if no config or capabilities provided
        if self._config is None and self._capabilities is None:
            _LOGGER.info("No config provided, auto-interrogating device")
            await self.interrogate()

        # Extract capabilities from config if available
        if self._config and not self._capabilities:
            self._capabilities = self._extract_capabilities_from_config()

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

    @property
    def _conn(self) -> BLEConnection:
        """Return active BLE connection, raising RuntimeError if not connected."""
        if self._connection is None:
            raise RuntimeError("Device not connected")
        return self._connection

    async def _write(self, data: bytes) -> None:
        """Write a command, encrypting it if an active session exists."""
        if self._session_key is not None and self._session_id is not None:
            await self._reauthenticate_if_needed()
            cmd = data[:2]
            payload = data[2:]
            encrypted = encrypt_command(self._session_key, self._session_id, self._nonce_counter, cmd, payload)
            self._nonce_counter += 1
            await self._conn.write_command(encrypted)
        else:
            await self._conn.write_command(data)

    async def _reauthenticate_if_needed(self) -> None:
        """Re-authenticate proactively at 90% of session_timeout_seconds."""
        if self._encryption_key is None or self._auth_time is None:
            return
        if self._config is None or self._config.security_config is None:
            return
        timeout = self._config.security_config.session_timeout_seconds
        if timeout == 0:
            return
        elapsed = time.monotonic() - self._auth_time
        if elapsed >= timeout * 0.9:
            _LOGGER.info(
                "Session approaching timeout (%.0fs / %ds), re-authenticating",
                elapsed,
                timeout,
            )
            await self.authenticate(self._encryption_key)

    async def _read(self, timeout: float) -> bytes:
        """Read a response, decrypting it if an active session exists.

        Raises:
            AuthenticationRequiredError: If device returns 0xFE (encryption required, no active session)
        """
        raw = await self._conn.read_response(timeout=timeout)
        if self._session_key is not None:
            # Encrypted packets are at least cmd(2)+nonce(16)+payload(1)+tag(12)=31 bytes.
            # Shorter responses are sent unencrypted by the firmware even during a session:
            # direct-write ACKs (0x0070-0x0073) are always 2-byte plaintext; error frames
            # like {0xFF, 0xFF} (compressed buffer unavailable) are also unencrypted.
            if len(raw) >= self._ENCRYPTED_RESPONSE_MIN_LEN:
                cmd_code, payload = decrypt_response(self._session_key, raw)
                return cmd_code.to_bytes(2, "big") + payload
        # Firmware returns [cmd_high, cmd_low, 0xFE] (3 bytes) when a command
        # requires authentication but no session is active.
        if len(raw) == 3 and raw[2] == 0xFE:
            raise AuthenticationRequiredError(
                "Device requires an encryption key — pass encryption_key=bytes.fromhex('...') to OpenDisplayDevice"
            )
        return raw

    async def authenticate(self, key: bytes) -> None:
        """Perform two-step challenge-response authentication with the device.

        After successful authentication, all subsequent commands and responses
        are transparently encrypted/decrypted via _write() and _read().

        Args:
            key: 16-byte AES-128 master key

        Raises:
            AuthenticationFailedError: If the device rejects the key or is rate-limited
            InvalidResponseError: If device sends malformed response
        """
        _LOGGER.debug("Authenticating with device %s", self.mac_address)

        # Step 1: Request server nonce (retry once if device reports existing session)
        for attempt in range(2):
            await self._conn.write_command(build_authenticate_step1())
            challenge_data = await self._conn.read_response(timeout=self.TIMEOUT_ACK)
            try:
                server_nonce, device_id = parse_authenticate_challenge(challenge_data)
                break
            except AuthenticationSessionExistsError:
                if attempt == 1:
                    raise
                _LOGGER.debug("Device has active session, retrying for fresh challenge")

        # Step 2: Prove key knowledge, receive server proof
        client_nonce = generate_client_nonce()
        challenge = compute_challenge_response(key, server_nonce, client_nonce, device_id)
        await self._conn.write_command(build_authenticate_step2(client_nonce, challenge))
        success_response = await self._conn.read_response(timeout=self.TIMEOUT_ACK)
        parse_authenticate_success(success_response)  # raises on wrong key / error

        # Derive session key and ID
        self._session_key = derive_session_key(key, client_nonce, server_nonce, device_id)
        self._session_id = derive_session_id(self._session_key, client_nonce, server_nonce)
        self._nonce_counter = 0
        self._auth_time = time.monotonic()

        _LOGGER.info("Authentication successful, session established")

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

    def _ensure_manufacturer_data(self) -> BoardManufacturer | int:
        """Ensure manufacturer data is available and return board manufacturer."""
        if not self._config:
            raise RuntimeError("Device config unknown - interrogate first or provide config")
        return self._config.manufacturer.manufacturer_id_enum

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
        return self._ensure_manufacturer_data()

    def get_board_type(self) -> int:
        """Get raw board type ID from config.

        Requires config to be available via interrogation or constructor.

        Raises:
            RuntimeError: If config is missing.
        """
        if not self._config:
            raise RuntimeError("Device config unknown - interrogate first or provide config")
        return self._config.manufacturer.board_type

    def get_board_type_name(self) -> str | None:
        """Get human-readable board type name from config, if known.

        Requires config to be available via interrogation or constructor.

        Raises:
            RuntimeError: If config is missing.
        """
        if not self._config:
            raise RuntimeError("Device config unknown - interrogate first or provide config")
        return self._config.manufacturer.board_type_name

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
        if not self._config:
            raise RuntimeError("Device config unknown - interrogate first or provide config")
        tag_type = self._config.displays[0].tag_type if self._config.displays else 0
        return build_landing_url(
            tag_type,
            self._device_id_bytes(),
            self._encryption_key,
            self._config.manufacturer.manufacturer_id,
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
        await self._write(cmd)

        # Read first chunk
        response = await self._read(self.TIMEOUT_FIRST_CHUNK)
        chunk_data = strip_command_echo(response, CommandCode.READ_CONFIG)

        # Parse first chunk header
        total_length = int.from_bytes(chunk_data[2:4], "little")
        tlv_data = bytearray(chunk_data[4:])

        _LOGGER.debug("First chunk: %d bytes, total length: %d", len(chunk_data), total_length)

        # Read remaining chunks
        while len(tlv_data) < total_length:
            next_response = await self._read(self.TIMEOUT_CONFIG_CHUNK)
            next_chunk_data = strip_command_echo(next_response, CommandCode.READ_CONFIG)

            # Skip chunk number field (2 bytes) and append data
            tlv_data.extend(next_chunk_data[2:])

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
            "Firmware version: %d.%d (SHA: %s...)",
            self._fw_version["major"],
            self._fw_version["minor"],
            self._fw_version["sha"][:8],
        )

        return self._fw_version

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
            BLEConnectionError: If command cannot be sent
        """
        _LOGGER.debug("Sending reboot command to device %s", self.mac_address)

        # Build and send reboot command
        cmd = build_reboot_command()
        await self._write(cmd)

        # Device will reset immediately - no ACK expected
        _LOGGER.info("Reboot command sent to %s - device will reset (connection will drop)", self.mac_address)

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
            BLEConnectionError: If the command cannot be sent
        """
        from .exceptions import BLEConnectionError

        _LOGGER.debug("Triggering DFU bootloader on device %s", self.mac_address)
        try:
            await self._write(build_enter_dfu_command())
        except BLEConnectionError as exc:
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
            True if a cache was cleared, False if unsupported by the backend.

        Raises:
            BLEConnectionError: If the device is not connected.
        """
        return await self._conn.clear_cache()

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
        await self._write(cmd)

        response_timeout = self.TIMEOUT_REFRESH if timeout is None else timeout
        response = await self._read(response_timeout)

        # Firmware LED errors use 0xFF73 + error code payload.
        if len(response) >= 2 and unpack_command_code(response) == 0xFF73:
            error_code = response[2] if len(response) >= 3 else None
            if error_code is None:
                raise ProtocolError("LED activate failed with malformed error response")
            raise ProtocolError(f"LED activate failed: firmware error code 0x{error_code:02x}")

        validate_ack_response(response, CommandCode.LED_ACTIVATE)
        return response

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
        await self._write(cmd)

        response_timeout = self.TIMEOUT_REFRESH if timeout is None else timeout
        response = await self._read(response_timeout)
        validate_ack_response(response, CommandCode.BUZZER_ACTIVATE)
        return response

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
            BLEConnectionError: If write fails
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
        await self._write(first_cmd)

        # Wait for ACK
        response = await self._read(self.TIMEOUT_ACK)
        validate_ack_response(response, CommandCode.WRITE_CONFIG)

        # Send remaining chunks if needed
        for i, chunk_cmd in enumerate(chunk_cmds, start=1):
            _LOGGER.debug("Sending config chunk %d/%d (%d bytes)", i, len(chunk_cmds), len(chunk_cmd))
            await self._write(chunk_cmd)

            # Wait for ACK after each chunk
            response = await self._read(self.TIMEOUT_ACK)
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

        from .models import config_to_json

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

        from .models import config_from_json

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
        tone: float | str = 0.0,
        gamut: float | str = 0.0,
        fit: FitMode = FitMode.CONTAIN,
        rotate: Rotation = Rotation.ROTATE_0,
    ) -> tuple[bytes, bytes | None, Image.Image]:
        """Prepare image for upload. Internal wrapper for the module-level prepare_image()."""
        panel_ic_type = self._config.displays[0].panel_ic_type if self._config and self._config.displays else None
        return prepare_image(
            image,
            config=self._config,
            capabilities=self._ensure_capabilities(),
            use_measured_palettes=self._use_measured_palettes,
            panel_ic_type=panel_ic_type,
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
        )

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
        tone: float | str = 0.0,
        gamut: float | str = 0.0,
        fit: FitMode = FitMode.CONTAIN,
        rotate: Rotation = Rotation.ROTATE_0,
        progress_callback: Callable[[int, int], None] | None = None,
        state: PartialState | None = None,
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
            tone: Dynamic range compression — "auto", "off", or 0.0–1.0 (default: 0.0)
            gamut: Gamut compression — "auto", "off", or 0.0–1.0 (default: 0.0)
            fit: How to map the image to display dimensions (default: CONTAIN).
            rotate: Source image rotation enum, applied before fit/encoding.

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

        # Check compression support early to avoid wasted CPU in _prepare_image
        display_cfg = self._config.displays[0] if (self._config and self._config.displays) else None
        supports_compression = display_cfg.supports_zip if display_cfg else True

        # Prepare image (fit, dither, encode, compress)
        image_data, compressed_data, processed_image = self._prepare_image(
            image,
            dither_mode,
            compress and supports_compression,
            serpentine=serpentine,
            exposure=exposure,
            saturation=saturation,
            shadows=shadows,
            highlights=highlights,
            tone=tone,
            gamut=gamut,
            fit=fit,
            rotate=rotate,
        )

        if state is not None:
            partial_outcome = await self._maybe_upload_partial(processed_image, state, progress_callback)
            if partial_outcome == "success":
                _LOGGER.info("Image upload complete (partial path)")
                return processed_image
            if partial_outcome == "no_change":
                _LOGGER.info("No pixels changed; skipping upload")
                return processed_image
            if partial_outcome == "fallback_full":
                _LOGGER.info("Partial path unavailable or etag mismatch; continuing with full upload")

        upload_refresh_mode = RefreshMode.FULL if state is not None else refresh_mode
        full_upload_etag = _generate_etag() if state is not None else None
        await self._dispatch_upload(
            image_data,
            upload_refresh_mode,
            compress,
            compressed_data,
            progress_callback,
            new_etag=full_upload_etag,
        )

        _LOGGER.info("Image upload complete")
        if state is not None:
            self._update_partial_state(state, processed_image, image_data, full_upload_etag)
        return processed_image

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

        if state is not None:
            partial_outcome = await self._maybe_upload_partial(processed_image, state, progress_callback)
            if partial_outcome == "success":
                _LOGGER.info("Prepared image upload complete (partial path)")
                return
            if partial_outcome == "no_change":
                _LOGGER.info("No pixels changed; skipping prepared upload")
                return
            if partial_outcome == "fallback_full":
                _LOGGER.info("Partial prepared upload unavailable or etag mismatch; continuing with full upload")

        upload_refresh_mode = RefreshMode.FULL if state is not None else refresh_mode
        full_upload_etag = _generate_etag() if state is not None else None
        await self._dispatch_upload(
            image_data,
            upload_refresh_mode,
            compress,
            compressed_data,
            progress_callback,
            new_etag=full_upload_etag,
        )
        _LOGGER.info("Prepared image upload complete")
        if state is not None:
            self._update_partial_state(state, processed_image, image_data, full_upload_etag)

    async def _dispatch_upload(
        self,
        image_data: bytes,
        refresh_mode: RefreshMode,
        compress: bool,
        compressed_data: bytes | None,
        progress_callback: Callable[[int, int], None] | None,
        new_etag: int | None = None,
    ) -> None:
        """Choose compressed or uncompressed upload protocol and execute it."""
        display_cfg = self._config.displays[0] if (self._config and self._config.displays) else None
        supports_compression = display_cfg.supports_zip if display_cfg else True
        max_compressed = (
            MAX_COMPRESSED_SIZE_ZIPXL if (display_cfg and display_cfg.supports_zipxl) else MAX_COMPRESSED_SIZE
        )
        if compress and supports_compression and compressed_data and len(compressed_data) < max_compressed:
            _LOGGER.info("Using compressed upload protocol (size: %d bytes)", len(compressed_data))
            await self._execute_upload(
                image_data,
                refresh_mode,
                use_compression=True,
                compressed_data=compressed_data,
                uncompressed_size=len(image_data),
                progress_callback=progress_callback,
                new_etag=new_etag,
            )
        else:
            # 4-gray ships the same two split planes (plane0 ++ plane1) over either
            # transport; the firmware streams them to PLANE_0/PLANE_1 whether they
            # arrive compressed or as raw 0x71 chunks, so no special-casing here.
            if compress and not supports_compression:
                _LOGGER.info("Device does not support compressed uploads, using uncompressed protocol")
            elif compress and compressed_data:
                _LOGGER.info("Compressed size exceeds %d bytes, using uncompressed protocol", max_compressed)
            else:
                _LOGGER.info("Compression disabled or no compressed data, using uncompressed protocol")
            await self._execute_upload(
                image_data,
                refresh_mode,
                use_compression=False,
                progress_callback=progress_callback,
                new_etag=new_etag,
            )

    def _update_partial_state(
        self,
        state: PartialState,
        processed_image: Image.Image,
        image_data: bytes,
        etag: int | None = None,
    ) -> None:
        """After a successful full upload, refresh state to reflect what's now on the panel.

        Stores the etag committed to the device on 0x72 (or generates one if
        absent), then stashes the palette pixels for diffing on the next call.
        ``image_data`` is unused but kept for API symmetry.
        """
        del image_data
        palette_image = processed_image.convert("P") if processed_image.mode != "P" else processed_image
        state.etag = _generate_etag() if etag is None else etag
        state.last_image = palette_image.tobytes()
        state.width, state.height = processed_image.size
        state.bytes_per_pixel = 1

    async def _send_partial_chunks(
        self,
        remaining: bytes,
        stream_bytes: bytes,
        state: PartialState,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Send remaining 0x71 chunks and update upload progress."""
        chunk_size = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else CHUNK_SIZE
        total_stream_bytes = len(stream_bytes)
        bytes_sent = total_stream_bytes - len(remaining)
        offset = 0
        while offset < len(remaining):
            chunk = remaining[offset : offset + chunk_size]
            await self._write(build_direct_write_data_command(chunk))
            ack = await self._read(self.TIMEOUT_ACK)
            nack = parse_nack(ack)
            if nack is not None:
                opcode, err = nack
                state.etag = 0
                state.last_image = None
                raise ProtocolError(f"Partial 0x71 NACK: opcode=0x{opcode:02x} err=0x{err:02x}")
            validate_ack_response(ack, CommandCode.DIRECT_WRITE_DATA)
            offset += len(chunk)
            bytes_sent += len(chunk)
            if progress_callback is not None:
                progress_callback(bytes_sent, total_stream_bytes)

    async def _maybe_upload_partial(
        self,
        processed_image: Image.Image,
        state: PartialState,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Try a partial upload using the 0x76 single-rectangle protocol."""
        # Resolve all partial-update preconditions in one pass (support checks,
        # state validation, diff computation, and region alignment).
        region = compute_partial_region(processed_image, state, self._config, self.color_scheme)
        if isinstance(region, str):
            return region

        display = region.display
        _LOGGER.debug(
            "Partial path diff: old_etag=0x%08x, image=%dx%d, rect=(%d,%d,%d,%d)",
            state.etag,
            region.width,
            region.height,
            region.rx,
            region.ry,
            region.rw,
            region.rh,
        )

        # Build logical stream from the changed rectangle only, then compress
        # when it reduces the transfer size.
        old_palette_image = region.palette_image.copy()
        old_palette_image.frombytes(region.old_palette)
        old_rect_bytes = encode_segment_wire(
            old_palette_image,
            region.rx,
            region.ry,
            region.rw,
            region.rh,
            region.color_scheme,
        )
        new_rect_bytes = encode_segment_wire(
            region.palette_image,
            region.rx,
            region.ry,
            region.rw,
            region.rh,
            region.color_scheme,
        )

        logical_stream = build_partial_logical_stream(old_rect_bytes, new_rect_bytes)
        compressed_stream = zlib.compress(logical_stream, level=6)
        use_compression = display.supports_zip and len(compressed_stream) < len(logical_stream)
        stream_bytes = compressed_stream if use_compression else logical_stream

        flags = 0
        if use_compression:
            flags |= PARTIAL_FLAG_COMPRESSED

        _LOGGER.debug(
            "Partial stream: rect=(%d,%d,%d,%d), uncompressed=%d, wire=%d, compressed=%s",
            region.rx,
            region.ry,
            region.rw,
            region.rh,
            len(logical_stream),
            len(stream_bytes),
            use_compression,
        )

        new_etag = _generate_etag()
        _LOGGER.debug("Partial upload: old_etag=0x%08x new_etag=0x%08x", state.etag, new_etag)

        # Start partial upload (0x76), stream remaining 0x71 chunks, and finish
        # with partial refresh.
        start_pkt, remaining = build_direct_write_partial_start(
            old_etag=state.etag,
            new_etag=new_etag,
            flags=flags,
            x=region.rx,
            y=region.ry,
            width=region.rw,
            height=region.rh,
            stream_bytes=stream_bytes,
        )
        await self._write(start_pkt)
        try:
            response = await self._read(self.TIMEOUT_ACK)
            nack = parse_nack(response)
            if nack is not None:
                opcode, err = nack
                if opcode == 0x76 and err == ERR_ETAG_MISMATCH:
                    _LOGGER.info("Partial upload: etag mismatch; falling back to full upload")
                    state.etag = 0
                    state.last_image = None
                    return "fallback_full"
                raise ProtocolError(f"Partial 0x76 NACK: opcode=0x{opcode:02x} err=0x{err:02x}")
            validate_ack_response(response, CommandCode.DIRECT_WRITE_PARTIAL_START)
        except (BLETimeoutError, InvalidResponseError):
            _LOGGER.info("Partial upload start was not acknowledged; falling back to full upload")
            return "fallback_full"

        await self._send_partial_chunks(remaining, stream_bytes, state, progress_callback)

        await self._write(build_direct_write_end_command(RefreshMode.PARTIAL.value))
        response = await self._read(self.TIMEOUT_ACK)
        validate_ack_response(response, CommandCode.DIRECT_WRITE_END)

        response = await self._read(self.TIMEOUT_REFRESH)
        command, _ = check_response_type(response)
        if command == CommandCode.DIRECT_WRITE_REFRESH_TIMEOUT:
            raise ProtocolError("Display refresh timed out (device sent 0x74)")
        if command != CommandCode.DIRECT_WRITE_REFRESH_COMPLETE:
            raise ProtocolError(f"Unexpected response waiting for refresh: {command.name} (0x{command:04x})")

        state.etag = new_etag
        state.last_image = region.new_palette
        state.width = region.width
        state.height = region.height
        state.bytes_per_pixel = 1
        return "success"

    async def _execute_upload(
        self,
        image_data: bytes,
        refresh_mode: RefreshMode,
        use_compression: bool = False,
        compressed_data: bytes | None = None,
        uncompressed_size: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        new_etag: int | None = None,
    ) -> None:
        """Execute image upload using compressed or uncompressed protocol.

        Args:
            image_data: Raw uncompressed image data (always needed for uncompressed)
            refresh_mode: Display refresh mode
            use_compression: True to use compressed protocol
            compressed_data: Compressed data (required if use_compression=True)
            uncompressed_size: Original size (required if use_compression=True)

        Raises:
            ProtocolError: If upload fails
        """
        # 1. Send START command (different for each protocol)
        if use_compression:
            if uncompressed_size is None or compressed_data is None:
                raise ValueError("uncompressed_size and compressed_data are required when use_compression=True")
            max_start = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else MAX_START_PAYLOAD
            start_cmd, remaining_compressed = build_direct_write_start_compressed(
                uncompressed_size, compressed_data, max_start_payload=max_start
            )
        else:
            start_cmd = build_direct_write_start_uncompressed()
            remaining_compressed = None

        await self._write(start_cmd)

        # 2. Wait for START ACK — firmware initializes display hardware here, which can be slow
        response = await self._read(self.TIMEOUT_FIRST_CHUNK)
        try:
            validate_ack_response(response, CommandCode.DIRECT_WRITE_START)
        except InvalidResponseError:
            # If we weren't using compression there's nothing to fall back to.
            if not use_compression:
                raise
            # Device rejected the compressed START (e.g., compressedDataBuffer is NULL —
            # ZIPXL bit set in config but firmware not built with PSRAM support). Fall
            # back to the uncompressed protocol and retry; the same image_data (for
            # 4-gray, the two split planes) streams fine uncompressed.
            _LOGGER.warning(
                "Compressed START rejected by device (0x%04x); falling back to uncompressed",
                int.from_bytes(response[:2], "big"),
            )
            use_compression = False
            start_cmd = build_direct_write_start_uncompressed()
            await self._write(start_cmd)
            response = await self._read(self.TIMEOUT_FIRST_CHUNK)
            validate_ack_response(response, CommandCode.DIRECT_WRITE_START)

        # 3. Send data chunks
        auto_completed = False
        if use_compression:
            if remaining_compressed:
                auto_completed = await self._send_data_chunks(remaining_compressed, progress_callback)
        else:
            auto_completed = await self._send_data_chunks(
                image_data, progress_callback, chunk_timeout=self.TIMEOUT_UNCOMPRESSED_DATA_ACK
            )

        # 4. Send END (unless device auto-triggered refresh), then wait for 0x73
        if not auto_completed:
            end_cmd = (
                build_direct_write_end_with_etag(refresh_mode.value, new_etag)
                if new_etag is not None
                else build_direct_write_end_command(refresh_mode.value)
            )
            await self._write(end_cmd)

            # END triggers the SPI write and/or refresh on device before the ACK is sent.
            # Both paths can block up to ~60s on slow displays (Spectra/ACeP).
            end_ack_timeout = self.TIMEOUT_COMPRESSED_END_ACK if use_compression else self.TIMEOUT_UNCOMPRESSED_END_ACK
            response = await self._read(end_ack_timeout)
            validate_ack_response(response, CommandCode.DIRECT_WRITE_END)
        _LOGGER.debug("Display refresh started, waiting for completion...")

        response = await self._read(self.TIMEOUT_REFRESH)
        command, _ = check_response_type(response)
        if command == CommandCode.DIRECT_WRITE_REFRESH_TIMEOUT:
            raise ProtocolError("Display refresh timed out (device sent 0x74)")
        if command != CommandCode.DIRECT_WRITE_REFRESH_COMPLETE:
            raise ProtocolError(f"Unexpected response waiting for refresh: {command.name} (0x{command:04x})")
        _LOGGER.info("Display refresh complete")

    async def _send_data_chunks(
        self,
        image_data: bytes,
        progress_callback: Callable[[int, int], None] | None = None,
        chunk_timeout: float | None = None,
    ) -> bool:
        """Send image data chunks, waiting for ACK after each.

        Returns:
            True if the device sent 0x72 in place of a 0x71 ACK, meaning it
            auto-triggered the refresh (uncompressed protocol, buffer full).
            Caller must NOT send an explicit END in this case.
            False on normal completion — caller should send END.

        Raises:
            ProtocolError: If device responds with an unexpected code
            BLETimeoutError: If no response within chunk_timeout
        """
        timeout = chunk_timeout if chunk_timeout is not None else self.TIMEOUT_ACK
        bytes_sent = 0
        chunks_sent = 0

        while bytes_sent < len(image_data):
            chunk_size = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else CHUNK_SIZE
            chunk_data = image_data[bytes_sent : bytes_sent + chunk_size]

            await self._write(build_direct_write_data_command(chunk_data))
            bytes_sent += len(chunk_data)
            chunks_sent += 1

            if progress_callback is not None:
                progress_callback(bytes_sent, len(image_data))

            response = await self._read(timeout)
            command, _ = check_response_type(response)

            if command == CommandCode.DIRECT_WRITE_END:
                # Device auto-triggered refresh (buffer full) and sent 0x72
                # instead of 0x71. No explicit END should follow.
                _LOGGER.debug(
                    "Device auto-completed upload after %d bytes (%d chunks)",
                    bytes_sent,
                    chunks_sent,
                )
                return True

            if command != CommandCode.DIRECT_WRITE_DATA:
                raise ProtocolError(f"Unexpected response during upload: {command.name} (0x{command:04x})")

            if chunks_sent % 50 == 0 or bytes_sent >= len(image_data):
                _LOGGER.debug(
                    "Sent %d/%d bytes (%.1f%%)",
                    bytes_sent,
                    len(image_data),
                    bytes_sent / len(image_data) * 100,
                )

        _LOGGER.debug("All data chunks sent (%d chunks total)", chunks_sent)
        return False

    def _extract_capabilities_from_config(self) -> DeviceCapabilities:
        """Extract DeviceCapabilities from GlobalConfig.

        Returns:
            DeviceCapabilities with display info

        Raises:
            RuntimeError: If config missing or invalid
        """
        if not self._config:
            raise RuntimeError("No config available")

        if not self._config.displays:
            raise RuntimeError("Config has no display information")

        display = self._config.displays[0]  # Primary display

        r = display.rotation_enum
        try:
            color_scheme = ColorScheme.from_value(display.color_scheme)
        except ValueError as exc:
            raise ImageEncodingError(
                f"Device uses unsupported color scheme value {display.color_scheme}. "
                "Reconfigure the device to a supported color scheme (0–5)."
            ) from exc
        return DeviceCapabilities(
            width=display.pixel_width,
            height=display.pixel_height,
            color_scheme=color_scheme,
            rotation=r.value if isinstance(r, Rotation) else 0,
        )
