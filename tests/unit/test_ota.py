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


# ---------------------------------------------------------------------------
# perform_silabs_ota — delegates to silabs-ble-ota, maps its error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silabs_ota_delegates_and_maps_errors() -> None:
    """The wrapper forwards (incl. fast=) to silabs_ble_ota and maps SilabsOTAError → OTAError."""
    import types

    from opendisplay.ota import perform_silabs_ota

    fake = types.ModuleType("silabs_ble_ota")

    class _SilabsOTAError(Exception):
        pass

    seen: dict[str, object] = {}

    async def _flash(gbl, dev, on_progress, on_log, *, fast):  # noqa: ANN001
        seen["call"] = (gbl, dev, fast)

    fake.SilabsOTAError = _SilabsOTAError  # type: ignore[attr-defined]
    fake.perform_silabs_ota = _flash  # type: ignore[attr-defined]

    dev = _make_ble_device()
    with patch.dict("sys.modules", {"silabs_ble_ota": fake}):
        await perform_silabs_ota(b"GBL", dev, fast=True)
        assert seen["call"] == (b"GBL", dev, True)

        async def _flash_fail(*_a, **_k):
            raise _SilabsOTAError("boom")

        fake.perform_silabs_ota = _flash_fail  # type: ignore[attr-defined]
        with pytest.raises(OTAError, match="boom"):
            await perform_silabs_ota(b"", _make_ble_device())


# ---------------------------------------------------------------------------
# perform_nrf_dfu — connect (via establish_connection) + LegacyDFU orchestration
# ---------------------------------------------------------------------------


def _make_dfu_client(service_uuid: str = "00001530-1212-efde-1523-785feabcd123") -> AsyncMock:
    """A connected DFU client exposing the Legacy DFU service UUID."""
    client = AsyncMock()
    svc = MagicMock()
    svc.uuid = service_uuid
    client.services = [svc]
    return client


@pytest.mark.asyncio
async def test_perform_nrf_dfu_happy_path() -> None:
    """Connects, runs the full LegacyDFU sequence, and disconnects."""
    from opendisplay.ota import perform_nrf_dfu

    client = _make_dfu_client()
    dfu = AsyncMock()
    dfu.read_version = AsyncMock(return_value=(0, 8))
    zip_info = MagicMock(firmware=b"\x00" * 100, init_packet=b"\x01\x02")

    with (
        patch("bleak_retry_connector.establish_connection", new=AsyncMock(return_value=client)),
        patch("nrf_ota.dfu.LegacyDFU", return_value=dfu),
        patch("nrf_ota._zip._parse_zip_bytes", return_value=zip_info),
    ):
        await perform_nrf_dfu(b"zip", _make_ble_device())

    dfu.start.assert_awaited_once()
    dfu.start_dfu.assert_awaited_once()
    dfu.init_dfu.assert_awaited_once()
    dfu.send_firmware.assert_awaited_once()
    dfu.activate_and_reset.assert_awaited_once()
    client.disconnect.assert_awaited()
    # default (not fast) paces the firmware stream
    assert dfu.send_firmware.await_args.kwargs["inter_packet_delay"] > 0


@pytest.mark.asyncio
async def test_perform_nrf_dfu_fast_sends_unpaced() -> None:
    """fast=True streams with no inter-packet delay."""
    from opendisplay.ota import perform_nrf_dfu

    client = _make_dfu_client()
    dfu = AsyncMock()
    dfu.read_version = AsyncMock(return_value=(0, 8))
    with (
        patch("bleak_retry_connector.establish_connection", new=AsyncMock(return_value=client)),
        patch("nrf_ota.dfu.LegacyDFU", return_value=dfu),
        patch("nrf_ota._zip._parse_zip_bytes", return_value=MagicMock(firmware=b"x" * 40, init_packet=b"i")),
    ):
        await perform_nrf_dfu(b"zip", _make_ble_device(), fast=True)
    assert dfu.send_firmware.await_args.kwargs["inter_packet_delay"] == 0.0


@pytest.mark.asyncio
async def test_perform_nrf_dfu_not_in_dfu_mode_raises() -> None:
    """A device without the Legacy DFU service raises OTAError and still disconnects."""
    from opendisplay.ota import perform_nrf_dfu

    client = _make_dfu_client(service_uuid="0000abcd-0000-1000-8000-00805f9b34fb")
    with (
        patch("bleak_retry_connector.establish_connection", new=AsyncMock(return_value=client)),
        patch("nrf_ota._zip._parse_zip_bytes", return_value=MagicMock(firmware=b"x", init_packet=b"i")),
    ):
        with pytest.raises(OTAError, match="not in Nordic Legacy DFU mode"):
            await perform_nrf_dfu(b"zip", _make_ble_device())
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_perform_nrf_dfu_connect_failure_raises() -> None:
    """A failed connection is wrapped in OTAError."""
    from opendisplay.ota import perform_nrf_dfu

    with (
        patch("bleak_retry_connector.establish_connection", new=AsyncMock(side_effect=RuntimeError("nope"))),
        patch("nrf_ota._zip._parse_zip_bytes", return_value=MagicMock(firmware=b"x", init_packet=b"i")),
    ):
        with pytest.raises(OTAError, match="Could not connect to nRF DFU bootloader"):
            await perform_nrf_dfu(b"zip", _make_ble_device())
