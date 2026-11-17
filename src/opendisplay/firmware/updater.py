"""``update()``: a firmware 3.x update over Bluetooth, the same six steps whatever the method.

1. Refuse what the device cannot take: claim mode, firmware 2.x (the first move to 3.0 is the
   setup flow), a target without a Bluetooth update, an image for another target.
2. Enable the update by security state (SECURITY_V2 § 6.1): a claimed device needs a sealed
   ENTER_DFU (ESP32 then unlocks SMP until it reboots; Nordic for the next link); an unclaimed one
   needs nothing, except a BG22, whose ENTER_DFU is what reboots it into the AppLoader.
3. Transfer: mcumgr SMP (ESP32, nRF52840, nRF54) or the Silicon Labs AppLoader (BG22).
4. Wait for the reboot and reconnect. The reset is noted as a redraw (``opendisplay.redraw``), so
   the reconnect waits by itself: the E1002 was not seen for 1.5 to 2 minutes after its reset.
5. Send READ_IDENT. On ESP32 and nRF the first command is what confirms the new image.
6. Decide the outcome. SMP devices are judged by the running slot's hash, read over SMP after the
   confirm: an ESP32 release reports 3.0.0 in CMD_FIRMWARE_VERSION whatever it is
   (Firmware_Unified#103), and SMP reports the image's own version.

SMP and the device's own link are separate connections, one after the other: a central holds one
link to a peripheral. The device is left connected when ``update()`` returns, where it can be.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

from .. import redraw
from ..exceptions import BLEConnectionError, OpenDisplayError, OTAError
from ..models.enums import ICType
from ..models.ident import FirmwareTarget
from ..ota import perform_silabs_ota
from ..sec2.wire import SecState
from . import smp

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

    from ..device.api import Device
    from ..models.firmware import FirmwareVersion

_LOGGER = logging.getLogger(__name__)

REBOOT_ALLOWANCE_S: Final = 180.0  # reboot + a Spectra redraw: not seen for 1.5-2 min on the E1002
_SCAN_TIMEOUT_S: Final = 30.0
_SCAN_ATTEMPTS: Final = 3  # macOS sometimes misses a display that has just been disconnected
_APPLOADER_NAME: Final = "OTA"

SMP: Final = "smp"
APPLOADER: Final = "silabs-apploader"
_METHODS: Final[dict[int, str]] = {
    FirmwareTarget.ESP32: SMP,
    FirmwareTarget.NRF52840: SMP,
    FirmwareTarget.NRF54: SMP,
    FirmwareTarget.EFR32BG22: APPLOADER,
}

# esp_image_header_t: magic 0xE9 at 0, chip_id (uint16 LE) at 12.
_ESP_MAGIC: Final = 0xE9
_ESP_CHIP_NAMES: Final[dict[int, str]] = {
    0: "ESP32",
    2: "ESP32-S2",
    5: "ESP32-C3",
    9: "ESP32-S3",
    12: "ESP32-C2",
    13: "ESP32-C6",
    16: "ESP32-H2",
}
_ESP_CHIP_BY_IC: Final[dict[int, int]] = {ICType.ESP32_S3: 9, ICType.ESP32_C3: 5, ICType.ESP32_C6: 13}
_MCUBOOT_MAGIC: Final = bytes.fromhex("3db8f396")  # IMAGE_MAGIC 0x96f3b83d, little-endian
_GBL_MAGIC: Final = bytes.fromhex("eb17a603")  # GBL header tag 0x03A617EB, little-endian

FindDevice = Callable[[str], Awaitable["BLEDevice | None"]]


class UpdateOutcome(Enum):
    """What an update left the device running."""

    UPDATED = "updated"
    ROLLED_BACK = "rolled_back"  # it came back on its old image
    WAITING_IN_BOOTLOADER = "waiting_in_bootloader"  # BG22: the AppLoader waits for a .gbl
    NOT_UPDATED = "not_updated"


@dataclass(frozen=True)
class UpdateResult:
    """The outcome, the versions before and after (``None`` when unknown), and what to do next."""

    outcome: UpdateOutcome
    version_before: str
    version_after: str | None
    detail: str


class UpdateRefusedError(OpenDisplayError):
    """The device cannot take this update; nothing was sent."""


def _version(firmware: FirmwareVersion) -> str:
    return f"{firmware['major']}.{firmware['minor']}.{firmware['patch']}"


def _target(device: Device) -> FirmwareTarget | int | None:
    return device.profile.target


def _check_image(target: FirmwareTarget | int, image: bytes, ic_type: int | None) -> None:
    if target == FirmwareTarget.ESP32:
        if image[:1] != bytes([_ESP_MAGIC]) or len(image) < 14:
            raise UpdateRefusedError("This is not an ESP32 image: an ESP32 takes the board's -app.bin")
        chip = int.from_bytes(image[12:14], "little")
        expected = _ESP_CHIP_BY_IC.get(ic_type) if ic_type is not None else None
        if expected is not None and chip != expected:
            raise UpdateRefusedError(
                f"This image is for the {_ESP_CHIP_NAMES.get(chip, f'chip {chip}')}; "
                f"the display is an {_ESP_CHIP_NAMES[expected]}"
            )
    elif target in (FirmwareTarget.NRF52840, FirmwareTarget.NRF54):
        if image[:4] != _MCUBOOT_MAGIC:
            raise UpdateRefusedError("This is not an MCUboot image: nRF52840 and nRF54 take the board's -ota.bin")
    elif target == FirmwareTarget.EFR32BG22 and image[:4] != _GBL_MAGIC:
        raise UpdateRefusedError("This is not a .gbl image: a BG22 takes opendisplay-bg22-ota.gbl")


def _check(device: Device, image: bytes) -> FirmwareTarget | int:
    """Step 1. Returns the target; raises :class:`UpdateRefusedError` with the reason."""
    if device.profile.firmware["major"] < 3:
        raise UpdateRefusedError("The display runs firmware 2.x: the first move to 3.0 is the setup flow, not update()")
    target = _target(device)
    if target not in _METHODS:
        raise UpdateRefusedError(f"Updating this target ({target!r}) over Bluetooth is not supported yet")
    assert target is not None  # in _METHODS
    config = device.config.cached
    _check_image(target, image, config.system.ic_type if config is not None else None)
    _refuse_claim_mode(device)
    return target


def _refuse_claim_mode(device: Device) -> None:
    status = device.security.status
    if status is not None and status.claim_mode:
        raise UpdateRefusedError(
            "The display is in claim mode, which refuses updates: claim it, or leave claim mode "
            "with dev.security.use_without_key(), then update"
        )


async def enable_ota(device: Device) -> None:
    """Step 2: unlock the update where the firmware needs it (SECURITY_V2 § 6.1).

    A claimed device gets ENTER_DFU on its open, sealed session; a BG22 gets it in any state,
    since it is what reboots it into the AppLoader. An unclaimed ESP32 or nRF needs nothing.

    Raises:
        UpdateRefusedError: The display is in claim mode.
    """
    _refuse_claim_mode(device)
    status = device.security.status
    claimed = status is not None and status.state == SecState.CLAIMED
    if claimed or _METHODS.get(_target(device) or -1) == APPLOADER:
        await device.legacy.trigger_dfu_bootloader()


async def _scan(address: str) -> BLEDevice | None:
    from bleak import BleakScanner  # pylint: disable=import-outside-toplevel

    return await BleakScanner.find_device_by_address(address, timeout=_SCAN_TIMEOUT_S)


async def _find(find: FindDevice, address: str) -> BLEDevice:
    for _ in range(_SCAN_ATTEMPTS):
        found = await find(address)
        if found is not None:
            return found
    raise BLEConnectionError(f"The display {address} was not found")


def _note_reboot(device: Device) -> None:
    # Device wraps OpenDisplayDevice until piece 7 inverts the two; these are its internals.
    keys = device.legacy._redraw_keys()  # pylint: disable=protected-access
    redraw.mark_redrawing(*keys, allowance=REBOOT_ALLOWANCE_S)


async def _connect(device: Device) -> None:
    """Reconnect the caller's ``Device``: ``update()`` closes and reopens the link it was handed."""
    await device.__aenter__()  # pylint: disable=unnecessary-dunder-call


