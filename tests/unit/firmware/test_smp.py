"""The SMP wrapper: upload, mark the image for a test boot, reset, confirm (through smpclient)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite

from opendisplay.firmware import smp

IMAGE = bytes(range(256)) * 40  # 10 KiB


@pytest.fixture(autouse=True)
def _replies_are_successes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake's replies are not smpclient response objects: treat them as successes."""
    monkeypatch.setattr(smp, "_is_error", lambda reply: False)


class _FakeClient:
    """Stands in for smpclient.SMPClient: records requests, scripts upload offsets and replies."""

    def __init__(self, *, offsets: list[int] | None = None, upload_error: Exception | None = None) -> None:
        self.requests: list[Any] = []
        self.uploaded: list[bytes] = []
        self._offsets = offsets if offsets is not None else [4096, 8192, len(IMAGE)]
        self._upload_error = upload_error
        self.replies: dict[type, Any] = {}

    async def upload(self, image: bytes, **_: Any) -> AsyncIterator[int]:
        self.uploaded.append(image)
        for offset in self._offsets:
            if self._upload_error is not None and offset > 4096:
                raise self._upload_error
            yield offset

    async def request(self, request: Any, timeout_s: float | None = None) -> Any:
        self.requests.append(request)
        return self.replies.get(type(request), _Ok())


class _Ok:
    pass


async def test_upload_reports_progress_up_to_the_image_size() -> None:
    client = _FakeClient()
    progress: list[tuple[int, int]] = []

    await smp.upload(client, IMAGE, on_progress=lambda done, total: progress.append((done, total)))  # type: ignore[arg-type]

    assert client.uploaded == [IMAGE]
    assert progress == [(4096, len(IMAGE)), (8192, len(IMAGE)), (len(IMAGE), len(IMAGE))]


async def test_an_interrupted_upload_raises_a_typed_error() -> None:
    client = _FakeClient(upload_error=TimeoutError("link lost"))

    with pytest.raises(smp.SmpTransferError, match="4096 of 10240"):
        await smp.upload(client, IMAGE)  # type: ignore[arg-type]


def _slots(*hashes: bytes) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        images=[SimpleNamespace(slot=n, hash=h, active=n == 0, confirmed=n == 0) for n, h in enumerate(hashes)]
    )


async def test_test_and_reset_marks_the_uploaded_slot_by_the_hash_the_device_reports() -> None:
    # The device names an image by its own hash (ESP32: esp_partition_get_sha256 of the slot;
    # MCUboot: the image hash), not the file's SHA-256 (od_smp.cpp handle_state_write).
    client = _FakeClient()
    device_hash = b"\xab" * 32
    client.replies[ImageStatesRead] = _slots(b"\x01" * 32, device_hash)

    assert await smp.test_and_reset(client) == device_hash  # type: ignore[arg-type]

    read, write, reset = client.requests
    assert isinstance(read, ImageStatesRead)
    assert isinstance(write, ImageStatesWrite)
    assert write.hash == device_hash and write.hash != hashlib.sha256(IMAGE).digest()
    assert write.confirm is False  # a test boot, not permanent
    assert isinstance(reset, ResetWrite)


async def test_test_and_reset_without_an_uploaded_slot_refuses() -> None:
    client = _FakeClient()
    client.replies[ImageStatesRead] = _slots(b"\x01" * 32)

    with pytest.raises(smp.SmpError, match="slot 1"):
        await smp.test_and_reset(client)  # type: ignore[arg-type]

    assert not any(isinstance(r, ResetWrite) for r in client.requests)


async def test_confirm_makes_the_running_image_permanent() -> None:
    client = _FakeClient()

    await smp.confirm(client)  # type: ignore[arg-type]

    (write,) = client.requests
    assert isinstance(write, ImageStatesWrite) and write.confirm is True and write.hash is None


async def test_a_refused_request_raises_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(smp, "_is_error", lambda reply: True)

    with pytest.raises(smp.SmpError, match="ImageStatesWrite"):
        await smp.confirm(client)  # type: ignore[arg-type]


async def test_image_states_lists_the_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()

    class _States:
        images = ["slot0", "slot1"]

    client.replies[ImageStatesRead] = _States()

    assert await smp.image_states(client) == ["slot0", "slot1"]  # type: ignore[arg-type]


async def test_the_transport_connects_to_the_given_device_and_never_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    # Home Assistant and ESPHome proxies hand over a BLEDevice; smpclient's own transport scans.
    from types import SimpleNamespace

    import bleak_retry_connector
    from bleak import BleakScanner

    device = SimpleNamespace(name="OD4064C9", address="AA:BB:CC:DD:EE:FF")
    connected: list[Any] = []
    notified: list[str] = []

    class _Backend:  # not BlueZ: the characteristic's size is used as is
        pass

    async def start_notify(uuid: Any, callback: Any) -> None:
        notified.append(str(uuid))

    client = SimpleNamespace(
        services=SimpleNamespace(get_characteristic=lambda uuid: SimpleNamespace(max_write_without_response_size=244)),
        start_notify=start_notify,
        _backend=_Backend(),
    )

    async def establish_connection(cls: Any, ble_device: Any, name: str, **kwargs: Any) -> Any:
        connected.append(ble_device)
        return client

    async def no_scan(*args: Any, **kwargs: Any) -> None:
        pytest.fail("scanned")

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish_connection)
    monkeypatch.setattr(BleakScanner, "find_device_by_address", no_scan)
    transport = smp._transport_class()(device)

    await transport._connect("ignored", 5.0)

    assert connected == [device]
    assert transport.mtu == 244 and notified


async def test_a_stale_service_cache_is_cleared_once_and_the_link_reopened(monkeypatch: pytest.MonkeyPatch) -> None:
    # An ESPHome proxy kept the display's 2.x service table (no SMP service) after it moved to 3.0;
    # seen on the Bedroom proxy with the E1002. One clear_cache and a new link find it.
    from types import SimpleNamespace

    import bleak_retry_connector

    device = SimpleNamespace(name="OD4064C9", address="44:1B:F6:C9:64:41")
    events: list[str] = []

    class _Backend:
        pass

    def _client(has_smp: bool, label: str) -> Any:
        async def clear_cache() -> bool:
            events.append(f"clear_cache:{label}")
            return True

        async def disconnect() -> None:
            events.append(f"disconnect:{label}")

        async def start_notify(uuid: Any, callback: Any) -> None:
            events.append(f"notify:{label}")

        characteristic = SimpleNamespace(max_write_without_response_size=244) if has_smp else None
        return SimpleNamespace(
            services=SimpleNamespace(get_characteristic=lambda uuid: characteristic),
            clear_cache=clear_cache,
            disconnect=disconnect,
            start_notify=start_notify,
            _backend=_Backend(),
        )

    clients = [_client(False, "stale"), _client(True, "fresh")]

    async def establish_connection(cls: Any, ble_device: Any, name: str, **kwargs: Any) -> Any:
        events.append("connect")
        return clients.pop(0)

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish_connection)
    transport = smp._transport_class()(device)

    await transport._connect("ignored", 5.0)

    assert events == ["connect", "clear_cache:stale", "disconnect:stale", "connect", "notify:fresh"]
    assert transport.mtu == 244
