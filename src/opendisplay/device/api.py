"""The new entry point: ``opendisplay.connect()`` returning a ``Device`` with a profile.

``Device`` wraps today's ``OpenDisplayDevice`` and delegates to it; nothing about that class
changes. On entry it reads the firmware version, the stored config and, on firmware 3.0 and
up, the identity, and builds a ``DeviceProfile`` from them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from epaper_dithering import DitherMode

from .. import redraw
from ..credentials import DeviceKey
from ..encoding import compress_image_data
from ..encoding.compression import FIRMWARE_ZLIB_WINDOW_BITS
from ..exceptions import (
    AuthenticationRequiredError,
    IdentNotSupportedError,
    InvalidResponseError,
    NoStoredConfigError,
    OpenDisplayTimeoutError,
)
from ..firmware import updater
from ..firmware.releases import can_roll_back
from ..models.enums import FitMode, RefreshMode, Rotation
from ..models.profile import DeviceProfile, build_profile
from ..panels import MigrationPlan, PanelDatabase, plan_migration
from ..protocol.config_parser import parse_config_response
from ..sec2.keystore import KeyStore
from .drive_write import drive_write
from .facade import OpenDisplayDevice
from .imaging import prepare_multipass
from .security import SecurityView

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from PIL import Image

    from ..models.config import GlobalConfig
    from ..models.firmware import FirmwareVersion
    from ..models.ident import DeviceIdent
    from ..models.panel_drive import DisplayMode
    from ..transport.base import Transport

_LOGGER = logging.getLogger(__name__)

# Spectra and some other panels redraw for about 30 s after a config write and answer nothing
# meanwhile; the read-back retries every 10 s, 9 times. Each attempt first waits out its own
# command timeout (5 s), so it gives up after about 2.5 minutes at most.

_IDENT_MIN_MAJOR = 3  # earlier firmware is silent on READ_IDENT and would cost every connect its timeout


async def _read_config(legacy: OpenDisplayDevice) -> GlobalConfig:
    """Read the stored config leniently and hand it to ``legacy``.

    A config without a display packet parses to ``displays=[]`` with every other packet kept
    (firmware 3.0 stores a write as sent), so a repair can keep them. ``legacy`` gets the config,
    and capabilities only when display 0 can describe them: not for a config without displays,
    and not for a 0x20 colour scheme the legacy enum lacks. ``OpenDisplayDevice`` itself stays
    strict.

    Raises:
        NoStoredConfigError: The device has no config.
        ConfigParseError: The config cannot be parsed even leniently.
    """
    # Device wraps OpenDisplayDevice until piece 7 inverts the two; these are its internals.
    raw = await legacy._read_config_bytes()  # pylint: disable=protected-access
    config = parse_config_response(raw, require_display=False)
    legacy._adopt_config(config)  # pylint: disable=protected-access
    return config


class ConfigView:
    """The device's stored config: the last one read or written, and reading and writing it."""

    def __init__(self, device: Device) -> None:
        self._device = device
        self._cached: GlobalConfig | None = None

    @property
    def cached(self) -> GlobalConfig | None:
        """The last config read or written; ``None`` when the device has none."""
        return self._cached

    async def read(self) -> GlobalConfig:
        """Read the stored config from the device.

        Raises:
            NoStoredConfigError: The device has no config.
        """
        try:
            config = await _read_config(self._device.legacy)
        except NoStoredConfigError:
            self._cached = None
            raise
        self._cached = config
        return config

    def plan_migration(self, db: PanelDatabase | None = None) -> MigrationPlan:
        """What the cached config needs before a firmware 3.0 device can draw (no I/O)."""
        return plan_migration(self.cached, self._device.profile, db or PanelDatabase.load())

    async def write(self, config: GlobalConfig) -> None:
        """Write ``config`` to the device, then refresh the profile from the device.

        The device may redraw its panel after storing a config (Spectra panels take about 30 s)
        and answers nothing meanwhile. The write notes that redraw (``opendisplay.redraw``), so the
        read-back's replies are waited for, up to ``REDRAW_ALLOWANCE_S``.

        Raises:
            OpenDisplayTimeoutError: The device stayed silent after the write. The write itself
                was acknowledged; only the read-back is missing.
        """
        await self._device.legacy.write_config(config)
        await self._device.refresh()

    async def clear(self) -> None:
        """Erase the device's stored config (CONFIG_CLEAR), then refresh the profile from the device.

        A claimed firmware 3.0 device keeps its reset-pin settings (the 0x27 packet).

        Raises:
            ConfigClearError: The device refused; its config is unchanged.
        """
        await self._device.legacy.clear_config()
        self._cached = None
        await self._device.refresh()


