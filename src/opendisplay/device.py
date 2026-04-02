"""Main OpenDisplay BLE device class."""

from __future__ import annotations

import logging
import time
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
from .display_palettes import PANELS_4GRAY, get_palette_for_display
from .encoding import (
    compress_image_data,
    encode_bitplanes,
    encode_image,
    fit_image,
)
from .exceptions import AuthenticationRequiredError, AuthenticationSessionExistsError, BLETimeoutError, ProtocolError
from .models.capabilities import DeviceCapabilities
from .models.config import GlobalConfig
from .models.enums import BoardManufacturer, FitMode, RefreshMode, Rotation
from .models.firmware import FirmwareVersion
from .models.led_flash import LedFlashConfig
from .protocol import (
    CHUNK_SIZE,
    ENCRYPTED_CHUNK_SIZE,
    MAX_COMPRESSED_SIZE,
    CommandCode,
    build_authenticate_step1,
    build_authenticate_step2,
    build_direct_write_data_command,
    build_direct_write_end_command,
    build_direct_write_start_compressed,
    build_direct_write_start_uncompressed,
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
    tone_compression: float | str = "auto",
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
        tone_compression: Dynamic range compression ("auto", or 0.0-1.0)
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
        capabilities = DeviceCapabilities(
            width=display.pixel_width,
            height=display.pixel_height,
            color_scheme=ColorScheme.from_value(display.color_scheme),
            rotation=display.rotation,
        )

    if panel_ic_type is None and config is not None and config.displays:
        panel_ic_type = config.displays[0].panel_ic_type

    target_size = (capabilities.width, capabilities.height)
    image = _rotate_source_image(image, rotate)

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
    dithered = dither_image(image, palette, mode=dither_mode, tone_compression=tone_compression)

    # Encode to device format
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        plane1, plane2 = encode_bitplanes(dithered, color_scheme)
        image_data = plane1 + plane2
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

    # BLE operation timeouts (seconds)
    TIMEOUT_FIRST_CHUNK = 10.0  # First chunk may take longer
    TIMEOUT_CHUNK = 2.0  # Subsequent chunks
    TIMEOUT_ACK = 5.0  # Command acknowledgments
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
            next_response = await self._read(self.TIMEOUT_CHUNK)
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
        tone_compression: float | str = "auto",
        fit: FitMode = FitMode.STRETCH,
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
            tone_compression=tone_compression,
            fit=fit,
            rotate=rotate,
        )

    @staticmethod
    def _rotate_source_image(image: Image.Image, rotate: Rotation) -> Image.Image:
        """Rotate source image by enum value before fitting."""
        return _rotate_source_image(image, rotate)

    async def upload_image(
        self,
        image: Image.Image,
        refresh_mode: RefreshMode = RefreshMode.FULL,
        dither_mode: DitherMode = DitherMode.BURKES,
        compress: bool = True,
        tone_compression: float | str = "auto",
        fit: FitMode = FitMode.CONTAIN,
        rotate: Rotation = Rotation.ROTATE_0,
        progress_callback: Callable[[int, int], None] | None = None,
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
            tone_compression: Dynamic range compression ("auto" or 0.0–1.0, default: "auto").
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

        # Determine compression support before preparing to avoid wasted CPU
        supports_compression = (
            self._config.displays[0].supports_zip if (self._config and self._config.displays) else True
        )

        # Prepare image (fit, dither, encode, compress)
        image_data, compressed_data, processed_image = self._prepare_image(
            image, dither_mode, compress and supports_compression, tone_compression, fit, rotate
        )
        if compress and supports_compression and compressed_data and len(compressed_data) < MAX_COMPRESSED_SIZE:
            _LOGGER.info("Using compressed upload protocol (size: %d bytes)", len(compressed_data))
            await self._execute_upload(
                image_data,
                refresh_mode,
                use_compression=True,
                compressed_data=compressed_data,
                uncompressed_size=len(image_data),
                progress_callback=progress_callback,
            )
        else:
            if compress and not supports_compression:
                _LOGGER.info("Device does not support compressed uploads, using uncompressed protocol")
            elif compress and compressed_data:
                _LOGGER.info("Compressed size exceeds %d bytes, using uncompressed protocol", MAX_COMPRESSED_SIZE)
            else:
                _LOGGER.info("Compression disabled or no compressed data, using uncompressed protocol")
            await self._execute_upload(
                image_data, refresh_mode, use_compression=False, progress_callback=progress_callback
            )

        _LOGGER.info("Image upload complete")
        return processed_image

    async def upload_prepared_image(
        self,
        prepared_data: tuple[bytes, bytes | None, Image.Image],
        refresh_mode: RefreshMode = RefreshMode.FULL,
        compress: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
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
        image_data, compressed_data, _ = prepared_data

        supports_compression = (
            self._config.displays[0].supports_zip if (self._config and self._config.displays) else True
        )
        if compress and supports_compression and compressed_data and len(compressed_data) < MAX_COMPRESSED_SIZE:
            _LOGGER.info("Using compressed upload protocol (size: %d bytes)", len(compressed_data))
            await self._execute_upload(
                image_data,
                refresh_mode,
                use_compression=True,
                compressed_data=compressed_data,
                uncompressed_size=len(image_data),
                progress_callback=progress_callback,
            )
        else:
            if compress and not supports_compression:
                _LOGGER.info("Device does not support compressed uploads, using uncompressed protocol")
            elif compress and compressed_data:
                _LOGGER.info("Compressed size exceeds %d bytes, using uncompressed protocol", MAX_COMPRESSED_SIZE)
            else:
                _LOGGER.info("Compression disabled or no compressed data, using uncompressed protocol")
            await self._execute_upload(
                image_data, refresh_mode, use_compression=False, progress_callback=progress_callback
            )

        _LOGGER.info("Prepared image upload complete")

    async def _execute_upload(
        self,
        image_data: bytes,
        refresh_mode: RefreshMode,
        use_compression: bool = False,
        compressed_data: bytes | None = None,
        uncompressed_size: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
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
            assert uncompressed_size is not None and compressed_data is not None
            start_cmd, remaining_compressed = build_direct_write_start_compressed(uncompressed_size, compressed_data)
        else:
            start_cmd = build_direct_write_start_uncompressed()
            remaining_compressed = None

        await self._write(start_cmd)

        # 2. Wait for START ACK (identical for both protocols)
        response = await self._read(self.TIMEOUT_ACK)
        validate_ack_response(response, CommandCode.DIRECT_WRITE_START)

        # 3. Send data chunks
        auto_completed = False
        if use_compression:
            # Compressed upload: send remaining compressed data as chunks
            if remaining_compressed:
                auto_completed = await self._send_data_chunks(remaining_compressed, progress_callback)
        else:
            # Uncompressed upload: send raw image data as chunks
            auto_completed = await self._send_data_chunks(image_data, progress_callback)

        # 4. Send END command if needed (identical for both protocols)
        if not auto_completed:
            end_cmd = build_direct_write_end_command(refresh_mode.value)
            await self._write(end_cmd)

            # Wait for END ACK (90s timeout for display refresh)
            response = await self._read(self.TIMEOUT_REFRESH)
            validate_ack_response(response, CommandCode.DIRECT_WRITE_END)

    async def _send_data_chunks(
        self,
        image_data: bytes,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Send image data chunks with ACK handling.

        Sends image data in chunks via 0x0071 DATA commands. Handles:
        - Timeout recovery when firmware starts display refresh
        - Auto-completion detection (firmware sends 0x0072 END early)
        - Progress logging

        Args:
            image_data: Uncompressed encoded image data

        Returns:
            True if device auto-completed (sent 0x0072 END early)
            False if all chunks sent normally (caller should send END)

        Raises:
            ProtocolError: If unexpected response received
            BLETimeoutError: If no response within timeout
        """
        bytes_sent = 0
        chunks_sent = 0

        while bytes_sent < len(image_data):
            # Get next chunk
            chunk_start = bytes_sent
            chunk_size = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else CHUNK_SIZE
            chunk_end = min(chunk_start + chunk_size, len(image_data))
            chunk_data = image_data[chunk_start:chunk_end]

            # Send DATA command
            data_cmd = build_direct_write_data_command(chunk_data)
            await self._write(data_cmd)

            bytes_sent += len(chunk_data)
            chunks_sent += 1
            if progress_callback is not None:
                progress_callback(bytes_sent, len(image_data))

            # Wait for response after every chunk (PIPELINE_CHUNKS=1)
            try:
                response = await self._read(self.TIMEOUT_ACK)
            except BLETimeoutError:
                # Timeout on response - firmware might be doing display refresh
                # This happens when the chunk completes directWriteTotalBytes
                _LOGGER.info(
                    "No response after chunk %d (%.1f%%), waiting for device refresh...",
                    chunks_sent,
                    bytes_sent / len(image_data) * 100,
                )

                # Wait up to 90 seconds for the END response
                response = await self._read(self.TIMEOUT_REFRESH)

            # Check what response we got (firmware can send 0x0072 on ANY chunk, not just last!)
            command, _ = check_response_type(response)

            if command == CommandCode.DIRECT_WRITE_DATA:
                # Normal DATA ACK (0x0071) - continue sending chunks
                pass
            elif command == CommandCode.DIRECT_WRITE_END:
                # Firmware auto-triggered END (0x0072) after receiving all data
                # This happens when last chunk completes directWriteTotalBytes
                _LOGGER.info(
                    "Received END response after chunk %d - device auto-completed",
                    chunks_sent,
                )
                # Note: 0x0072 is sent AFTER display refresh completes (waitforrefresh(60))
                # So we're already done - no need to send our own 0x0072 END command!
                return True  # Auto-completed
            else:
                # Unexpected response
                raise ProtocolError(f"Unexpected response: {command.name} (0x{command:04x})")

            # Log progress every 50 chunks to reduce spam
            if chunks_sent % 50 == 0 or bytes_sent >= len(image_data):
                _LOGGER.debug(
                    "Sent %d/%d bytes (%.1f%%)",
                    bytes_sent,
                    len(image_data),
                    bytes_sent / len(image_data) * 100,
                )

        _LOGGER.debug("All data chunks sent (%d chunks total)", chunks_sent)
        return False  # Normal completion, caller should send END

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

        return DeviceCapabilities(
            width=display.pixel_width,
            height=display.pixel_height,
            color_scheme=ColorScheme.from_value(display.color_scheme),
            rotation=display.rotation,
        )
