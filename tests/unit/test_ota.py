"""Tests for OTA firmware update utilities.

The Silabs and nRF OTA *protocols* live in their own libraries
(``silabs-ble-ota`` / ``nrf-ota``) and are tested there. Here we only cover the
py-opendisplay-side glue: ``find_nrf_dfu_device`` and the lazy-import guards in
the ``perform_*`` wrappers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendisplay.exceptions import OTAError
from opendisplay.ota import find_nrf_dfu_device


def _make_ble_device(address: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    dev = MagicMock()
    dev.address = address
    return dev


# ---------------------------------------------------------------------------
# find_nrf_dfu_device
# ---------------------------------------------------------------------------


def _make_scanner_device(address: str) -> MagicMock:
    dev = MagicMock()
    dev.address = address
    return dev


@pytest.mark.asyncio
async def test_find_nrf_dfu_device_mac_plus1() -> None:
    """Device found at MAC+1 on the first attempt."""
    dfu_dev = _make_scanner_device("AA:BB:CC:DD:EE:02")

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakScanner") as scanner_cls,
    ):
        scanner_cls.discover = AsyncMock(return_value=[dfu_dev])
        result = await find_nrf_dfu_device("AA:BB:CC:DD:EE:01")

    assert result is dfu_dev


@pytest.mark.asyncio
async def test_find_nrf_dfu_device_original_address_after_5_attempts() -> None:
    """Original address is only checked after 5 attempts (10 s)."""
    original_dev = _make_scanner_device("AA:BB:CC:DD:EE:01")
    attempt = 0

    async def _discover(timeout: float) -> list[MagicMock]:
        nonlocal attempt
        result = [original_dev] if attempt >= 5 else []
        attempt += 1
        return result

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakScanner") as scanner_cls,
    ):
        scanner_cls.discover = _discover
        result = await find_nrf_dfu_device("AA:BB:CC:DD:EE:01")

    assert result is original_dev
    assert attempt == 6  # found on attempt index 5


@pytest.mark.asyncio
async def test_find_nrf_dfu_device_not_found_returns_none() -> None:
    """Returns None after 15 attempts with no matching device."""
    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakScanner") as scanner_cls,
    ):
        scanner_cls.discover = AsyncMock(return_value=[])
        result = await find_nrf_dfu_device("AA:BB:CC:DD:EE:01")

    assert result is None


@pytest.mark.asyncio
async def test_find_nrf_dfu_device_mac_plus1_wraps_ff() -> None:
    """MAC+1 wraps correctly: EE:FF → EE:00."""
    dfu_dev = _make_scanner_device("AA:BB:CC:DD:EE:00")

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakScanner") as scanner_cls,
    ):
        scanner_cls.discover = AsyncMock(return_value=[dfu_dev])
        result = await find_nrf_dfu_device("AA:BB:CC:DD:EE:FF")

    assert result is dfu_dev


# ---------------------------------------------------------------------------
# perform_* wrappers — optional-dependency import guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nrf_dfu_missing_dependency_raises() -> None:
    """OTAError with install hint when nrf-ota is not installed."""
    from opendisplay.ota import perform_nrf_dfu

    blocked = {
        "nrf_ota": None,
        "nrf_ota._const": None,
        "nrf_ota._zip": None,
        "nrf_ota.dfu": None,
    }
    with patch.dict("sys.modules", blocked):
        with pytest.raises(OTAError, match="nrf-ota is required"):
            await perform_nrf_dfu(b"", _make_ble_device())


@pytest.mark.asyncio
async def test_silabs_ota_missing_dependency_raises() -> None:
    """OTAError with install hint when silabs-ble-ota is not installed."""
    from opendisplay.ota import perform_silabs_ota

    with patch.dict("sys.modules", {"silabs_ble_ota": None}):
        with pytest.raises(OTAError, match="silabs-ble-ota is required"):
            await perform_silabs_ota(b"", _make_ble_device())
