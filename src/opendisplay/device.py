"""Main OpenDisplay BLE device class."""
# pylint: disable=too-many-lines,too-many-public-methods

from __future__ import annotations

import asyncio
import functools
import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, cast

from epaper_dithering import ColorScheme, DitherMode, dither_image
from PIL import Image

from .crypto import (
    compute_challenge_response,
    compute_server_proof,
    decrypt_response,
    derive_session_id,
    derive_session_key,
    encrypt_command,
    generate_client_nonce,
)
from .display_palettes import PANELS_4GRAY, get_bwry_codes, get_gray4_codes, get_palette_for_display
from .encoding import (
    FIRMWARE_ZLIB_WINDOW_BITS,
    compress_image_data,
    encode_2bpp,
    encode_bitplanes,
    encode_gray4_bitplanes,
    encode_image,
    fit_image,
    zlib_window_bits,
)
from .exceptions import (
    AuthenticationError,
    AuthenticationFailedError,
    AuthenticationRequiredError,
    AuthenticationSessionExistsError,
    BLETimeoutError,
    ImageEncodingError,
    IntegrityCheckError,
    InvalidResponseError,
    NfcNotSupportedError,
    ProtocolError,
    RefreshTimeoutError,
    TruncatedConfigError,
)
from .landing import build_landing_url
from .models.buzzer_activate import BuzzerActivateConfig
from .models.capabilities import DeviceCapabilities
from .models.config import GlobalConfig
from .models.enums import BoardManufacturer, FitMode, NfcRecordType, RefreshMode, Rotation
from .models.firmware import FirmwareVersion
from .models.led_flash import LedFlashConfig
from .partial import (
    PARTIAL_FLAG_COMPRESSED,
    PartialRegion,
    PartialState,
    _generate_etag,
    build_partial_logical_stream,
    compute_partial_region,
    encode_segment_wire,
    parse_nack,
)
from .protocol import (
    CHUNK_SIZE,
    DEFAULT_MAX_FRAME,
    ENCRYPTED_CHUNK_SIZE,
    MAX_COMPRESSED_SIZE,
    MAX_PTO,
    MAX_START_PAYLOAD,
    NFC_CHUNK_SIZE,
    NFC_INLINE_MAX,
    NFC_WRITE_MAX_TOTAL,
    PIPE_FLAG_PARTIAL,
    PIPE_FRAME_OVERHEAD,
    TIMEOUT_PIPE_START,
    CommandCode,
    PipeParams,
    PipePartialRequest,
    build_authenticate_step1,
    build_authenticate_step2,
    build_buzzer_activate_command,
    build_deep_sleep_command,
    build_direct_write_data_command,
    build_direct_write_end_command,
    build_direct_write_end_with_etag,
    build_direct_write_partial_start,
    build_direct_write_start_compressed,
    build_direct_write_start_uncompressed,
    build_enter_dfu_command,
    build_led_activate_command,
    build_nfc_write_data_command,
    build_nfc_write_end_command,
    build_nfc_write_inline_command,
    build_nfc_write_start_command,
    build_pipe_write_data_command,
    build_pipe_write_end_command,
    build_pipe_write_start_command,
    build_read_config_command,
    build_read_fw_version_command,
    build_reboot_command,
    build_write_config_command,
    classify_pipe_frame,
    parse_config_response,
    parse_firmware_version,
    parse_pipe_data_ack,
    parse_pipe_data_nack,
    parse_pipe_start_response,
    serialize_config,
    unpack_ack_ranges,
    validate_ack_response,
)
from .protocol.responses import (
    NFC_STATUS_CHUNK_ACK,
    NFC_STATUS_WRITE_OK,
    PIPE_FRAME_ACK,
    PIPE_FRAME_END_ACK,
    PIPE_FRAME_END_NACK,
    PIPE_FRAME_NACK,
    PIPE_START_NACK_COMPRESSION,
    PIPE_START_NACK_ETAG_MISMATCH,
    PIPE_START_NACK_PARTIAL_UNSUPPORTED,
    PIPE_START_NACK_RECT_INVALID,
    check_response_type,
    is_compressed_failure_frame,
    parse_authenticate_challenge,
    parse_authenticate_success,
    strip_command_echo,
    unpack_command_code,
    validate_nfc_response,
)
from .transport import BLEConnection

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)


class _PipePartialEtagMismatch(Exception):
    """Internal: the device NACKed a pipe-partial START with 0x05 (etag mismatch).

    The device has already cleared its displayed_etag, so a 0x76 retry would
    mismatch again — the caller clears PartialState and goes straight to full.
    """


class _PipePartialRejected(Exception):
    """Internal: the device NACKed a pipe-partial START with 0x06/0x07.

    0x06 = partial unsupported (bpp/driver), 0x07 = rect invalid. The 0x76
    fallback runs identical checks and would fail identically, so the caller
    clears PartialState and goes straight to full.
    """


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


def _capabilities_from_config(config: GlobalConfig) -> DeviceCapabilities:
    if not config.displays:
        raise RuntimeError("Config has no display information")

    display = config.displays[0]
    rotation = display.rotation_enum
    return DeviceCapabilities(
        width=display.pixel_width,
        height=display.pixel_height,
        color_scheme=ColorScheme.from_value(display.color_scheme),
        rotation=rotation.value if isinstance(rotation, Rotation) else 0,
    )


# pixels-per-byte for each direct-write color scheme. GRAYSCALE_4 is omitted:
# firmware row-pads its upload, so a non-aligned width is safe there.
_DIRECT_WRITE_PIXELS_PER_BYTE: dict[ColorScheme, int] = {
    ColorScheme.MONO: 8,
    ColorScheme.BWR: 8,
    ColorScheme.BWY: 8,
    ColorScheme.BWRY: 4,
    ColorScheme.BWGBRY: 2,
    ColorScheme.GRAYSCALE_16: 2,
}


