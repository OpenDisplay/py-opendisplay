"""OTA firmware update utilities for OpenDisplay devices."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from .exceptions import OTAError

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_SILABS_OTA_CONTROL_UUID = "f7bf3564-fb6d-4e53-88a4-5e37e0326063"
_SILABS_OTA_DATA_UUID = "984227f3-34fc-4045-a5d0-2c581f81a153"
_SILABS_OTA_CHUNK_SIZE = 244
_SILABS_APPLOADER_BOOT_DELAY = 6.0  # seconds to wait before the first connect
_SILABS_CONNECT_ATTEMPTS = 5  # establish_connection retries while AppLoader boots
# Window of data chunks sent write-without-response before forcing one
# write-with-response (whose ATT ack gates the sender).
#
# Set to 1 = EVERY chunk is write-with-response. This is required for reliability
# over a BT proxy: write-without-response (ATT Write Command) has no delivery
# guarantee — if the device's buffer is briefly full it SILENTLY DROPS the chunk
# (no ack, no error). Over a proxy (no backpressure) some chunks get dropped, the
# stream still "completes" (the periodic sync writes are acked), but the image is
# incomplete and the 0x03 finalize fails verification with "Application error"
# (observed: window=8 stalled ~20% from buffer overrun; window=2 reached 100% but
# finalize rejected the gappy image). The Silabs AppLoader has no packet-receipt
# mechanism to detect/recover drops, so the only safe option is to acknowledge
# every write. Slower, but it guarantees a complete image. (>1 is only safe on a
# direct connection, where the link layer makes Write Commands reliable.)
_SILABS_OTA_WINDOW = 1
# Adaptive flow control: a BT proxy returns "Congested" when its BLE TX buffer
# fills under the write-without-response burst (worse on a weak/busy link). It
# means "slow down", not failure — back off and resend the same chunk.
_SILABS_OTA_CONGESTION_RETRIES = 6
_SILABS_OTA_CONGESTION_BACKOFF = 0.15  # seconds, to let the proxy queue drain


async def perform_nrf_dfu(
    zip_bytes: bytes,
    dfu_ble_device: BLEDevice,
    on_progress: Callable[[float], None] | None = None,
    on_log: Callable[[str], None] | None = None,
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

    Raises:
        OTAError: DFU transfer failed or DFU service not present.
    """
    try:
        from bleak import BleakClient
        from nrf_ota._const import DEFAULT_PRN, LEGACY_DFU_SERVICE_UUID, TYPE_APPLICATION
        from nrf_ota._zip import _parse_zip_bytes
        from nrf_ota.dfu import LegacyDFU
    except ImportError as exc:
        raise OTAError("nrf-ota is required for nRF firmware updates; install it with: pip install nrf-ota") from exc

    log = on_log or (lambda _: None)
    zip_info = _parse_zip_bytes(zip_bytes)

    try:
        async with BleakClient(dfu_ble_device) as client:
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

            await dfu.start()
            await dfu.start_dfu(len(zip_info.firmware), TYPE_APPLICATION)
            await dfu.init_dfu(zip_info.init_packet)
            await dfu.send_firmware(zip_info.firmware, packets_per_notification=DEFAULT_PRN)
            await dfu.activate_and_reset()
            log("DFU complete — device is rebooting with new firmware.")

    except OTAError:
        raise
    except Exception as exc:
        raise OTAError(f"nRF DFU failed: {exc}") from exc