async def _reconnect_quietly(device: Device) -> None:
    try:
        await _connect(device)
    except OpenDisplayError as err:
        _LOGGER.warning("Could not reconnect to %s after a failed update: %s", device.legacy.mac_address, err)


def _running(states: list[Any]) -> Any:
    return next((s for s in states if s.active), states[0])


async def _update_smp(
    device: Device, image: bytes, find: FindDevice, on_progress: Callable[[int, int], None] | None
) -> UpdateResult:
    address = device.legacy.mac_address
    failed: smp.SmpTransferError | None = None
    async with smp.session(await _find(find, address)) as client:
        before = _running(await smp.image_states(client))
        try:
            await smp.upload(client, image, on_progress=on_progress)
        except smp.SmpTransferError as err:
            failed = err
        else:
            new_hash = await smp.test_and_reset(client)
    old = str(before.version)
    if failed is not None:
        _LOGGER.debug("SMP upload to %s failed", address, exc_info=failed)
        await _reconnect_quietly(device)
        reason = str(failed).rstrip(".")
        return UpdateResult(UpdateOutcome.NOT_UPDATED, old, old, f"{reason}. The display keeps running {old}.")

    _note_reboot(device)
    try:
        await _connect(device)
    except OpenDisplayError as err:
        return UpdateResult(
            UpdateOutcome.NOT_UPDATED,
            old,
            None,
            f"The display did not come back after the reset ({err}). It may still be rebooting: the "
            f"first command it serves will confirm the new image; a reset before that returns it to {old}.",
        )
    await device.legacy.read_ident()  # step 5: confirms the new image
    await device.__aexit__(None, None, None)
    async with smp.session(await _find(find, address)) as client:
        running = _running(await smp.image_states(client))
    await _connect(device)

    after = str(running.version)
    if bytes(running.hash) == new_hash:
        return UpdateResult(UpdateOutcome.UPDATED, old, after, f"The display runs {after}.")
    return UpdateResult(
        UpdateOutcome.ROLLED_BACK,
        old,
        after,
        f"The new image did not start and the display returned to {after}. Its old image is intact.",
    )