def _warn_firmware_upload_limitations(color_scheme: ColorScheme, width: int) -> None:
    """Warn about known device-firmware upload bugs the library can't fix on-device.

    - BWR/BWY direct write drops the red/yellow plane on current firmware (C1).
    - Widths not aligned to the scheme's byte boundary are truncated because the
      firmware sizes the upload from the raw pixel count (C2). GRAYSCALE_4 is
      exempt because firmware row-pads it.
    """
    if color_scheme in (ColorScheme.BWR, ColorScheme.BWY):
        _LOGGER.warning(
            "Color scheme %s (BWR/BWY direct write) is not reliably supported by current "
            "firmware: it stores only one plane, so the red/yellow layer is discarded and "
            "the black/white layer renders over stale color RAM. Output may be wrong until "
            "device firmware gains BWR/BWY parity.",
            color_scheme.name,
        )

    ppb = _DIRECT_WRITE_PIXELS_PER_BYTE.get(color_scheme)
    if ppb is not None and width % ppb != 0:
        _LOGGER.warning(
            "Panel width %d is not a multiple of %d for color scheme %s; current firmware "
            "sizes the upload from the raw pixel count and will truncate the last rows on "
            "the device. A byte-aligned width avoids this.",
            width,
            ppb,
            color_scheme.name,
        )


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
        if config is None:
            raise RuntimeError("Config has no display information")
        capabilities = _capabilities_from_config(config)

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

    _warn_firmware_upload_limitations(color_scheme, capabilities.width)

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
    elif color_scheme == ColorScheme.BWRY:
        # Some YR panels (0x001D/0x001E) use a native 4-color code order with
        # yellow/red swapped relative to the dither palette; apply the per-panel
        # code table so the firmware's raw-nibble direct write shows the right color.
        image_data = encode_2bpp(dithered, codes=get_bwry_codes(panel_ic_type))
    else:
        image_data = encode_image(dithered, color_scheme)

    # Optionally compress
    compressed_data = None
    if compress:
        # Current firmware compiles uzlib with a 9-bit window and hard-rejects any
        # zlib header advertising more, so always compress with a 9-bit window
        # regardless of transmission_modes: a 9-bit stream decodes fine on any firmware whose
        # window is >= 9 (the firmware check is <=).
        compressed_data = compress_image_data(image_data, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS)

    return image_data, compressed_data, dithered


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

        # Use theoretical ColorScheme instead of measured palettes
        async with OpenDisplayDevice(mac, use_measured_palettes=False) as device:
            await device.upload_image(image)
    """

    _ENCRYPTED_RESPONSE_MIN_LEN = 31  # cmd(2) + nonce(16) + payload(1) + tag(12)

    # BLE operation timeouts (seconds)
    TIMEOUT_FIRST_CHUNK = 10.0  # First chunk may take longer
    TIMEOUT_CONFIG_CHUNK = 2.0  # Subsequent config read chunks (interrogate)
    TIMEOUT_ACK = 5.0  # Command acknowledgments
    TIMEOUT_NFC_WRITE = 15.0  # NFC EEPROM commit (inline write / chunk end): slow I2C work
    TIMEOUT_UNCOMPRESSED_DATA_ACK = 90.0  # Uncompressed DATA: bbepWriteData() blocks SPI on Spectra/ACeP (~60s max)
    TIMEOUT_UNCOMPRESSED_END_ACK = 90.0  # Uncompressed END: some firmware variants refresh before replying (~60s max)
    TIMEOUT_COMPRESSED_END_ACK = 90.0  # Compressed END: decompression + full SPI write to IC (~60s on Spectra/ACeP)
    TIMEOUT_REFRESH = 90.0  # Display refresh (firmware spec: up to 60s)

    # PIPE_WRITE per-path progress timeouts (Part 1 §1.4): a compressed chunk lands
    # fast; an uncompressed chunk can block bbepWriteData on SPI for the Spectra/ACeP
    # ~60s SPI-block budget, so 90s preserves it.
    TIMEOUT_PIPE_DATA_COMPRESSED = 5.0
    TIMEOUT_PIPE_DATA_UNCOMPRESSED = 90.0
    # Compressed tail-flush: firmware ACKs only every N_eff accepted frames, so a
    # tail of < N_eff unacked frames never earns a cadence ACK on its own. Rather
    # than stalling chunk_timeout (5 s) waiting for one, block briefly and then
    # dup-probe (resend the oldest unacked chunk) — the duplicate elicits an
    # immediate ACK from firmware. Never applied to the uncompressed path, whose
    # 90 s budget covers legitimate SPI stalls.
    TIMEOUT_PIPE_TAIL_FLUSH = 0.5

    # Version gate sentinel: None ⇒ version gating disabled, the 0x0080 probe is
    # authoritative. Pin a (major, minor) tuple once a firmware release ships PIPE_WRITE.
    PIPE_MIN_FW: tuple[int, int] | None = None

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
        blocks_per_ack: int = 8,
        max_queue_size: int = 16,
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
            blocks_per_ack: Requested PIPE_WRITE ACK cadence N (blocks per ack), 1..32
                (default: 8). Negotiated down to the device maximum.
            max_queue_size: Requested PIPE_WRITE window W (tokens in flight), 1..32
                (default: 16). ``max_queue_size <= 1`` disables sliding-window fast
                transfer entirely — legacy stop-and-wait only, no 0x0080 probe.

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

        # Serializes command round-trips (see _serialized / _transaction).
        self._command_lock = asyncio.Lock()
        self._lock_owner: asyncio.Task[object] | None = None

        # Sliding-window (PIPE_WRITE) tuning + per-connection capability cache.
        self._blocks_per_ack = blocks_per_ack
        self._max_queue_size = max_queue_size
        self._pipe_params: PipeParams | None = None  # active transfer only
        self._pipe_probed: bool = False  # capability determined this connection
        self._pipe_supported: bool = False  # probe result (valid iff _pipe_probed)
        # Pipe-partial support is inferred, then confirmed by the 0x0080 ACK flags
        # bit1: None = unknown, True = confirmed, False = rejected this connection
        # (older pipe-capable firmware NACKs the partial flag with 0x02).
        self._pipe_partial_supported: bool | None = None

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
            disconnected_callback=self._on_ble_disconnect,
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
        # Forget the session so a reused device object re-authenticates cleanly.
        self._clear_session()

    @property
    def _conn(self) -> BLEConnection:
        """Return active BLE connection, raising RuntimeError if not connected."""
        if self._connection is None:
            raise RuntimeError("Device not connected")
        return self._connection

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

    def _clear_session(self) -> None:
        """Drop any encryption session state.

        Called on disconnect so a reused device object does not encrypt against
        a session the firmware has already torn down.
        """
        self._session_key = None
        self._session_id = None
        self._nonce_counter = 0
        self._auth_time = None

    def _on_ble_disconnect(self) -> None:
        """Handle an unexpected BLE drop: forget the (now-dead) session and pipe state."""
        _LOGGER.debug("Link to %s dropped; clearing session state", self.mac_address)
        self._clear_session()
        # All pipe negotiation/capability state is per-connection (Part 1 §1.1).
        self._pipe_probed = False
        self._pipe_supported = False
        self._pipe_partial_supported = None
        self._pipe_params = None

    def _encrypt_frame(self, data: bytes) -> bytes:
        """Encrypt one plaintext command frame under the active session.

        Advances the nonce counter by one so every transmission — including a
        PIPE_WRITE retransmission — carries a fresh, higher nonce. Returns ``data``
        unchanged when no session is active. Does NOT re-authenticate (callers
        handle re-auth once before a stream, never mid-stream).
        """
        if self._session_key is not None and self._session_id is not None:
            cmd = data[:2]
            payload = data[2:]
            frame = encrypt_command(self._session_key, self._session_id, self._nonce_counter, cmd, payload)
            self._nonce_counter += 1
            return frame
        return data

    async def _write(self, data: bytes, response: bool = True) -> None:
        """Write a command, encrypting it if an active session exists.

        Args:
            data: Plaintext command frame (opcode + payload).
            response: Passed through to the transport. False requests a BLE Write
                Without Response (used for 0x71 data chunks); applies whether or not
                the frame is encrypted.
        """
        if self._session_key is not None and self._session_id is not None:
            await self._reauthenticate_if_needed()
            await self._conn.write_command(self._encrypt_frame(data), response=response)
        else:
            await self._conn.write_command(data, response=response)

    async def _write_pipe_frame(self, data: bytes, *, response: bool) -> None:
        """Encrypt (no re-auth) and write a live PIPE_WRITE stream frame.

        Passes ``drain_stale=False`` so queued sliding-window ACKs are preserved.
        Used for every 0x0081 DATA frame (response=False) and the 0x0082 END
        (response=True). Re-authentication is intentionally skipped: it runs once
        before 0x0080 and never mid-stream (Part 1 §1.6).
        """
        await self._conn.write_command(self._encrypt_frame(data), response=response, drain_stale=False)

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
            IntegrityCheckError: If device returns 0xFF (decrypt/integrity check failed, command not executed)
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
        # Firmware returns [cmd_high, cmd_low, 0xFF] (3 bytes) when an encrypted
        # command fails AES-GCM decryption / tag verification. The command was
        # NOT executed, so it must not be treated as an ACK — the 2-byte echo
        # otherwise matches the expected command code and passes validation.
        # Distinct from the 2-byte {0xFF, 0xFF} compressed-failure frame and
        # 4-byte NACK frames, which have different lengths.
        if len(raw) == 3 and raw[2] == 0xFF:
            raise IntegrityCheckError(
                f"Device rejected command 0x{unpack_command_code(raw):04x}: "
                "decryption/integrity check failed (command not executed) — likely a dropped or corrupted packet"
            )
        return raw

    @_serialized
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
        server_proof = parse_authenticate_success(success_response)  # raises on wrong key / error

        # Derive session key and ID
        session_key = derive_session_key(key, client_nonce, server_nonce, device_id)

        # Verify the device's mutual-auth proof so we authenticate the device, not
        # just the other way around. A device (or MITM) that returns status OK
        # without knowing the master key cannot produce this CMAC. Constant-time
        # compare to avoid leaking a timing side channel.
        expected_proof = compute_server_proof(session_key, server_nonce, client_nonce, device_id)
        if not hmac.compare_digest(server_proof, expected_proof):
            raise AuthenticationFailedError("Device failed mutual authentication (server proof mismatch)")

        self._session_key = session_key
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
        await self._write(cmd)

        # Read first chunk
        response = await self._read(self.TIMEOUT_FIRST_CHUNK)

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
                next_response = await self._read(self.TIMEOUT_CONFIG_CHUNK)
            except BLETimeoutError as err:
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
            "Firmware version: %d.%d (SHA: %s...)",
            self._fw_version["major"],
            self._fw_version["minor"],
            self._fw_version["sha"][:8],
        )

        return self._fw_version

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
            BLEConnectionError: If command cannot be sent
        """
        _LOGGER.debug("Sending reboot command to device %s", self.mac_address)

        # Build and send reboot command
        cmd = build_reboot_command()
        await self._write(cmd)

        # Device will reset immediately - no ACK expected
        _LOGGER.info("Reboot command sent to %s - device will reset (connection will drop)", self.mac_address)

    @_serialized
    async def deep_sleep(self) -> None:
        """Put the device into deep sleep (command 0x0052).

        Supported on ESP32 and Silabs Flex; nRF targets do not implement deep
        sleep. The command is sent encrypted when an active session exists.

        The firmware's exact behavior depends on the target, and this method
        tolerates all of them — in every supported case the BLE link drops during
        or right after the command, so a disconnect (or a missing ACK) is treated
        as success, mirroring reboot() and trigger_dfu_bootloader():

        - ESP32 with a D-FF power latch: firmware ACKs 0x0052, then powers off
          after ~100 ms (the link drops).
        - ESP32 without a power latch: firmware enters deep sleep immediately,
          tearing down BLE with no ACK (the write or read fails as the link drops).
        - Silabs Flex: firmware ACKs 0x0052, then closes the connection and enters
          EM4 (wake on button/NFC).

        Raises:
            ProtocolError: If the device explicitly reports that deep sleep is not
                supported (protocol error frame 0xFF52).
        """
        from .exceptions import BLEConnectionError

        _LOGGER.debug("Sending deep sleep command (0x0052) to device %s", self.mac_address)

        try:
            await self._write(build_deep_sleep_command())
        except BLEConnectionError as exc:
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
        # with 0x0052 and then drop the link; a device that does not support the
        # command replies with the 0xFF52 error frame (protocol: 0xFF [command_low]).
        # A disconnect or timeout here means the device slept without acking.
        try:
            response = await self._read(self.TIMEOUT_ACK)
        except (BLEConnectionError, BLETimeoutError) as exc:
            _LOGGER.debug(
                "No deep sleep ACK (expected — device dropped the link or sleeps silently): %s",
                exc,
            )
            _LOGGER.info("Deep sleep command sent to %s — device is sleeping", self.mac_address)
            return

        if len(response) >= 2 and unpack_command_code(response) == 0xFF52:
            raise ProtocolError("Device reported deep sleep is not supported (command 0x0052)")

        validate_ack_response(response, CommandCode.DEEP_SLEEP)
        _LOGGER.info("Deep sleep command acknowledged by %s — device is sleeping", self.mac_address)

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
        await self._write(cmd)

        response_timeout = self.TIMEOUT_REFRESH if timeout is None else timeout
        response = await self._read(response_timeout)
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
        if not 1 <= len(payload) <= NFC_WRITE_MAX_TOTAL:
            raise ValueError(f"payload length must be 1..{NFC_WRITE_MAX_TOTAL}, got {len(payload)}")

        commit_timeout = self.TIMEOUT_NFC_WRITE if timeout is None else timeout
        first_read = True

        async def _read_nfc(read_timeout: float) -> bytes:
            nonlocal first_read
            try:
                response = await self._read(read_timeout)
            except BLETimeoutError:
                if first_read:
                    raise NfcNotSupportedError() from None
                raise
            finally:
                first_read = False
            return response

        if len(payload) <= NFC_INLINE_MAX:
            await self._write(build_nfc_write_inline_command(int(rec_type), payload))
            response = await _read_nfc(commit_timeout)
            validate_nfc_response(response, NFC_STATUS_WRITE_OK)
            return

        await self._write(build_nfc_write_start_command(int(rec_type), len(payload)))
        response = await _read_nfc(self.TIMEOUT_ACK)
        validate_nfc_response(response, NFC_STATUS_CHUNK_ACK)

        for offset in range(0, len(payload), NFC_CHUNK_SIZE):
            chunk = payload[offset : offset + NFC_CHUNK_SIZE]
            await self._write(build_nfc_write_data_command(chunk))
            response = await _read_nfc(self.TIMEOUT_ACK)
            validate_nfc_response(response, NFC_STATUS_CHUNK_ACK)

        await self._write(build_nfc_write_end_command())
        response = await _read_nfc(commit_timeout)
        validate_nfc_response(response, NFC_STATUS_WRITE_OK)

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

        # Check compression support early to avoid wasted CPU in _prepare_image.
        # Post-2.0 configs may advertise only streaming decompression (bit 0x01,
        # historically ZIPXL) without the plain ZIP bit; firmware 2.0 accepts
        # compressed uploads either way (<=1.81 NACKs without the ZIP bit and
        # the upload falls back to uncompressed).
        display_cfg = self._config.displays[0] if (self._config and self._config.displays) else None
        supports_compression = (
            (display_cfg.supports_zip or display_cfg.supports_streaming_decompression) if display_cfg else True
        )

        # When a partial upload may succeed, defer full-frame compression: it is
        # pure waste if the partial path handles the update. _dispatch_upload
        # compresses lazily on the full-upload fallback.
        prepare_compress = compress and supports_compression and state is None

        # Prepare image (fit, dither, encode, compress)
        image_data, compressed_data, processed_image = self._prepare_image(
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
        etag_committed = await self._dispatch_upload(
            image_data,
            upload_refresh_mode,
            compress,
            compressed_data,
            progress_callback,
            new_etag=full_upload_etag,
        )

        _LOGGER.info("Image upload complete")
        if state is not None:
            self._update_partial_state(state, processed_image, image_data, full_upload_etag if etag_committed else None)
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
        etag_committed = await self._dispatch_upload(
            image_data,
            upload_refresh_mode,
            compress,
            compressed_data,
            progress_callback,
            new_etag=full_upload_etag,
        )
        _LOGGER.info("Prepared image upload complete")
        if state is not None:
            self._update_partial_state(state, processed_image, image_data, full_upload_etag if etag_committed else None)

    async def _dispatch_upload(
        self,
        image_data: bytes,
        refresh_mode: RefreshMode,
        compress: bool,
        compressed_data: bytes | None,
        progress_callback: Callable[[int, int], None] | None,
        new_etag: int | None = None,
    ) -> bool:
        """Choose compressed or uncompressed upload protocol and execute it.

        Returns True if the device committed ``new_etag`` (END-with-etag was
        sent), False if the firmware auto-completed the upload.
        """
        display_cfg = self._config.displays[0] if (self._config and self._config.displays) else None
        supports_compression = (
            (display_cfg.supports_zip or display_cfg.supports_streaming_decompression) if display_cfg else True
        )
        streaming_decompression = bool(display_cfg and display_cfg.supports_streaming_decompression)
        if (
            compress
            and supports_compression
            and (
                compressed_data is None
                or (streaming_decompression and zlib_window_bits(compressed_data) != FIRMWARE_ZLIB_WINDOW_BITS)
            )
        ):
            # Firmware only accepts zlib streams with a <=9-bit window (see
            # prepare_image), so the lazy deferred/partial-fallback compression
            # must use it too.
            compressed_data = compress_image_data(image_data, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS)

        within_compressed_limit = compressed_data is not None and (
            # The 50 KB cap protects the old buffered decompressor; streaming
            # decompression (bit 0x01) has no whole-blob buffer.
            streaming_decompression or len(compressed_data) < MAX_COMPRESSED_SIZE
        )
        if compress and supports_compression and compressed_data and within_compressed_limit:
            _LOGGER.info(
                "Using compressed upload protocol (size: %d bytes, zlib window: %d bits)",
                len(compressed_data),
                zlib_window_bits(compressed_data) or 0,
            )
            return await self._execute_upload(
                image_data,
                refresh_mode,
                use_compression=True,
                compressed_data=compressed_data,
                uncompressed_size=len(image_data),
                progress_callback=progress_callback,
                new_etag=new_etag,
            )

        # 4-gray ships the same two split planes (plane0 ++ plane1) over either
        # transport; the firmware streams them to PLANE_0/PLANE_1 whether they
        # arrive compressed or as raw 0x71 chunks, so no special-casing here.
        if compress and not supports_compression:
            _LOGGER.info("Device does not support compressed uploads, using uncompressed protocol")
        elif compress and compressed_data:
            _LOGGER.info("Compressed size exceeds %d bytes, using uncompressed protocol", MAX_COMPRESSED_SIZE)
        else:
            _LOGGER.info("Compression disabled or no compressed data, using uncompressed protocol")
        return await self._execute_upload(
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
        """After a full upload, refresh state to reflect what's now on the panel.

        ``etag`` is the etag committed to the device via END-with-etag, or None
        if the upload auto-completed (no etag was committed). When None, the
        device's displayed_etag was never set, so populating state.etag would
        make the next partial attempt fail ERR_ETAG_MISMATCH (or match a stale
        device etag and render against the wrong old plane). In that case
        invalidate the partial state so the next upload goes full.
        ``image_data`` is unused but kept for API symmetry.
        """
        del image_data
        if etag is None:
            state.etag = 0
            state.last_image = None
            return
        palette_image = processed_image.convert("P") if processed_image.mode != "P" else processed_image
        state.etag = etag
        state.last_image = palette_image.tobytes()
        state.width, state.height = processed_image.size
        state.bytes_per_pixel = 1

    async def _send_partial_chunks(
        self,
        remaining: bytes,
        stream_bytes: bytes,
        state: PartialState,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Send remaining 0x71 chunks and update upload progress.

        Returns "success", or "fallback_full" if the device NACKed a chunk
        before the refresh started (firmware aborts the partial on such a NACK,
        so a subsequent full upload is safe).
        """
        chunk_size = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else CHUNK_SIZE
        total_stream_bytes = len(stream_bytes)
        bytes_sent = total_stream_bytes - len(remaining)
        offset = 0
        while offset < len(remaining):
            chunk = remaining[offset : offset + chunk_size]
            # Write Without Response; the per-chunk ACK read below keeps flow control.
            await self._write(build_direct_write_data_command(chunk), response=False)
            ack = await self._read(self.TIMEOUT_ACK)
            nack = parse_nack(ack)
            if nack is not None:
                opcode, err = nack
                _LOGGER.info(
                    "Partial upload rejected mid-stream (opcode=0x%02x err=0x%02x); falling back to full upload",
                    opcode,
                    err,
                )
                state.etag = 0
                state.last_image = None
                return "fallback_full"
            validate_ack_response(ack, CommandCode.DIRECT_WRITE_DATA)
            offset += len(chunk)
            bytes_sent += len(chunk)
            if progress_callback is not None:
                progress_callback(bytes_sent, total_stream_bytes)
        return "success"

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
        # A partial stream rides inside the 0x76 initial bytes; firmware only
        # accepts a <= 9-bit zlib window, so always use a 9-bit window (a 15-bit
        # window would be NACKed with ERR_PARTIAL_STREAM by 9-bit firmware).
        compressed_stream = compress_image_data(logical_stream, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS)
        use_compression = (display.supports_zip or display.supports_streaming_decompression) and len(
            compressed_stream
        ) < len(logical_stream)
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

        # Pipe-first: ride the sliding window when the device advertises PIPE_WRITE
        # and pipe-partial has not been disabled this connection. partial_update_support
        # != NONE is already guaranteed (compute_partial_region gated on it). Falls
        # back to the legacy 0x76 flow below on any None result.
        if (
            display.supports_pipe_write
            and self._max_queue_size > 1
            and not (self._pipe_probed and not self._pipe_supported)
            and self._pipe_partial_supported is not False
        ):
            total_size = len(logical_stream)
            try:
                params = await self._negotiate_pipe_partial(use_compression, total_size, state.etag, region)
            except _PipePartialEtagMismatch:
                # Device already cleared its etag — a 0x76 retry would mismatch
                # again, so skip straight to a full upload.
                _LOGGER.info("pipe-partial etag mismatch (0x05); clearing state, falling back to full")
                state.etag = 0
                state.last_image = None
                return "fallback_full"
            except _PipePartialRejected as exc:
                # 0x76 runs identical bpp/rect checks and would fail identically.
                _LOGGER.info("pipe-partial rejected (%s); clearing state, falling back to full", exc)
                state.etag = 0
                state.last_image = None
                return "fallback_full"
            if params is not None:
                # Negotiation may have downgraded compressed→uncompressed (NACK 0x02).
                payload = compressed_stream if params.compressed else logical_stream
                try:
                    await self._run_pipe_upload(payload, params, RefreshMode.PARTIAL, progress_callback, new_etag)
                except RefreshTimeoutError:
                    # 0x74 after END_ACK: firmware already cleared its etag on
                    # the failed refresh; re-raise (parity with the 0x76 path).
                    # Note BLETimeoutError on the post-DATA reads deliberately
                    # propagates too (not caught below): the END may already have
                    # committed on the device, so falling back to full and
                    # re-baselining state on an unknown outcome would be unsafe.
                    raise
                except (AuthenticationError, IntegrityCheckError):
                    # An auth/session failure or a decrypt-integrity rejection is NOT
                    # a clean, safe-to-retry protocol abort. Auth errors MUST surface
                    # so the caller (e.g. Home Assistant) can trigger reauth instead
                    # of silently retrying; an integrity failure signals an out-of-sync
                    # encrypted channel that a full upload over the same session would
                    # likely hit again. Masking either as a full-upload fallback would
                    # hide it, so re-raise. Only the genuine protocol NACKs below are
                    # safe to recover from with a full upload.
                    raise
                except ProtocolError as exc:
                    # Mid-stream NACK / MAX_RETX / END NACK before any refresh — the
                    # transfer aborted cleanly, so a full upload is safe.
                    _LOGGER.info("pipe-partial upload failed (%s); clearing state, falling back to full", exc)
                    state.etag = 0
                    state.last_image = None
                    return "fallback_full"
                # Success — commit partial state (mirrors the 0x76 success path).
                state.etag = new_etag
                state.last_image = region.new_palette
                state.width = region.width
                state.height = region.height
                state.bytes_per_pixel = 1
                return "success"
            # params is None → fall through to the legacy 0x76 flow unchanged.

        # Start partial upload (0x76), stream remaining 0x71 chunks, and finish
        # with partial refresh.
        max_start = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else MAX_START_PAYLOAD
        start_pkt, remaining = build_direct_write_partial_start(
            old_etag=state.etag,
            new_etag=new_etag,
            flags=flags,
            x=region.rx,
            y=region.ry,
            width=region.rw,
            height=region.rh,
            stream_bytes=stream_bytes,
            max_start_payload=max_start,
        )
        await self._write(start_pkt)
        try:
            response = await self._read(self.TIMEOUT_ACK)
            nack = parse_nack(response)
            if nack is not None:
                opcode, err = nack
                # No partial data has been applied yet, so any pre-refresh 0x76
                # NACK (etag mismatch, rect OOB/align, flags, unsupported, ...) is
                # safe to recover from by falling back to a full upload rather than
                # raising and losing the upload entirely.
                _LOGGER.info(
                    "Partial upload rejected at START (opcode=0x%02x err=0x%02x); falling back to full upload",
                    opcode,
                    err,
                )
                state.etag = 0
                state.last_image = None
                return "fallback_full"
            validate_ack_response(response, CommandCode.DIRECT_WRITE_PARTIAL_START)
        except (BLETimeoutError, InvalidResponseError):
            _LOGGER.info("Partial upload start was not acknowledged; falling back to full upload")
            return "fallback_full"

        if await self._send_partial_chunks(remaining, stream_bytes, state, progress_callback) == "fallback_full":
            return "fallback_full"

        await self._write(build_direct_write_end_command(RefreshMode.PARTIAL.value))
        response = await self._read(self.TIMEOUT_ACK)
        validate_ack_response(response, CommandCode.DIRECT_WRITE_END)

        response = await self._read(self.TIMEOUT_REFRESH)
        command, _ = check_response_type(response)
        if command == CommandCode.DIRECT_WRITE_REFRESH_TIMEOUT:
            raise RefreshTimeoutError("Display refresh timed out (device sent 0x74)")
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
    ) -> bool:
        """Execute image upload using compressed or uncompressed protocol.

        Args:
            image_data: Raw uncompressed image data (always needed for uncompressed)
            refresh_mode: Display refresh mode
            use_compression: True to use compressed protocol
            compressed_data: Compressed data (required if use_compression=True)
            uncompressed_size: Original size (required if use_compression=True)

        Returns:
            True if ``new_etag`` was committed via END-with-etag, False if the
            firmware auto-completed the upload (no etag committed).

        Raises:
            ProtocolError: If upload fails
        """
        # 0. Sliding-window (PIPE_WRITE) attempt. Gated on the device config
        # advertising pipe support (transmission_modes bit 0x10) — a device whose
        # config lacks the bit never sees a 0x0080 probe. Also skipped when
        # disabled (max_queue_size <= 1) or when this connection already probed
        # negative. The 0x0080 negotiation below remains authoritative for the
        # transfer parameters when the gate passes.
        display_cfg = self._config.displays[0] if (self._config and self._config.displays) else None
        pipe_eligible = (
            bool(display_cfg and display_cfg.supports_pipe_write)
            and self._max_queue_size > 1
            and not (self._pipe_probed and not self._pipe_supported)
        )
        if pipe_eligible:
            total_size = len(image_data)
            params = await self._negotiate_pipe(use_compression, total_size)
            self._pipe_probed = True
            if params is not None:
                self._pipe_supported = True
                # Negotiation may have downgraded compressed→uncompressed (NACK 0x02).
                if params.compressed and compressed_data is not None:
                    payload = compressed_data
                else:
                    payload = image_data
                    if params.compressed:
                        # Contract: use_compression implies compressed_data; guard anyway.
                        params = PipeParams(params.window, params.ack_every, params.max_frame, params.selective, False)
                return await self._run_pipe_upload(payload, params, refresh_mode, progress_callback, new_etag)
            self._pipe_supported = False
            _LOGGER.info("PIPE_WRITE unavailable on %s; using legacy direct-write flow", self.mac_address)
            # Fall through to the untouched legacy flow below.

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
            # Device rejected the compressed START. Fall back to the uncompressed
            # protocol and retry; the same image_data (for 4-gray, the two split
            # planes) streams fine uncompressed. Firmware may report the failure as
            # the legacy {0xFF, 0xFF} frame or the spec-conformant {0xFF, 0x70}
            # ({0xFF, <cmd low byte>}); both are recognized as the same signal.
            if is_compressed_failure_frame(response):
                _LOGGER.warning(
                    "Device signalled compressed-write failure (0x%04x); falling back to uncompressed",
                    int.from_bytes(response[:2], "big"),
                )
            else:
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
            raise RefreshTimeoutError("Display refresh timed out (device sent 0x74)")
        if command != CommandCode.DIRECT_WRITE_REFRESH_COMPLETE:
            raise ProtocolError(f"Unexpected response waiting for refresh: {command.name} (0x{command:04x})")
        _LOGGER.info("Display refresh complete")

        # The etag is only committed on the device when we send END-with-etag;
        # a firmware auto-END (auto_completed) never sets displayed_etag.
        return not auto_completed and new_etag is not None

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

            # Send the data chunk without waiting for the ATT write confirmation
            # (Write Without Response). Flow control is preserved by the per-chunk
            # application ACK read below, so only one write is ever in flight.
            await self._write(build_direct_write_data_command(chunk_data), response=False)
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

    # ─── PIPE_WRITE (sliding-window) upload ──────────────────────────────────

    async def _negotiate_pipe(
        self, compressed: bool, total_size: int, _retry_uncompressed: bool = True
    ) -> PipeParams | None:
        """Probe + negotiate a sliding-window transfer via 0x0080.

        Sends PIPE_WRITE_START and waits ``TIMEOUT_PIPE_START`` for the response.
        Attempts are config-gated (transmission_modes bit 0x10), so this is a plain
        command timeout rather than a discovery probe. Silence (stale config bit on
        pipe-less firmware) or an unrecoverable NACK returns
        None → the caller falls back to the legacy 0x70 flow. A NACK err 0x02
        (compression unsupported) on a compressed request retries 0x0080 once
        uncompressed before giving up.

        Returns:
            PipeParams (effective, post-min-rule) on success, else None.
        """
        req_frame = DEFAULT_MAX_FRAME  # HA GATT ceiling; also our client_max_frame
        # The 0x0080 is the single pre-stream write; _write re-authenticates here
        # (once, never again mid-stream).
        await self._write(
            build_pipe_write_start_command(
                compressed, self._max_queue_size, self._blocks_per_ack, req_frame, total_size
            )
        )
        try:
            resp = await self._read(TIMEOUT_PIPE_START)
        except BLETimeoutError:
            _LOGGER.debug("No 0x0080 response within %.1fs; firmware lacks PIPE_WRITE", TIMEOUT_PIPE_START)
            return None

        try:
            ok, payload = parse_pipe_start_response(resp)
        except InvalidResponseError as err:
            _LOGGER.debug("Garbled 0x0080 response (%s); falling back to legacy", err)
            return None

        if not ok:
            err_code = cast(int, payload)
            if err_code == PIPE_START_NACK_COMPRESSION and compressed and _retry_uncompressed:
                _LOGGER.info("Device rejected compressed PIPE_WRITE (err 0x02); retrying uncompressed")
                return await self._negotiate_pipe(False, total_size, _retry_uncompressed=False)
            _LOGGER.info("PIPE_WRITE START NACK (err 0x%02x); falling back to legacy", err_code)
            return None

        ver, dev_max_window, dev_max_ack_every, dev_max_frame, flags = cast("tuple[int, int, int, int, int]", payload)
        # Min-rule (Part 1 §1.1) — computed identically to firmware.
        w_eff = max(1, min(self._max_queue_size, dev_max_window, 32))
        n_eff = max(1, min(self._blocks_per_ack, dev_max_ack_every, w_eff))
        frame_eff = min(req_frame, dev_max_frame)
        selective = bool(flags & 0x01)
        params = PipeParams(w_eff, n_eff, frame_eff, selective, compressed)
        _LOGGER.info(
            "PIPE_WRITE negotiated: W=%d N=%d frame=%d selective=%s compressed=%s (dev max %d/%d/%d, ver %d)",
            w_eff,
            n_eff,
            frame_eff,
            selective,
            compressed,
            dev_max_window,
            dev_max_ack_every,
            dev_max_frame,
            ver,
        )
        return params

    async def _negotiate_pipe_partial(
        self,
        compressed: bool,
        total_size: int,
        old_etag: int,
        region: PartialRegion,
        _retry_uncompressed: bool = True,
    ) -> PipeParams | None:
        """Probe + negotiate a partial-region sliding-window transfer via 0x0080.

        Sends an extended PIPE_WRITE_START (flags bit1 + 12-byte geometry) and
        interprets the response per Part 1 §1.2. A partial 0x0080 is a valid pipe
        probe, so this also updates ``_pipe_probed`` / ``_pipe_supported``.

        Returns:
            PipeParams(partial=True) on an ACK confirming partial (flags bit1),
            or None when the caller should fall back to the legacy 0x76 flow
            (silence / garble / other NACK / partial-flag rejected / bit1 clear).

        Raises:
            _PipePartialEtagMismatch: NACK 0x05 — caller skips 0x76, goes full.
            _PipePartialRejected: NACK 0x06/0x07 — caller skips 0x76, goes full.
        """
        req_frame = DEFAULT_MAX_FRAME  # HA GATT ceiling; also our client_max_frame
        partial = PipePartialRequest(old_etag=old_etag, x=region.rx, y=region.ry, w=region.rw, h=region.rh)
        # The 0x0080 is the single pre-stream write; _write re-authenticates here
        # (once, never again mid-stream).
        await self._write(
            build_pipe_write_start_command(
                compressed,
                self._max_queue_size,
                self._blocks_per_ack,
                req_frame,
                total_size,
                partial=partial,
            )
        )
        try:
            resp = await self._read(TIMEOUT_PIPE_START)
        except BLETimeoutError:
            _LOGGER.debug("No 0x0080 partial response within %.1fs; firmware lacks PIPE_WRITE", TIMEOUT_PIPE_START)
            self._pipe_probed = True
            self._pipe_supported = False
            return None

        try:
            ok, payload = parse_pipe_start_response(resp)
        except InvalidResponseError as err:
            _LOGGER.debug("Garbled 0x0080 partial response (%s); falling back to legacy", err)
            self._pipe_probed = True
            self._pipe_supported = False
            return None

        if not ok:
            err_code = cast(int, payload)
            # The device answered 0x0080, so pipe write itself is supported.
            self._pipe_probed = True
            self._pipe_supported = True
            if err_code == PIPE_START_NACK_COMPRESSION and compressed and _retry_uncompressed:
                _LOGGER.info("Device rejected compressed pipe-partial (err 0x02); retrying uncompressed still-partial")
                return await self._negotiate_pipe_partial(
                    False, total_size, old_etag, region, _retry_uncompressed=False
                )
            if err_code == PIPE_START_NACK_COMPRESSION:
                # A second 0x02 (or 0x02 on an already-uncompressed request) means
                # the partial flag bit itself is unknown — older pipe-capable
                # firmware. Cache the negative so we never re-send a partial 0x0080
                # this connection.
                _LOGGER.info("Device rejected the pipe-partial flag (err 0x02); disabling pipe-partial this connection")
                self._pipe_partial_supported = False
                return None
            if err_code == PIPE_START_NACK_ETAG_MISMATCH:
                raise _PipePartialEtagMismatch("Device rejected pipe-partial START: displayed etag mismatch (0x05)")
            if err_code in (PIPE_START_NACK_PARTIAL_UNSUPPORTED, PIPE_START_NACK_RECT_INVALID):
                if err_code == PIPE_START_NACK_PARTIAL_UNSUPPORTED:
                    # bpp/driver can't do partial at all — never retry this connection.
                    self._pipe_partial_supported = False
                raise _PipePartialRejected(f"Device rejected pipe-partial START (err 0x{err_code:02x})")
            _LOGGER.info("pipe-partial START NACK (err 0x%02x); falling back to legacy", err_code)
            return None

        ver, dev_max_window, dev_max_ack_every, dev_max_frame, flags = cast("tuple[int, int, int, int, int]", payload)
        # A valid ACK is a valid pipe probe.
        self._pipe_probed = True
        self._pipe_supported = True
        if not flags & PIPE_FLAG_PARTIAL:
            # Requested partial but the device did not confirm bit1 → older
            # pipe-capable firmware. Abandon the pipe attempt; the subsequent 0x76
            # START resets the orphaned firmware session (Part 1 §1.2).
            _LOGGER.info("Device ACKed 0x0080 without the partial bit; pipe-partial unsupported this connection")
            self._pipe_partial_supported = False
            return None
        self._pipe_partial_supported = True
        # Min-rule (Part 1 §1.1) — identical to _negotiate_pipe.
        w_eff = max(1, min(self._max_queue_size, dev_max_window, 32))
        n_eff = max(1, min(self._blocks_per_ack, dev_max_ack_every, w_eff))
        frame_eff = min(req_frame, dev_max_frame)
        selective = bool(flags & 0x01)
        params = PipeParams(w_eff, n_eff, frame_eff, selective, compressed, partial=True)
        _LOGGER.info(
            "pipe-partial negotiated: W=%d N=%d frame=%d selective=%s compressed=%s (dev max %d/%d/%d, ver %d)",
            w_eff,
            n_eff,
            frame_eff,
            selective,
            compressed,
            dev_max_window,
            dev_max_ack_every,
            dev_max_frame,
            ver,
        )
        return params

    def _pipe_data_size(self, frame_eff: int) -> int:
        """Chunk data capacity for a pipe frame at ``frame_eff`` bytes.

        Encrypted: frame_eff - CCM envelope (31) - seq (1) = 212 @ 244.
        Plaintext: frame_eff - PIPE_FRAME_OVERHEAD (cmd 2 + seq 1) = 241 @ 244.
        """
        if self._session_key is not None:
            return frame_eff - 31 - 1
        return frame_eff - PIPE_FRAME_OVERHEAD

    async def _run_pipe_upload(
        self,
        payload: bytes,
        params: PipeParams,
        refresh_mode: RefreshMode,
        progress_callback: Callable[[int, int], None] | None,
        new_etag: int | None,
    ) -> bool:
        """Split, stream, END, and await refresh for a sliding-window transfer.

        Returns True if ``new_etag`` was committed via END-with-etag, False if the
        firmware auto-completed the upload (no etag committed).
        """
        size = self._pipe_data_size(params.max_frame)
        if size < 1:
            raise ProtocolError(f"Negotiated pipe frame {params.max_frame} too small for a data byte")
        # Always keep at least one frame so the receiver's total check + END
        # handshake run even for an empty payload (mirrors legacy START/END).
        chunks = [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]
        if params.partial and new_etag is None:
            # An etag-less partial END would make firmware commit displayed_etag=0
            # on a successful refresh, silently desyncing the client PartialState.
            raise ProtocolError("PIPE_WRITE partial transfer requires a new_etag for the END commit")
        self._pipe_params = params
        chunk_timeout = self.TIMEOUT_PIPE_DATA_COMPRESSED if params.compressed else self.TIMEOUT_PIPE_DATA_UNCOMPRESSED

        try:
            auto_completed = await self._send_pipe_chunks(chunks, params, chunk_timeout, progress_callback)
            # Uncompressed full-frame transfers ALWAYS auto-complete (firmware
            # resets pipe state and sends an unsolicited END_ACK once total_size is
            # reached), so the explicit END path below is skipped there. Compressed
            # and partial transfers use the explicit END (partial firmware never
            # auto-completes — Part 1 §1.5). Sending an END after auto-complete
            # would be NACKed and desync etag accounting.
            if params.partial and auto_completed:
                # Partial firmware must never auto-complete; an unsolicited END_ACK
                # here is a contract violation (would have refreshed FULL, not
                # REFRESH_PARTIAL, with no committed etag).
                raise ProtocolError("PIPE_WRITE partial transfer auto-completed unexpectedly (unsolicited END_ACK)")
            if not auto_completed:
                await self._await_pipe_end_ack(chunks, refresh_mode, new_etag, params)

            # Shared refresh wait — identical to the legacy _execute_upload tail for
            # both the auto-complete and explicit-END paths.
            _LOGGER.debug("Display refresh started, waiting for completion...")
            response = await self._read(self.TIMEOUT_REFRESH)
            command, _ = check_response_type(response)
            if command == CommandCode.DIRECT_WRITE_REFRESH_TIMEOUT:
                raise RefreshTimeoutError("Display refresh timed out (device sent 0x74)")
            if command != CommandCode.DIRECT_WRITE_REFRESH_COMPLETE:
                raise ProtocolError(f"Unexpected response waiting for refresh: {command.name} (0x{command:04x})")
            _LOGGER.info("Display refresh complete (pipe)")
        finally:
            self._pipe_params = None

        return not auto_completed and new_etag is not None

    async def _send_pipe_chunks(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        chunks: list[bytes],
        params: PipeParams,
        chunk_timeout: float,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """QUIC-style selective-repeat sender for PIPE_WRITE data frames.

        Keeps up to ``params.window`` frames in flight (span-based tokens), refunds
        tokens on ACK, retransmits only missing chunks (selective repeat) — or
        rewinds when the receiver does not buffer out-of-order (bit0 clear).

        Completion contract is set by ``explicit_end = params.compressed or
        params.partial``:
        - explicit-END path (compressed OR partial): returns once every chunk is
          acked; the caller sends the explicit 0x0082 END. Compressed firmware can
          only verify totals at zlib flush; partial firmware never auto-completes
          (Part 1 §1.5) because only the 0x0082 carries the refresh mode + new_etag.
          A tail of < N_eff unacked frames never earns a cadence ACK, so the tail
          wait uses TIMEOUT_PIPE_TAIL_FLUSH and a dup-probe instead of stalling
          on chunk_timeout.
        - auto-complete path (uncompressed full-frame only): the client sends
          exactly total_size bytes, so firmware ALWAYS auto-completes — flush-ACK,
          then an unsolicited {0x00,0x82}, resetting its pipe state before any
          explicit END could arrive. This sender therefore keeps reading past the
          flush-ACK until that END_ACK and returns True (mirroring legacy
          _send_data_chunks' 0x72-in-place-of-0x71 handling); the caller must NOT
          send END.

        Returns:
            True if the device auto-completed (unsolicited END_ACK — only the
            uncompressed full-frame path), False on normal explicit-END completion
            (compressed or partial; caller sends END).

        Raises:
            ProtocolError: On a fatal NACK, MAX_RETX/MAX_PTO exhaustion, a missing
                auto-complete END_ACK, or an unexpected frame.
        """
        n = len(chunks)
        window = params.window
        # Partial transfers never auto-complete (Part 1 §1.5), so they use the
        # same explicit-END completion contract compressed transfers use today.
        explicit_end = params.compressed or params.partial
        max_retx = 3 * window
        acked: set[int] = set()
        window_base = 0  # lowest unacked
        next_to_send = 0
        pending_retx: dict[int, int] = {}  # missing idx → ACKs seen since last (re)transmit
        retx_count = 0
        pto_count = 0
        stall_acks = 0  # consecutive ACKs that neither advance window_base nor expose a hole
        bytes_total = sum(len(c) for c in chunks)
        bytes_sent_hw = 0

        async def _send(idx: int) -> None:
            nonlocal bytes_sent_hw
            await self._write_pipe_frame(build_pipe_write_data_command(idx % 256, chunks[idx]), response=False)

        while True:
            # 1. Send new chunks while span-tokens are available.
            while next_to_send < n and (next_to_send - window_base) < window:
                await _send(next_to_send)
                bytes_sent_hw += len(chunks[next_to_send])
                if progress_callback is not None:
                    progress_callback(min(bytes_sent_hw, bytes_total), bytes_total)
                next_to_send += 1

            # Explicit-END (compressed or partial): all acked → done, caller sends
            # the explicit END. Uncompressed full-frame: keep reading for the
            # unsolicited auto-complete END_ACK.
            if window_base >= n and explicit_end:
                break

            # 2. Block for an ACK (credit exhausted or tail pending). Compressed
            # tail (< N_eff unacked frames, no holes) will never earn a cadence
            # ACK, so wait only briefly before dup-probing — never send END with
            # unacked chunks (a genuinely lost tail chunk must be repaired by
            # retransmit, not surface as a fatal END NACK at zlib flush).
            tail_flush = (
                explicit_end
                and next_to_send >= n
                and 0 < n - window_base < params.ack_every
                and not (acked and max(acked) >= window_base)  # no known holes
            )
            read_timeout = self.TIMEOUT_PIPE_TAIL_FLUSH if tail_flush else chunk_timeout
            try:
                resp = await self._read(read_timeout)
            except BLETimeoutError:
                if window_base >= n:
                    # Uncompressed with everything acked: firmware owes the
                    # unsolicited END_ACK; there is nothing left to probe.
                    raise ProtocolError("PIPE_WRITE aborted: auto-complete END_ACK never arrived") from None
                pto_count += 1
                if pto_count >= MAX_PTO:
                    raise ProtocolError(f"PIPE_WRITE aborted: no ACK progress after {MAX_PTO} PTO probes") from None
                # PTO / tail-flush dup-probe: resend the oldest unacked chunk
                # (fresh nonce); a duplicate elicits an immediate ACK.
                await _send(window_base)
                retx_count += 1
                if retx_count > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: MAX_RETX ({max_retx}) exceeded (PTO)") from None
                continue

            kind = classify_pipe_frame(resp)
            if kind == PIPE_FRAME_NACK:
                err, _hs, _mask = parse_pipe_data_nack(resp)
                raise ProtocolError(f"PIPE_WRITE data NACK err=0x{err:02x} (fatal)")
            if kind == PIPE_FRAME_END_ACK:
                # Unsolicited auto-complete: the receiver confirms it holds the full
                # image (accepted total reached total_size), so it is authoritative
                # and terminal — even if a final local ACK was lost. Mirrors legacy
                # 0x72 auto-finish; the client sends no explicit END.
                if window_base < n:
                    _LOGGER.debug("PIPE auto-complete END_ACK with %d/%d chunks locally acked", len(acked), n)
                return True
            if kind != PIPE_FRAME_ACK:
                raise ProtocolError(f"Unexpected pipe frame during send: {resp[:8].hex()}")

            # 3. Process the ACK — refund tokens over the contiguous acked prefix.
            pto_count = 0
            highest_seen, ack_mask = parse_pipe_data_ack(resp)
            acked |= unpack_ack_ranges(highest_seen, ack_mask, window_base)
            prev_base = window_base
            while window_base in acked:
                pending_retx.pop(window_base, None)
                window_base += 1
            # Progress bound: an ACK stream that never advances the cumulative
            # point and never exposes a hole would otherwise reset pto_count each
            # pass and loop forever (only reachable with pathological firmware —
            # real firmware ACKs in response to frames, not unsolicited).
            if window_base > prev_base:
                stall_acks = 0
            if window_base >= n:
                # Loop top: explicit-end (compressed or partial) breaks (caller
                # sends END); uncompressed full-frame keeps reading for the
                # unsolicited auto-complete END_ACK.
                stall_acks += 1
                if stall_acks > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: {stall_acks} ACKs without auto-complete END_ACK")
                continue

            # 4. Loss handling — holes below the highest received are definite losses.
            highest_recv = max(acked) if acked else window_base - 1
            missing = [i for i in range(window_base, min(highest_recv, next_to_send)) if i not in acked]
            if not missing:
                stall_acks += 1
                if stall_acks > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: {stall_acks} consecutive ACKs without progress")
                continue

            if params.selective:
                for m in missing:  # oldest first
                    if m not in pending_retx:
                        do_retx = True  # newly detected
                    else:
                        pending_retx[m] += 1  # a new ACK still shows it missing
                        do_retx = pending_retx[m] >= 1  # one implicit RTT of spacing
                    if do_retx:
                        await _send(m)
                        pending_retx[m] = 0
                        retx_count += 1
                        if retx_count > max_retx:
                            raise ProtocolError(f"PIPE_WRITE aborted: MAX_RETX ({max_retx}) exceeded")
            else:
                # bit0 clear: rewind-style recovery (resend from window_base).
                next_to_send = window_base
                pending_retx.clear()
                retx_count += 1
                if retx_count > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: MAX_RETX ({max_retx}) exceeded (rewind)")

        return False

    async def _await_pipe_end_ack(
        self,
        chunks: list[bytes],
        refresh_mode: RefreshMode,
        new_etag: int | None,
        params: PipeParams,
    ) -> None:
        """Send 0x0082 END and wait for the END_ACK (explicit-END transfers).

        Reached by compressed transfers and ALL partial transfers (partial never
        auto-completes — the END alone carries the refresh selector + new_etag).
        Uncompressed full-frame transfers never reach here: firmware always
        auto-completes them (see _send_pipe_chunks), and an END sent after that
        reset would be NACKed. Called only after ``_send_pipe_chunks`` has seen
        every chunk acked, so the transfer is complete; a trailing tail-flush
        PIPE_ACK may precede the END_ACK in the queue and is skipped. An
        END_NACK or data NACK aborts (the caller's existing retry-from-scratch
        recovers).
        """
        del chunks  # completeness already guaranteed by the sender loop
        end_cmd = build_pipe_write_end_command(refresh_mode.value, new_etag)
        await self._write_pipe_frame(end_cmd, response=True)

        end_timeout = self.TIMEOUT_COMPRESSED_END_ACK if params.compressed else self.TIMEOUT_UNCOMPRESSED_END_ACK
        stray_acks = 0
        while True:
            resp = await self._read(end_timeout)
            kind = classify_pipe_frame(resp)
            if kind == PIPE_FRAME_END_ACK:
                return
            if kind == PIPE_FRAME_END_NACK:
                raise ProtocolError("PIPE_WRITE END NACK (byte-total mismatch or incomplete transfer)")
            if kind == PIPE_FRAME_NACK:
                err, _hs, _mask = parse_pipe_data_nack(resp)
                raise ProtocolError(f"PIPE_WRITE data NACK during END: err=0x{err:02x}")
            if kind == PIPE_FRAME_ACK:
                # Tail-flush ACK preceding END_ACK — ignore, keep reading. Bounded:
                # firmware sends at most one flush ACK per END; an unending ACK
                # stream would otherwise renew end_timeout forever.
                stray_acks += 1
                if stray_acks > 32:
                    raise ProtocolError(f"PIPE_WRITE aborted: {stray_acks} ACK frames while awaiting END_ACK")
                continue
            raise ProtocolError(f"Unexpected frame awaiting END_ACK: {resp[:8].hex()}")

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
                "Reconfigure the device to a supported color scheme (0–5)."
            ) from exc
