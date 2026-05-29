"""Tests for OTA firmware update utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendisplay.exceptions import OTAError
from opendisplay.ota import (
    _SILABS_OTA_CHUNK_SIZE,
    _SILABS_OTA_CONTROL_UUID,
    _SILABS_OTA_DATA_UUID,
    find_nrf_dfu_device,
    perform_silabs_ota,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bleak_client(char_uuids: list[str]) -> MagicMock:
    """Return a mock BleakClient whose services expose the given char UUIDs."""
    char_mocks = [MagicMock(uuid=uuid) for uuid in char_uuids]
    svc = MagicMock()
    svc.characteristics = char_mocks
    client = AsyncMock()
    client.services = [svc]
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_ble_device(address: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    dev = MagicMock()
    dev.address = address
    return dev


# ---------------------------------------------------------------------------
# perform_silabs_ota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silabs_ota_happy_path() -> None:
    """Full OTA transfer: start write, all data chunks, finalize write."""
    gbl = bytes(range(256)) * 2  # 512 bytes → 3 chunks of 244/244/24
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])
    progress: list[float] = []

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakClient", return_value=client),
    ):
        await perform_silabs_ota(gbl, _make_ble_device(), on_progress=progress.append)

    calls = client.write_gatt_char.call_args_list
    # First call: OTA start (0x00)
    assert calls[0].args[0] == _SILABS_OTA_CONTROL_UUID
    assert calls[0].args[1] == bytearray([0x00])
    assert calls[0].kwargs["response"] is True

    # Middle calls: data chunks
    data_calls = [c for c in calls if c.args[0] == _SILABS_OTA_DATA_UUID]
    total_sent = sum(len(c.args[1]) for c in data_calls)
    assert total_sent == len(gbl)
    assert all(len(c.args[1]) <= _SILABS_OTA_CHUNK_SIZE for c in data_calls)
    assert all(c.kwargs["response"] is False for c in data_calls)

    # Last call: OTA finalize (0x03)
    assert calls[-1].args[0] == _SILABS_OTA_CONTROL_UUID
    assert calls[-1].args[1] == bytearray([0x03])
    assert calls[-1].kwargs["response"] is True

    # Progress goes from > 0 to 100
    assert progress[0] > 0
    assert progress[-1] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_silabs_ota_sleeps_before_connect() -> None:
    """perform_silabs_ota waits for AppLoader to boot before connecting."""
    sleep_mock = AsyncMock()
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=sleep_mock),
        patch("bleak.BleakClient", return_value=client),
    ):
        await perform_silabs_ota(b"\x00" * 10, _make_ble_device())

    sleep_mock.assert_awaited_once_with(6.0)


@pytest.mark.asyncio
async def test_silabs_ota_missing_control_char_raises() -> None:
    """OTAError when the AppLoader OTA control characteristic is absent."""
    client = _make_bleak_client(["some-other-uuid"])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakClient", return_value=client),
    ):
        with pytest.raises(OTAError, match="not in Silabs OTA mode"):
            await perform_silabs_ota(b"\x00" * 10, _make_ble_device())


@pytest.mark.asyncio
async def test_silabs_ota_connection_error_wrapped() -> None:
    """Non-OTA exceptions from BleakClient are wrapped in OTAError."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=RuntimeError("connection refused"))
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakClient", return_value=client),
    ):
        with pytest.raises(OTAError, match="Silabs OTA failed"):
            await perform_silabs_ota(b"\x00" * 10, _make_ble_device())


@pytest.mark.asyncio
async def test_silabs_ota_no_progress_callback() -> None:
    """perform_silabs_ota works without an on_progress callback."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakClient", return_value=client),
    ):
        await perform_silabs_ota(b"\x00" * 10, _make_ble_device(), on_progress=None)


@pytest.mark.asyncio
async def test_silabs_ota_log_callback() -> None:
    """on_log receives status messages during the transfer."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])
    logs: list[str] = []

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak.BleakClient", return_value=client),
    ):
        await perform_silabs_ota(b"\x00" * 10, _make_ble_device(), on_log=logs.append)

    assert any("AppLoader" in msg for msg in logs)
    assert any("complete" in msg.lower() for msg in logs)


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
# perform_nrf_dfu — import guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nrf_dfu_missing_dependency_raises() -> None:
    """OTAError with install hint when nrf-ota is not installed."""
    import sys

    # Temporarily hide nrf_ota from imports
    saved = {k: v for k, v in sys.modules.items() if k.startswith("nrf_ota")}
    for key in list(sys.modules):
        if key.startswith("nrf_ota"):
            sys.modules[key] = None  # type: ignore[assignment]

    try:
        from opendisplay.ota import perform_nrf_dfu

        with pytest.raises(OTAError, match="nrf-ota is required"):
            await perform_nrf_dfu(b"", _make_ble_device())
    finally:
        for key in list(sys.modules):
            if key.startswith("nrf_ota"):
                del sys.modules[key]
        sys.modules.update(saved)