_RESERVED_MODES = {
    "fast": "'fast' is reached with refresh_mode=RefreshMode.FAST",
    "partial": "'partial' is reached with a PartialState (partial updates)",
}


class DisplayView:
    """Drawing on the device's display."""

    def __init__(self, device: Device) -> None:
        self._device = device

    def drive_mode(self, name: str) -> DisplayMode:
        """Display 0's drive mode called ``name`` (case-insensitive), as the config declares it. No I/O.

        Raises:
            ValueError: ``name`` is "fast" or "partial" (reached through the refresh mode or a
                PartialState), or a name the device's config does not declare.
        """
        key = name.lower()
        if key in _RESERVED_MODES:
            raise ValueError(f"{_RESERVED_MODES[key]}; mode= names a drive mode such as 'colour16'")
        profile = self._device.profile
        known = {m.name.lower(): m for m in profile.drive_modes}
        if key not in known:
            names = ", ".join(profile.drive_mode_names) or "none"
            raise ValueError(f"Unknown drive mode '{name}'; the device has: {names}")
        config = self._device.config.cached
        assert config is not None and config.displays  # drive_modes come from display 0 of the cached config
        index, instance = known[key].mode_index, config.displays[0].instance_number
        return next(m for m in config.display_modes if m.mode_index == index and m.instance_number == instance)

    def nominal_colours(self, mode: DisplayMode) -> bool:
        """Whether a multi-pass mode's palette is nominal rather than measured on this unit."""
        config = self._device.config.cached
        if mode.passes <= 1 or config is None:
            return False
        return any(
            c.mode_index == mode.mode_index and c.instance_number == mode.instance_number and not c.measured
            for c in config.display_calibrations
        )

    async def show(  # pylint: disable=too-many-arguments
        self,
        image: Image.Image,
        *,
        mode: str | None = None,
        refresh_mode: RefreshMode = RefreshMode.FULL,
        dither_mode: DitherMode = DitherMode.BURKES,
        serpentine: bool = True,
        exposure: float = 1.0,
        saturation: float = 1.0,
        fit: FitMode = FitMode.CONTAIN,
        rotate: Rotation = Rotation.ROTATE_0,
        compress: bool = True,
        use_measured_palettes: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> int | None:
        """Draw ``image``, in a config-declared drive mode when ``mode`` names one.

        Without ``mode`` this is ``OpenDisplayDevice.upload_image`` with the same options
        (``use_measured_palettes`` is then the device's own setting). With ``mode`` the image is
        encoded for that mode (every pass of a multi-pass mode) off the event loop and sent with
        CMD_DRIVE_WRITE_START. Options the drive-mode path cannot honour (tone, gamut, partial
        state) are not parameters; ``OpenDisplayDevice.upload_image`` still has them.

        Returns:
            The panel temperature the device reported for a mode upload (``None`` when unknown),
            or ``None`` without ``mode``.

        Raises:
            ValueError: See ``drive_mode``; raised before any I/O.
            DriveModeSubstitutedError: The device runs another mode; nothing was uploaded.
        """
        legacy = self._device.legacy
        if mode is None:
            await legacy.upload_image(
                image,
                refresh_mode=refresh_mode,
                dither_mode=dither_mode,
                compress=compress,
                serpentine=serpentine,
                exposure=exposure,
                saturation=saturation,
                fit=fit,
                rotate=rotate,
                progress_callback=progress_callback,
            )
            return None
        drive_mode = self.drive_mode(mode)
        config = self._device.config.cached
        assert config is not None
        # Device wraps OpenDisplayDevice until piece 7 inverts the two; these are its internals.
        uploader = legacy._uploader  # pylint: disable=protected-access
        compressed = compress and uploader.supports_compression

        def encode() -> tuple[bytes, bytes | None]:
            frames, _ = prepare_multipass(
                image,
                config,
                drive_mode,
                use_measured_palettes=use_measured_palettes,
                dither_mode=dither_mode,
                serpentine=serpentine,
                exposure=exposure,
                saturation=saturation,
                fit=fit,
                rotate=rotate,
            )
            stream = compress_image_data(frames, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS) if compressed else None
            return frames, stream

        frames, stream = await asyncio.to_thread(encode)
        async with legacy._transaction():  # pylint: disable=protected-access
            return await drive_write(
                uploader,
                legacy._channel,  # pylint: disable=protected-access
                drive_mode,
                frames,
                stream=stream,
                refresh=refresh_mode,
                progress_callback=progress_callback,
            )


class FirmwareView:
    """Updating the device's firmware over Bluetooth (firmware 3.x; ``opendisplay.firmware``)."""

    def __init__(self, device: Device) -> None:
        self._device = device

    @property
    def can_roll_back(self) -> bool:
        """Whether this device returns to its old image by itself when a new one never starts.

        ESP32, nRF52840 and nRF54 keep the old image in a second slot; a BG22 has none and falls
        back into its AppLoader instead. Tell the user before an update on a device without it.
        """
        return can_roll_back(self._device.profile.target)

    async def enable_ota(self) -> None:
        """Unlock updates where the firmware needs it (a claimed device; a BG22 reboots into its AppLoader)."""
        await updater.enable_ota(self._device)

    async def update(
        self,
        image: bytes,
        *,
        method: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        find_device: updater.FindDevice | None = None,
    ) -> updater.UpdateResult:
        """Update to ``image``; see :func:`opendisplay.firmware.update`."""
        return await updater.update(
            self._device, image, method=method, on_progress=on_progress, find_device=find_device
        )


class Device:
    """A connected OpenDisplay device. Use as ``async with opendisplay.connect(...) as dev``.

    Attributes:
        config: The stored config.
        firmware: Firmware updates over Bluetooth (``update()``, ``can_roll_back``).
        legacy: The wrapped ``OpenDisplayDevice``, for everything not yet on the new surface.
            Its config is the one read on entry. When that config has no display, or its
            display has a colour scheme the legacy API does not know, ``legacy`` has no
            capabilities:
            ``legacy.width`` and ``legacy.upload_image()`` raise "capabilities unknown".
            Use ``profile`` for what the device is. After a ``refresh()`` that finds the
            device changed, ``legacy`` can still hold the previous config (when the device now
            has none) or the previous capabilities; ``config.cached`` and ``profile`` are current.
    """

    def __init__(self, legacy: OpenDisplayDevice, security: SecurityView | None = None) -> None:
        self.legacy = legacy
        self.security = security if security is not None else SecurityView(legacy)
        self.config = ConfigView(self)
        self.display = DisplayView(self)
        self.firmware = FirmwareView(self)
        self.security.config_view = self.config  # whether the key is shown on the panel
        self._profile: DeviceProfile | None = None

    @property
    def profile(self) -> DeviceProfile:
        """What the device is, as read on entry or by the last ``refresh()``."""
        if self._profile is None:
            raise RuntimeError("Device is not connected; use it as `async with opendisplay.connect(...)`")
        return self._profile

    async def refresh(self, firmware: FirmwareVersion | None = None) -> DeviceProfile:
        """Re-read the firmware version, config and identity, and rebuild the profile.

        The three reads are separate commands, each holding the device's command lock, not one
        transaction. Two concurrent refreshes interleave harmlessly; the last to finish wins.
        A malformed identity reply is logged and the profile is built without the identity
        (``ident_missing=True``), as ``opendisplay info`` does; link errors propagate.
        """
        if firmware is None:
            firmware = await self.legacy.read_firmware_version()
        config = await self._read_config()
        ident, ident_missing = await self._read_ident(firmware)
        self._profile = build_profile(firmware, config, ident, ident_missing=ident_missing)
        return self._profile

    async def _read_config(self) -> GlobalConfig | None:
        try:
            return await self.config.read()
        except NoStoredConfigError:
            return None

    async def _read_ident(self, firmware: FirmwareVersion) -> tuple[DeviceIdent | None, bool]:
        """The identity, and whether a device that should answer READ_IDENT did not."""
        if firmware["major"] < _IDENT_MIN_MAJOR:
            return None, False
        try:
            return await self.legacy.read_ident(), False
        except IdentNotSupportedError:
            return None, True
        except InvalidResponseError as err:
            _LOGGER.warning("Ignoring a malformed identity reply from %s: %s", self.legacy.mac_address, err)
            return None, True

    async def _first_read(self) -> FirmwareVersion:
        """The firmware version; a silence with no redraw noted says the display may be redrawing."""
        try:
            return await self.legacy.read_firmware_version()
        except OpenDisplayTimeoutError as err:
            if redraw.remaining(*self.legacy._redraw_keys()):  # pylint: disable=protected-access
                raise
            raise type(err)(f"{err} {redraw.REDRAW_HINT}") from err

    async def __aenter__(self) -> Device:
        await self.legacy.__aenter__()
        try:
            try:
                firmware = await self._first_read()
            except AuthenticationRequiredError:
                # Firmware_Silabs 2.x with encryption on answers even the version only in a session.
                if not await self.security.open_legacy_session():
                    raise
                firmware = await self._first_read()
            ident, ident_missing = await self._read_ident(firmware)
            await self.security.establish(firmware["major"], ident)
            config = await self._read_config()
            self._profile = build_profile(firmware, config, ident, ident_missing=ident_missing)
        except BaseException:
            with suppress(Exception):
                await self.legacy.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.legacy.__aexit__(None, None, None)


def connect(  # pylint: disable=too-many-arguments
    address: str | None = None,
    *,
    name: str | None = None,
    ble_device: BLEDevice | None = None,
    transport: Transport | None = None,
    host: str | None = None,
    port: int | None = None,
    tls: bool = False,
    psk: bytes | None = None,
    encryption_key: bytes | None = None,
    timeout: float = 10.0,
    key_store: KeyStore | None = None,
    rotate_legacy_key: bool = True,
) -> Device:
    """Describe a device to connect to. Use as ``async with opendisplay.connect(...) as dev``.

    Nothing is sent until the ``async with`` is entered. A device without a stored config
    connects; its profile says ``configured=False``.

    Args:
        address: BLE MAC address.
        name: BLE device name, resolved by a scan (instead of ``address``).
        ble_device: BLEDevice from the caller's scanner (e.g. Home Assistant).
        transport: A transport built by the caller.
        host: IP address or hostname for a LAN connection.
        port: TCP port for a LAN connection.
        tls: Use TLS-PSK on the LAN connection.
        psk: TLS pre-shared key.
        encryption_key: The device's key: a ``LegacyKey`` (firmware 2.x), a ``DeviceKey``
            (firmware 3.0, SEC2), or plain bytes, tried with whichever scheme the device speaks.
        timeout: Link operation timeout in seconds.
        key_store: Where firmware 3.0 device keys live: tried on connect (at most three), and
            where a claim or a rotation keeps its new key. See ``opendisplay.sec2.keystore``.
        rotate_legacy_key: Rotate a key that a 3.0 device migrated from 2.x to a
            device-generated one after the first session (needs ``key_store``; default True).

    Raises:
        ValueError: If not exactly one addressing mode is given, as for ``OpenDisplayDevice``.
    """
    legacy = OpenDisplayDevice(
        mac_address=address,
        device_name=name,
        ble_device=ble_device,
        transport=transport,
        host=host,
        port=port,
        tls=tls,
        psk=psk,
        # A SEC2 key never goes to the 2.x layer (its renewal and the LAN TLS-PSK are 2.x).
        encryption_key=None if isinstance(encryption_key, DeviceKey) else encryption_key,
        timeout=timeout,
        auto_interrogate=False,
        auto_authenticate=False,
    )
    security = SecurityView(legacy, credential=encryption_key, key_store=key_store, rotate_legacy_key=rotate_legacy_key)
    return Device(legacy, security)
