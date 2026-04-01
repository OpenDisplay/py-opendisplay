"""BLE device discovery for OpenDisplay devices."""

from __future__ import annotations

import logging

from bleak import BleakScanner

from .exceptions import BLETimeoutError
from .models.advertisement import AdvertisementData, parse_advertisement
from .protocol import MANUFACTURER_ID

_LOGGER = logging.getLogger(__name__)


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
