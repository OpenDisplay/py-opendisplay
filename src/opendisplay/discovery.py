"""BLE and LAN discovery for OpenDisplay devices."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Mapping
from typing import Any

from bleak import BleakScanner

from .exceptions import BLETimeoutError
from .models.advertisement import AdvertisementData, parse_advertisement
from .protocol import MANUFACTURER_ID

_LOGGER = logging.getLogger(__name__)

_OPENDISPLAY_TCP_TYPE = "_opendisplay._tcp.local."
# Same string as ``_OPENDISPLAY_TCP_TYPE`` (ESP32 ``MDNS.addService("opendisplay", "tcp", ...)``).
OPENDISPLAY_MDNS_SERVICE_TYPE = _OPENDISPLAY_TCP_TYPE
# TXT key for 14-byte v1 MSD payload (company ID excluded), lowercase hex (28 chars).
MDNS_TXT_KEY_MSD = "msd"


def _discover_lan_sync(scan_seconds: float) -> list[tuple[str, int, str]]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as e:
        raise ImportError("LAN discovery requires the 'zeroconf' package (pip install py-opendisplay[lan])") from e

    found: list[tuple[str, int, str]] = []

    class _Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=3000)
            if not info or info.port == 0:
                return
            v4 = next((a for a in info.addresses if len(a) == 4), None)
            if not v4:
                return
            ip = socket.inet_ntoa(v4)
            found.append((ip, int(info.port or 0), name))
            _LOGGER.debug("LAN service %s -> %s:%s", name, ip, info.port)

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
            pass

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, _OPENDISPLAY_TCP_TYPE, _Listener())
        time.sleep(max(scan_seconds, 0.1))
    finally:
        zc.close()

    return found


def msd_bytes_from_mdns_txt_properties(properties: Mapping[Any, Any] | None) -> bytes | None:
    """Decode 14-byte v1 MSD from mDNS TXT ``msd`` (28 lowercase hex chars).

    Returns:
        14 bytes matching BLE ``parse_advertisement`` v1 payload, or None if missing/invalid.
    """
    if not properties:
        return None
    raw_val: bytes | None = None
    for k, v in properties.items():
        key = k if isinstance(k, bytes) else str(k).encode("utf-8")
        if key.lower() != b"msd":
            continue
        if v is None:
            return None
        raw_val = v if isinstance(v, bytes) else str(v).encode("utf-8")
        break
    if raw_val is None:
        return None
    s = raw_val.decode("ascii", errors="replace").strip()
    if len(s) != 28:
        return None
    try:
        out = bytes.fromhex(s)
    except ValueError:
        return None
    return out if len(out) == 14 else None


async def discover_lan_devices(scan_seconds: float = 3.0) -> list[tuple[str, int, str]]:
    """Browse mDNS for ``_opendisplay._tcp`` (display TCP server on LAN).

    Returns:
        List of ``(ipv4, port, service_instance_name)``.
    """
    return await asyncio.to_thread(_discover_lan_sync, scan_seconds)


async def discover_devices_with_adv(
    timeout: float = 10.0,
    manufacturer_id: int = MANUFACTURER_ID,
) -> dict[str, tuple[str, AdvertisementData | None]]:
    """Discover OpenDisplay BLE devices and parse their advertisement data.

    Scans for BLE devices with OpenDisplay manufacturer ID and returns a mapping
    of device names to (MAC address, parsed advertisement data). Advertisement data
    contains battery voltage, chip temperature, and loop counter broadcast passively
    by the device — no BLE connection required.

    Args:
        timeout: Scan duration in seconds (default: 10.0)
        manufacturer_id: Manufacturer ID to filter (default: 0x2446)

    Returns:
        Dictionary mapping device_name -> (mac_address, AdvertisementData | None)
        - AdvertisementData is None if the manufacturer payload could not be parsed.
        - If device has no name, uses "Unknown_{last_4_chars_of_mac}"
        - If duplicate names found, appends "_{last_4}" to subsequent ones

    Raises:
        BLETimeoutError: If scan fails to complete
    """
    _LOGGER.debug("Starting BLE scan (timeout=%ds, manufacturer_id=0x%04x)", timeout, manufacturer_id)

    try:
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception as e:
        raise BLETimeoutError(f"BLE scan failed: {e}") from e

    result: dict[str, tuple[str, AdvertisementData | None]] = {}
    name_counts: dict[str, int] = {}

    for device, adv_data in devices.values():
        if manufacturer_id not in adv_data.manufacturer_data:
            continue

        if device.name:
            name = device.name
        else:
            mac_suffix = device.address.replace(":", "")[-4:]
            name = f"Unknown_{mac_suffix}"

        if name in result:
            count = name_counts.get(name, 1) + 1
            name_counts[name] = count
            mac_suffix = device.address.replace(":", "")[-4:]
            name = f"{name}_{mac_suffix}"

        raw = adv_data.manufacturer_data[manufacturer_id]
        try:
            adv = parse_advertisement(raw)
        except Exception:
            adv = None

        result[name] = (device.address, adv)
        _LOGGER.debug("Found device: %s (%s)", name, device.address)

    _LOGGER.info("Discovery complete: found %d OpenDisplay device(s)", len(result))
    return result


async def discover_devices(
    timeout: float = 10.0,
    manufacturer_id: int = MANUFACTURER_ID,
) -> dict[str, str]:
    """Discover OpenDisplay BLE devices.

    Scans for BLE devices with OpenDisplay manufacturer ID and returns
    a mapping of device names to MAC addresses.

    Args:
        timeout: Scan duration in seconds (default: 10.0)
        manufacturer_id: Manufacturer ID to filter (default: 0x2446)

    Returns:
        Dictionary mapping device_name -> mac_address
        - If device has no name, uses "Unknown_{last_4_chars_of_mac}"
        - If duplicate names found, appends "_{last_4}" to subsequent ones

    Raises:
        BLETimeoutError: If scan fails to complete

    Example:
        devices = await discover_devices(timeout=5.0)
        # Returns: {"OpenDisplay-A123": "AA:BB:CC:DD:EE:FF", ...}
    """
    return {name: mac for name, (mac, _) in (await discover_devices_with_adv(timeout, manufacturer_id)).items()}
