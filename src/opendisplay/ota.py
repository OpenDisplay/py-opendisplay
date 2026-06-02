"""OTA firmware update utilities for OpenDisplay devices."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from .exceptions import OTAError

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice


async def perform_nrf_dfu(
    zip_bytes: bytes,
    dfu_ble_device: BLEDevice,
    on_progress: Callable[[float], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    *,
    fast: bool = False,
) -> None:
    """Flash an nRF device that is already in Nordic Legacy DFU mode.

    The device must already be advertising the Legacy DFU GATT service
    (UUID 00001530-1212-efde-1523-785feabcd123). Call
    ``OpenDisplayDevice.trigger_dfu_bootloader()`` first, then use
    ``find_nrf_dfu_device()`` to obtain the DFU-mode BLE device.

    Args:
        zip_bytes: Raw .zip firmware archive bytes.
        dfu_ble_device: BLE device already in DFU mode.
        on_progress: Optional callback with float percentage 0–100.
        on_log: Optional callback for human-readable status messages.
        fast: Stream firmware packets unpaced for a much faster transfer. Only
            safe on a direct connection — over an ESPHome Bluetooth proxy the
            unpaced write-without-response burst is silently dropped and the DFU
            stalls. Leave ``False`` (paced) when flashing through a proxy, which
            is always the case on HA OS. Defaults to ``False``.

    Raises:
        OTAError: DFU transfer failed or DFU service not present.
    """
    try:
        from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
        from nrf_ota._const import DEFAULT_PRN, LEGACY_DFU_SERVICE_UUID, TYPE_APPLICATION
        from nrf_ota._zip import _parse_zip_bytes
        from nrf_ota.dfu import LegacyDFU
    except ImportError as exc:
        raise OTAError("nrf-ota is required for nRF firmware updates; install it with: pip install nrf-ota") from exc

    log = on_log or (lambda _: None)
    zip_info = _parse_zip_bytes(zip_bytes)

    # Connect through bleak-retry-connector so the DFU transfer works over an
    # ESPHome Bluetooth proxy too — a plain BleakClient only connects via a local
    # adapter, which is why this previously failed on HA OS but worked on a dev
    # Mac. use_services_cache=False forces a fresh GATT discovery: the DFU
    # bootloader exposes a different service table than the application.
    try:
        client = await establish_connection(
            BleakClientWithServiceCache,
            dfu_ble_device,
            dfu_ble_device.name or "nRF DFU",
            use_services_cache=False,
            max_attempts=4,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as OTAError
        raise OTAError(f"Could not connect to nRF DFU bootloader: {exc}") from exc

    try:
        svc_uuids = [str(s.uuid).lower() for s in client.services]
        log(f"DFU device services: {svc_uuids}")

        if not any(s == LEGACY_DFU_SERVICE_UUID.lower() for s in svc_uuids):
            raise OTAError(f"Device is not in Nordic Legacy DFU mode. Services found: {svc_uuids}")

        dfu = LegacyDFU(client, on_progress=on_progress, on_log=log)
        try:
            major, minor = await dfu.read_version()
            log(f"DFU bootloader version: {major}.{minor}")
        except Exception:  # noqa: BLE001
            log("Warning: could not read DFU version")

        # Over a Bluetooth proxy the DFU Packet characteristic (write-without-
        # response) is dropped if it overruns the proxy's buffer, and Legacy DFU
        # can't retransmit, so a single drop fails the whole 11.7k-packet transfer.
        # 0.01/0.02s both dropped intermittently; 0.05s gives the proxy far more
        # drain time per packet (~8-10min) to test whether the drops are
        # pacing-sensitive buffer overruns. A direct connection has real flow
        # control, so fast mode sends unpaced.
        inter_packet_delay = 0.0 if fast else 0.05
        await dfu.start()
        await dfu.start_dfu(len(zip_info.firmware), TYPE_APPLICATION)
        await dfu.init_dfu(zip_info.init_packet)
        await dfu.send_firmware(
            zip_info.firmware,
            packets_per_notification=DEFAULT_PRN,
            inter_packet_delay=inter_packet_delay,
        )
        await dfu.activate_and_reset()
        log("DFU complete — device is rebooting with new firmware.")

    except OTAError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as OTAError
        raise OTAError(f"nRF DFU failed: {exc}") from exc
    finally:
        # The device reboots out of DFU on activate/disconnect anyway; disconnect
        # explicitly so we don't leak the connection if the transfer raised.
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


async def perform_silabs_ota(
    gbl_bytes: bytes,
    ble_device: BLEDevice,
    on_progress: Callable[[float], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    *,
    fast: bool = False,
) -> None:
    """Flash an EFR32 device in the Silicon Labs AppLoader (delegates to silabs-ble-ota).

    Thin wrapper over the standalone :mod:`silabs_ble_ota` library (an optional
    dependency, installed via the ``silabs-ota`` extra), mirroring how
    :func:`perform_nrf_dfu` wraps ``nrf-ota``.

    Call ``OpenDisplayDevice.trigger_dfu_bootloader()`` first, then pass the
    AppLoader's ``BLEDevice`` (same address as app mode). Over an ESPHome
    Bluetooth proxy, clear the proxy's stale per-MAC GATT cache first via
    ``OpenDisplayDevice.clear_gatt_cache()`` so this connection re-discovers the
    AppLoader's OTA service instead of the cached app-firmware table.

    Args:
        gbl_bytes: Raw .gbl firmware file bytes.
        ble_device: BLE device in (or booting into) the AppLoader.
        on_progress: Optional callback with float percentage 0–100.
        on_log: Optional callback for human-readable status messages.
        fast: Use the faster write-without-response transfer. Only safe on a
            direct connection (no Bluetooth proxy). Leave ``False`` when flashing
            through an ESPHome Bluetooth proxy. Defaults to ``False``.

    Raises:
        OTAError: silabs-ble-ota is not installed, or the OTA failed.
    """
    try:
        from silabs_ble_ota import SilabsOTAError
        from silabs_ble_ota import perform_silabs_ota as _perform_silabs_ota
    except ImportError as exc:
        raise OTAError(
            "silabs-ble-ota is required for Silabs firmware updates; install it with: pip install silabs-ble-ota"
        ) from exc

    try:
        await _perform_silabs_ota(gbl_bytes, ble_device, on_progress, on_log, fast=fast)
    except SilabsOTAError as exc:
        raise OTAError(str(exc)) from exc


async def find_nrf_dfu_device(original_address: str) -> BLEDevice | None:
    """Poll the BLE scanner for an nRF DFU-mode device.

    Call this after ``OpenDisplayDevice.trigger_dfu_bootloader()`` disconnects.
    Checks MAC+1 first (Nordic DFU bootloaders commonly increment the last
    byte of the address). Falls back to the original address after 10 s in
    case this particular bootloader keeps the same address.

    Works in both plain bleak environments and HA's cached scanner — in HA,
    BleakScanner.discover() returns the passive-scan cache, so repeated calls
    reflect newly-discovered advertisements.

    Args:
        original_address: BLE MAC address of the device in app mode.

    Returns:
        BLEDevice in DFU mode, or None if not found within 30 s.
    """
    from bleak import BleakScanner

    parts = original_address.upper().split(":")
    mac_plus1 = ":".join(parts[:-1] + [f"{(int(parts[-1], 16) + 1) & 0xFF:02X}"])

    for attempt in range(15):  # 2 s × 15 = 30 s
        await asyncio.sleep(2.0)
        # Only consider the original address after 10 s (5 attempts).
        # Before that, HA's cache still holds the stale app-mode entry for
        # the original address, so returning it immediately would cause a
        # connection timeout.
        candidates = [mac_plus1]
        if attempt >= 5:
            candidates.append(original_address.upper())

        devices = await BleakScanner.discover(timeout=0.1)
        addr_map = {dev.address.upper(): dev for dev in devices}
        for addr in candidates:
            if addr in addr_map:
                return addr_map[addr]

    return None