async def perform_silabs_ota(
    gbl_bytes: bytes,
    ble_device: BLEDevice,
    on_progress: Callable[[float], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> None:
    """Flash an EFR32BG22 device using Silicon Labs AppLoader OTA.

    Call ``OpenDisplayDevice.trigger_dfu_bootloader()`` first. This function
    retries the *connection* while the AppLoader boots — no external sleep is
    required before calling.

    The AppLoader exits back to the application when the BLE connection
    drops, so the full transfer must complete in a single connection. We
    therefore only retry failed connects (which do not arm the reboot) and
    never reconnect once connected.

    Through an ESPHome Bluetooth proxy, clear the proxy's stale per-MAC GATT
    cache *before* this call — while still connected in app mode — via
    ``OpenDisplayDevice.clear_gatt_cache()``, so this connection re-discovers
    the AppLoader's OTA service instead of the cached app-firmware table.

    Args:
        gbl_bytes: Raw .gbl firmware file bytes.
        ble_device: BLE device (same address as app mode).
        on_progress: Optional callback with float percentage 0–100.
        on_log: Optional callback for human-readable status messages.

    Raises:
        OTAError: OTA transfer failed or AppLoader did not appear within 20 s.
    """
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

    log = on_log or (lambda _: None)
    file_size = len(gbl_bytes)

    # Brief pause to let the device begin booting into AppLoader before the
    # first connection attempt. establish_connection then retries the connect
    # itself (max_attempts) — failed/incomplete connects are harmless because
    # the AppLoader only arms its reboot-on-disconnect after a *successful*
    # connection. We therefore retry the connect but NEVER reconnect once
    # connected: this single connection is our only chance to flash.
    log("Waiting for AppLoader to boot…")
    await asyncio.sleep(_SILABS_APPLOADER_BOOT_DELAY)

    try:
        # use_services_cache=False forces a fresh GATT discovery rather than
        # reusing the app-firmware service table. On an ESPHome Bluetooth proxy
        # the stale per-MAC GATT cache must additionally be cleared on the proxy
        # *before* this call (while still connected in app mode) via
        # OpenDisplayDevice.clear_gatt_cache(); see that method.
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.name or "AppLoader",
            use_services_cache=False,
            max_attempts=_SILABS_CONNECT_ATTEMPTS,
        )
    except Exception as exc:
        raise OTAError(f"Could not connect to AppLoader: {exc}") from exc

    try:
        # Identify the OTA service by its control/data characteristics rather
        # than the service UUID, which varies between AppLoader builds.
        char_uuids = {str(c.uuid).lower() for svc in client.services for c in svc.characteristics}
        if _SILABS_OTA_CONTROL_UUID not in char_uuids or _SILABS_OTA_DATA_UUID not in char_uuids:
            raise OTAError(
                "Device is not in Silabs OTA mode — AppLoader OTA characteristics "
                "not found. Cannot recover on this connection (reconnecting would "
                "reboot the device out of OTA mode); re-trigger the bootloader and retry."
            )

        log("Connected to AppLoader. Starting OTA transfer…")
        await client.write_gatt_char(_SILABS_OTA_CONTROL_UUID, bytearray([0x00]), response=True)

        # Windowed flow control. Write-without-response is fire-and-forget over an
        # ESPHome BT proxy (no backpressure): feeding it the whole image faster
        # than it forwards over BLE overruns it and the link drops before finalize.
        # Fully synchronous writes (response on every chunk) avoid that but are
        # painfully slow (a round-trip per chunk through the proxy). Instead we
        # stream a window of chunks write-without-response, then force one
        # write-with-response whose ATT ack drains the proxy's queue — bounding
        # in-flight writes while paying the latency only once per window. The
        # final chunk is always synced so the data is acked before the 0x03
        # finalize. (Same idea as nRF DFU's PRN, but the Silabs AppLoader has no
        # receipt-notification characteristic, so the ATT ack is the sync point.)
        sent = 0
        index = 0
        while sent < file_size:
            chunk = gbl_bytes[sent : sent + _SILABS_OTA_CHUNK_SIZE]
            sent += len(chunk)
            index += 1
            sync = index % _SILABS_OTA_WINDOW == 0 or sent >= file_size
            # Resend on proxy congestion ("Congested" = ESP TX buffer full): the
            # write hasn't been delivered, so back off briefly and retry the same
            # chunk rather than abort the transfer.
            for congestion_attempt in range(_SILABS_OTA_CONGESTION_RETRIES):
                try:
                    await client.write_gatt_char(_SILABS_OTA_DATA_UUID, chunk, response=sync)
                    break
                except Exception as exc:  # noqa: BLE001 - inspect message for congestion
                    is_congested = "congest" in str(exc).lower()
                    if not is_congested or congestion_attempt == _SILABS_OTA_CONGESTION_RETRIES - 1:
                        raise
                    await asyncio.sleep(_SILABS_OTA_CONGESTION_BACKOFF)
            if on_progress:
                on_progress(sent / file_size * 100)

        log("Stream complete. Finalizing…")
        await client.write_gatt_char(_SILABS_OTA_CONTROL_UUID, bytearray([0x03]), response=True)
        log("OTA complete — device is verifying and rebooting.")

    except OTAError:
        raise
    except Exception as exc:
        raise OTAError(f"Silabs OTA failed: {exc}") from exc
    finally:
        # The device reboots out of AppLoader on disconnect anyway; disconnect
        # explicitly so we don't leak the connection if the transfer raised.
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


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
