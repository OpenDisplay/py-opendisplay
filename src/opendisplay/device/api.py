"""The new entry point: ``opendisplay.connect()`` returning a ``Device`` with a profile.

``Device`` wraps today's ``OpenDisplayDevice`` and delegates to it; nothing about that class
changes. On entry it reads the firmware version, the stored config and, on firmware 3.0 and
up, the identity, and builds a ``DeviceProfile`` from them.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from ..exceptions import IdentNotSupportedError, ImageEncodingError, NoStoredConfigError
from ..models.profile import DeviceProfile, build_profile
from .facade import OpenDisplayDevice

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

    from ..models.config import GlobalConfig
    from ..models.ident import DeviceIdent
    from ..transport.base import Transport

_IDENT_MIN_MAJOR = 3  # earlier firmware is silent on READ_IDENT and would cost every connect its timeout


async def _read_config(legacy: OpenDisplayDevice) -> GlobalConfig:
    """Read the stored config, also when the legacy capability step cannot describe it.

    ``interrogate()`` stores the parsed config, then derives capabilities from display 0. That
    step raises ``ImageEncodingError`` for a colour scheme the legacy enum lacks (such as
    BWRY16). The profile describes it, so the config is kept. Any other failure, or one before
    a config was stored, is re-raised. (A config without displays never gets this far: the
    parser rejects it with ``ConfigParseError``.)

    Raises:
        NoStoredConfigError: The device has no config.
    """
    try:
        return await legacy.interrogate()
    except ImageEncodingError:
        if legacy.config is None:
            raise
        return legacy.config


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

    async def write(self, config: GlobalConfig) -> None:
        """Write ``config`` to the device, then refresh the profile from the device."""
        await self._device.legacy.write_config(config)
        await self._device.refresh()


class Device:
    """A connected OpenDisplay device. Use as ``async with opendisplay.connect(...) as dev``.

    Attributes:
        config: The stored config.
        legacy: The wrapped ``OpenDisplayDevice``, for everything not yet on the new surface.
            Its config is the one read on entry. When that config has a colour scheme the
            legacy API does not know (such as BWRY16), ``legacy`` has no capabilities:
            ``legacy.width`` and ``legacy.upload_image()`` raise "capabilities unknown".
            Use ``profile`` for what the device is.
    """

    def __init__(self, legacy: OpenDisplayDevice) -> None:
        self.legacy = legacy
        self.config = ConfigView(self)
        self._profile: DeviceProfile | None = None

    @property
    def profile(self) -> DeviceProfile:
        """What the device is, as read on entry or by the last ``refresh()``."""
        if self._profile is None:
            raise RuntimeError("Device is not connected; use it as `async with opendisplay.connect(...)`")
        return self._profile

    async def refresh(self) -> DeviceProfile:
        """Re-read the firmware version, config and identity, and rebuild the profile."""
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
