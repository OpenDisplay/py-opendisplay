"""Tests for OTA firmware update utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendisplay.exceptions import OTAError
from opendisplay.ota import (
    _SILABS_APPLOADER_BOOT_DELAY,
    _SILABS_OTA_CHUNK_SIZE,
    _SILABS_OTA_CONTROL_UUID,
    _SILABS_OTA_DATA_UUID,
    _SILABS_OTA_WINDOW,
    find_nrf_dfu_device,
    perform_silabs_ota,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bleak_client(char_uuids: list[str]) -> MagicMock:
    """Return a mock client (as returned by establish_connection) whose services
    expose the given char UUIDs. write_gatt_char/disconnect are AsyncMocks."""
    char_mocks = [MagicMock(uuid=uuid) for uuid in char_uuids]
    svc = MagicMock()
    svc.characteristics = char_mocks
    client = AsyncMock()
    client.services = [svc]
    return client


def _patch_connect(client_or_exc) -> object:
    """Patch establish_connection to return a client (or raise an exception)."""
    if isinstance(client_or_exc, BaseException):
        return patch(
            "bleak_retry_connector.establish_connection",
            new=AsyncMock(side_effect=client_or_exc),
        )
    return patch(
        "bleak_retry_connector.establish_connection",
        new=AsyncMock(return_value=client_or_exc),
    )


def _make_ble_device(address: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    dev = MagicMock()
    dev.address = address
    return dev


# ---------------------------------------------------------------------------
# perform_silabs_ota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silabs_ota_happy_path() -> None:
    """Full OTA transfer: start write, all data chunks, finalize write, disconnect."""
    gbl = bytes(range(256)) * 2  # 512 bytes → 3 chunks of 244/244/24
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])
    progress: list[float] = []
    connect = AsyncMock(return_value=client)

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        patch("bleak_retry_connector.establish_connection", new=connect),
    ):
        await perform_silabs_ota(gbl, _make_ble_device(), on_progress=progress.append)

    # Fresh GATT discovery, not the cached app-firmware service table.
    assert connect.await_args.kwargs["use_services_cache"] is False

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
    # Windowed flow control: earlier chunks stream write-without-response; the
    # final chunk is synced (response=True) so data is acked before finalize.
    assert data_calls[0].kwargs["response"] is False
    assert data_calls[-1].kwargs["response"] is True

    # Last call: OTA finalize (0x03)
    assert calls[-1].args[0] == _SILABS_OTA_CONTROL_UUID
    assert calls[-1].args[1] == bytearray([0x03])
    assert calls[-1].kwargs["response"] is True

    # Progress goes from > 0 to 100
    assert progress[0] > 0
    assert progress[-1] == pytest.approx(100.0)

    # The single connection is always closed.
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_silabs_ota_windowed_flow_control() -> None:
    """Data chunks sync (response=True) every WINDOW chunks and on the last chunk."""
    n_chunks = _SILABS_OTA_WINDOW * 2 + 3  # spans 2 full windows + a partial tail
    gbl = b"\x00" * (_SILABS_OTA_CHUNK_SIZE * n_chunks)
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
    ):
        await perform_silabs_ota(gbl, _make_ble_device())

    data_calls = [c for c in client.write_gatt_char.call_args_list if c.args[0] == _SILABS_OTA_DATA_UUID]
    assert len(data_calls) == n_chunks
    # 1-based chunk indices that were synced.
    synced = [i for i, c in enumerate(data_calls, start=1) if c.kwargs["response"] is True]
    expected = sorted(set(range(_SILABS_OTA_WINDOW, n_chunks + 1, _SILABS_OTA_WINDOW)) | {n_chunks})
    assert synced == expected


@pytest.mark.asyncio
async def test_silabs_ota_retries_on_congestion() -> None:
    """A 'Congested' proxy error on a data write is retried, not fatal."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])
    fails = {"n": 0}

    async def write(uuid, data, response=False):  # noqa: ARG001
        if uuid == _SILABS_OTA_DATA_UUID and fails["n"] < 2:
            fails["n"] += 1
            raise RuntimeError("Bluetooth GATT Error ... description=Congested")

    client.write_gatt_char = AsyncMock(side_effect=write)

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
    ):
        await perform_silabs_ota(b"\x00" * (_SILABS_OTA_CHUNK_SIZE * 2), _make_ble_device())

    # The first data chunk was resent twice (Congested) then succeeded.
    assert fails["n"] == 2


@pytest.mark.asyncio
async def test_silabs_ota_waits_for_apploader_boot() -> None:
    """perform_silabs_ota waits for the AppLoader to boot before connecting."""
    sleep_mock = AsyncMock()
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=sleep_mock),
        _patch_connect(client),
    ):
        await perform_silabs_ota(b"\x00" * 10, _make_ble_device())

    sleep_mock.assert_awaited_once_with(_SILABS_APPLOADER_BOOT_DELAY)


@pytest.mark.asyncio
async def test_silabs_ota_missing_control_char_raises() -> None:
    """OTAError when the AppLoader OTA control characteristic is absent."""
    client = _make_bleak_client([_SILABS_OTA_DATA_UUID, "some-other-uuid"])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
    ):
        with pytest.raises(OTAError, match="not in Silabs OTA mode"):
            await perform_silabs_ota(b"\x00" * 10, _make_ble_device())

    # Even on failure the one-shot connection is closed.
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_silabs_ota_missing_data_char_raises() -> None:
    """OTAError when the OTA data characteristic is absent (control present)."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
    ):
        with pytest.raises(OTAError, match="not in Silabs OTA mode"):
            await perform_silabs_ota(b"\x00" * 10, _make_ble_device())


@pytest.mark.asyncio
async def test_silabs_ota_connect_failure_wrapped() -> None:
    """A failed connection is wrapped in OTAError with a connect-specific message."""
    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(RuntimeError("connection refused")),
    ):
        with pytest.raises(OTAError, match="Could not connect to AppLoader"):
            await perform_silabs_ota(b"\x00" * 10, _make_ble_device())


@pytest.mark.asyncio
async def test_silabs_ota_transfer_error_wrapped() -> None:
    """An error mid-transfer is wrapped in OTAError and the connection still closed."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])
    client.write_gatt_char = AsyncMock(side_effect=RuntimeError("gatt write failed"))

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
    ):
        with pytest.raises(OTAError, match="Silabs OTA failed"):
            await perform_silabs_ota(b"\x00" * 10, _make_ble_device())

    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_silabs_ota_no_progress_callback() -> None:
    """perform_silabs_ota works without an on_progress callback."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
    ):
        await perform_silabs_ota(b"\x00" * 10, _make_ble_device(), on_progress=None)


@pytest.mark.asyncio
async def test_silabs_ota_log_callback() -> None:
    """on_log receives status messages during the transfer."""
    client = _make_bleak_client([_SILABS_OTA_CONTROL_UUID, _SILABS_OTA_DATA_UUID])
    logs: list[str] = []

    with (
        patch("opendisplay.ota.asyncio.sleep", new=AsyncMock()),
        _patch_connect(client),
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
