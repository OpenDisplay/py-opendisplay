"""Pytest configuration and fixtures."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest
from PIL import Image

# Path to captured real protocol data
FIXTURES_DIR = Path(__file__).parent / "fixtures/real_protocol_data"


class FakeTransport:
    """In-memory Transport implementation for driving device flows without bleak.

    Replays a scripted list of response frames (bytes, or an Exception
    class/instance to raise) and records every written command. Structurally
    satisfies ``opendisplay.transport.Transport``, so it can back PIPE/crypto/
    upload suites without a real BLE or TCP link.
    """

    def __init__(
        self,
        responses: list | None = None,
        *,
        max_frame: int = 244,
        supports_write_without_response: bool = True,
        device_name: str | None = "FakeDevice",
    ) -> None:
        self.max_frame = max_frame
        self.supports_write_without_response = supports_write_without_response
        self.device_name = device_name
        self.written: list[bytes] = []
        self.write_responses: list[bool] = []
        self.drain_flags: list[bool] = []
        self.timeouts: list[float] = []
        self._responses: deque = deque(responses or [])
        self._connected = False
        self.drained = 0

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)
        self.write_responses.append(response)
        self.drain_flags.append(drain_stale)

    async def read_response(self, timeout: float = 5.0) -> bytes:
        self.timeouts.append(timeout)
        if not self._responses:
            raise RuntimeError("FakeTransport: no responses left")
        item = self._responses.popleft()
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item("scripted")
        if isinstance(item, BaseException):
            raise item
        return item

    def drain_notifications(self) -> int:
        self.drained += 1  # record the call; scripted responses are never dropped
        return 0

    async def clear_cache(self) -> bool:
        return False


@pytest.fixture
def fake_transport():
    """Factory fixture returning a configured :class:`FakeTransport`."""

    def _make(responses: list | None = None, **kwargs: object) -> FakeTransport:
        return FakeTransport(responses, **kwargs)

    return _make


@pytest.fixture
def small_test_image():
    """Create a small RGB test image for encoding tests."""
    return Image.new("RGB", (10, 10), color=(128, 128, 128))


@pytest.fixture
def real_read_config_command():
    """Real READ_CONFIG command captured from device."""
    file = FIXTURES_DIR / "01_read_config_command.bin"
    if file.exists():
        return file.read_bytes()
    # Fallback if not captured yet
    return b"\x00\x40"


@pytest.fixture
def real_read_config_response():
    """Real config response from actual device."""
    file = FIXTURES_DIR / "01_read_config_response.bin"
    if file.exists():
        return file.read_bytes()
    return b""


@pytest.fixture
def real_firmware_command():
    """Real READ_FW_VERSION command."""
    file = FIXTURES_DIR / "02_read_firmware_command.bin"
    if file.exists():
        return file.read_bytes()
    return b"\x00\x43"


@pytest.fixture
def real_firmware_response():
    """Real firmware version response from device."""
    file = FIXTURES_DIR / "02_read_firmware_response.bin"
    if file.exists():
        return file.read_bytes()
    return b""


@pytest.fixture
def real_upload_start_command():
    """Real DIRECT_WRITE_START command (uncompressed)."""
    file = FIXTURES_DIR / "03_upload_start_uncompressed_command.bin"
    if file.exists():
        return file.read_bytes()
    return b"\x00\x70"


@pytest.fixture
def real_data_chunk_command():
    """Real DIRECT_WRITE_DATA chunk command."""
    file = FIXTURES_DIR / "04_data_chunk_command.bin"
    if file.exists():
        return file.read_bytes()
    return b""


@pytest.fixture
def real_upload_end_command():
    """Real DIRECT_WRITE_END command."""
    file = FIXTURES_DIR / "05_upload_end_command.bin"
    if file.exists():
        return file.read_bytes()
    return b"\x00\x72\x00"


def real_advertisement_data():
    """Real advertisement data from the device (manufacturer ID stripped by Bleak)."""
    # Format: [protocol:7][battery:2 LE][temp:1 signed][loop:1]
    # Real captured data: Battery 3925mV, Temp 22°C, Loop 77
    return bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0x4D])


@pytest.fixture(autouse=True)
def _forget_redraw_notes():
    """Each test starts with no display noted as redrawing (opendisplay.redraw is process-wide)."""
    from opendisplay import redraw

    redraw._reset()
    yield
    redraw._reset()


@pytest.fixture(autouse=True)
def _no_release_downloads(monkeypatch):
    """No test reaches GitHub: a release fetch fails as it does offline (tests that want one pass ``get``)."""
    from opendisplay.firmware import download

    def offline(url: str) -> bytes:
        raise download.DownloadError(f"tests are offline: {url}")

    monkeypatch.setattr(download, "_get", offline)
