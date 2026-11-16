"""The new entry point: ``opendisplay.connect()`` returning a ``Device`` with a profile.

``Device`` wraps today's ``OpenDisplayDevice`` and delegates to it; nothing about that class
changes. On entry it reads the firmware version, the stored config and, on firmware 3.0 and
up, the identity, and builds a ``DeviceProfile`` from them.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from ..exceptions import IdentNotSupportedError, InvalidResponseError, NoStoredConfigError, OpenDisplayTimeoutError
from ..models.enums import RefreshMode
from ..models.profile import DeviceProfile, build_profile
from ..panels import MigrationPlan, PanelDatabase, plan_migration
from ..protocol.config_parser import parse_config_response
from .drive_write import drive_write
from .facade import OpenDisplayDevice
from .imaging import prepare_multipass

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from PIL import Image

    from ..models.config import GlobalConfig
    from ..models.ident import DeviceIdent
    from ..models.panel_drive import DisplayMode
    from ..transport.base import Transport

_LOGGER = logging.getLogger(__name__)

# Spectra and some other panels redraw for about 30 s after a config write and answer nothing
# meanwhile; the read-back retries every 10 s, 9 times. Each attempt first waits out its own
# command timeout (5 s), so it gives up after about 2.5 minutes at most.
_REDRAW_WAIT_S = 10.0
_REDRAW_RETRIES = 9

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
        and answers nothing meanwhile, so the read-back is retried: every 10 s, 9 times, each attempt
        after its own 5 s timeout, about 2.5 minutes at most.

        Raises:
            OpenDisplayTimeoutError: The device stayed silent after the write. The write itself
                was acknowledged; only the read-back is missing.
        """
        await self._device.legacy.write_config(config)
        for attempt in range(_REDRAW_RETRIES + 1):
            try:
                await self._device.refresh()
                return
            except OpenDisplayTimeoutError:
                if attempt == _REDRAW_RETRIES:
                    raise
                _LOGGER.info("Device silent after the config write (redrawing?); retrying in %.0f s", _REDRAW_WAIT_S)
                await asyncio.sleep(_REDRAW_WAIT_S)


_RESERVED_MODES = {
    "fast": "'fast' is reached with refresh_mode=RefreshMode.FAST",
    "partial": "'partial' is reached with a PartialState (partial updates)",
}


class DisplayView:  # pylint: disable=too-few-public-methods  # grows with pieces 3-5
    """Drawing on the device's display."""

    def __init__(self, device: Device) -> None:
        self._device = device

    def _mode(self, name: str) -> DisplayMode:
        """The config's drive mode called ``name`` (case-insensitive); no I/O."""
        key = name.lower()
        if key in _RESERVED_MODES:
            raise ValueError(f"{_RESERVED_MODES[key]}; mode= names a drive mode such as 'colour16'")
        profile = self._device.profile
        known = {m.name.lower(): m for m in profile.drive_modes}
        if key not in known:
            names = ", ".join(profile.drive_mode_names) or "none"
            raise ValueError(f"Unknown drive mode '{name}'; the device has: {names}")
        config = self._device.config.cached
        assert config is not None  # drive_modes come from the cached config
        index = known[key].mode_index
        return next(m for m in config.display_modes if m.mode_index == index)

    async def show(self, image: Image.Image, *, mode: str | None = None, **options: Any) -> None:
        """Draw ``image``. Without ``mode`` this is ``OpenDisplayDevice.upload_image`` with ``options``.

        With ``mode`` the image is encoded for that config-declared drive mode (every pass of a
        multi-pass mode) and sent with CMD_DRIVE_WRITE_START. Accepted options then:
        ``refresh_mode``, ``compress``, ``progress_callback``, ``dither_mode``, ``serpentine``,
        ``exposure``, ``saturation``, ``fit``, ``rotate``, ``use_measured_palettes``.

        Raises:
            ValueError: ``mode`` is "fast" or "partial" (use the refresh mode or a PartialState),
                or a name the device's config does not declare. Raised before any I/O.
            DriveModeSubstitutedError: The device runs another mode; nothing was uploaded.
        """
        legacy = self._device.legacy
        if mode is None:
            await legacy.upload_image(image, **options)
            return
        drive_mode = self._mode(mode)
        config = self._device.config.cached
        assert config is not None
        refresh = options.pop("refresh_mode", RefreshMode.FULL)
        compress = options.pop("compress", True)
        progress = options.pop("progress_callback", None)
        frames, _ = prepare_multipass(image, config, drive_mode, **options)
        # Device wraps OpenDisplayDevice until piece 7 inverts the two; these are its internals.
        async with legacy._transaction():  # pylint: disable=protected-access
            await drive_write(
                legacy._uploader,  # pylint: disable=protected-access
                legacy._channel,  # pylint: disable=protected-access
                drive_mode,
                frames,
                refresh=refresh,
                compress=compress,
                progress_callback=progress,
            )