async def _update_apploader(
    device: Device, image: bytes, find: FindDevice, on_progress: Callable[[int, int], None] | None
) -> UpdateResult:
    address = device.legacy.mac_address
    old = _version(device.profile.firmware)
    total = len(image)

    def percent(pct: float) -> None:
        if on_progress is not None:
            on_progress(int(total * pct / 100), total)

    try:
        await perform_silabs_ota(image, await _find(find, address), percent)
    except OTAError as err:
        return UpdateResult(
            UpdateOutcome.WAITING_IN_BOOTLOADER,
            old,
            None,
            f"The transfer failed ({err}). The display waits in its AppLoader (advertising as "
            f"'{_APPLOADER_NAME}'): send the .gbl again.",
        )

    _note_reboot(device)
    try:
        await _connect(device)
    except OpenDisplayError as err:
        seen = await find(address)
        if seen is not None and seen.name == _APPLOADER_NAME:
            return UpdateResult(
                UpdateOutcome.WAITING_IN_BOOTLOADER,
                old,
                None,
                f"The new image did not start: the display is back in its AppLoader (advertising as "
                f"'{_APPLOADER_NAME}'). Send a known good .gbl.",
            )
        return UpdateResult(
            UpdateOutcome.NOT_UPDATED, old, None, f"The display did not come back after the transfer ({err})."
        )
    await device.legacy.read_ident()
    after = _version(device.profile.firmware)
    return UpdateResult(UpdateOutcome.UPDATED, old, after, f"The display runs {after}.")


async def update(
    device: Device,
    image: bytes,
    *,
    method: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    find_device: FindDevice | None = None,
) -> UpdateResult:
    """Update a firmware 3.x device over Bluetooth with ``image`` (see the module docstring).

    Args:
        device: A connected ``Device``; connected again on return where it can be.
        image: The release file for this board's ``ble-ota`` flow (``ReleaseManifest``).
        method: ``"smp"`` or ``"silabs-apploader"``; by default, from the firmware target.
        on_progress: Called with (bytes sent, total).
        find_device: Finds the display's ``BLEDevice`` by address, for the transfer and after the
            reboot. Home Assistant passes its own (``async_ble_device_from_address``); the default
            scans with bleak.

    Raises:
        UpdateRefusedError: The device cannot take this update; nothing was sent.
    """
    target = _check(device, image)
    chosen = method or _METHODS[target]
    if chosen not in (SMP, APPLOADER):
        raise ValueError(f"Unknown update method {chosen!r}")
    await enable_ota(device)
    await device.__aexit__(None, None, None)
    find = find_device or _scan
    if chosen == SMP:
        return await _update_smp(device, image, find, on_progress)
    return await _update_apploader(device, image, find, on_progress)