class Device:
    """A connected OpenDisplay device. Use as ``async with opendisplay.connect(...) as dev``.

    Attributes:
        config: The stored config.
        legacy: The wrapped ``OpenDisplayDevice``, for everything not yet on the new surface.
            Its config is the one read on entry. When that config has no display, or its
            display has a colour scheme the legacy API does not know, ``legacy`` has no
            capabilities:
            ``legacy.width`` and ``legacy.upload_image()`` raise "capabilities unknown".
            Use ``profile`` for what the device is. After a ``refresh()`` that finds the
            device changed, ``legacy`` can still hold the previous config (when the device now
            has none) or the previous capabilities; ``config.cached`` and ``profile`` are current.
    """

    def __init__(self, legacy: OpenDisplayDevice) -> None:
        self.legacy = legacy
        self.config = ConfigView(self)
        self.display = DisplayView(self)
        self._profile: DeviceProfile | None = None

    @property
    def profile(self) -> DeviceProfile:
        """What the device is, as read on entry or by the last ``refresh()``."""
        if self._profile is None:
            raise RuntimeError("Device is not connected; use it as `async with opendisplay.connect(...)`")
        return self._profile

    async def refresh(self) -> DeviceProfile:
        """Re-read the firmware version, config and identity, and rebuild the profile.

        The three reads are separate commands, each holding the device's command lock, not one
        transaction. Two concurrent refreshes interleave harmlessly; the last to finish wins.
        A malformed identity reply is logged and the profile is built without the identity
        (``ident_missing=True``), as ``opendisplay info`` does; link errors propagate.
        """
        firmware = await self.legacy.read_firmware_version()
        try:
            config: GlobalConfig | None = await self.config.read()
        except NoStoredConfigError:
            config = None
        ident: DeviceIdent | None = None
        ident_missing = False
        if firmware["major"] >= _IDENT_MIN_MAJOR:
            try:
                ident = await self.legacy.read_ident()
            except IdentNotSupportedError:
                ident_missing = True
            except InvalidResponseError as err:
                _LOGGER.warning("Ignoring a malformed identity reply from %s: %s", self.legacy.mac_address, err)
                ident_missing = True
        self._profile = build_profile(firmware, config, ident, ident_missing=ident_missing)
        return self._profile

    async def __aenter__(self) -> Device:
        await self.legacy.__aenter__()
        try:
            await self.refresh()
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
        encryption_key: 16-byte device key for an encrypted device.
        timeout: Link operation timeout in seconds.

    Raises:
        ValueError: If not exactly one addressing mode is given, as for ``OpenDisplayDevice``.
    """
    return Device(
        OpenDisplayDevice(
            mac_address=address,
            device_name=name,
            ble_device=ble_device,
            transport=transport,
            host=host,
            port=port,
            tls=tls,
            psk=psk,
            encryption_key=encryption_key,
            timeout=timeout,
            auto_interrogate=False,
        )
    )
