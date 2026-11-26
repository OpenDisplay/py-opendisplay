"""The library's own SMP client, against a fake GATT link that runs a scripted SMP server."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from opendisplay.firmware import _cbor, smp

IMAGE = bytes(range(256)) * 40  # 10 KiB
DEVICE = SimpleNamespace(name="OD4064C9", address="AA:BB:CC:DD:EE:FF")
WRITE_SIZE = 244


def _frame(op: int, group: int, sequence: int, command: int, body: dict[str, Any]) -> bytes:
    payload = _cbor.encode(body)
    return (
        bytes([op, 0])
        + len(payload).to_bytes(2, "big")
        + group.to_bytes(2, "big")
        + bytes([sequence, command])
        + payload
    )


class _Server:
    """A scripted SMP server: answers what od_smp.cpp answers, with knobs for what goes wrong."""

    def __init__(self) -> None:
        self.requests: list[tuple[bytes, dict[str, Any]]] = []
        self.buf_size: int | None = 2048
        self.images: list[dict[str, Any]] = [{"slot": 0, "version": "3.0.2", "hash": b"\x01" * 32, "active": True}]
        self.received = bytearray()
        self.off = 0
        self.silent_on: Callable[[int, int, dict[str, Any]], bool] = lambda group, command, body: False
        self.refuse_upload_with: int | None = None
        self.refuse_state_write_with: int | None = None
        self.split: int | None = None
        self.stale_first = False
        self.stuck = False
        self.active = False  # an upload is held (od_smp.cpp: s_ota_part != NULL)

    def handle(self, frame: bytes) -> list[bytes]:
        header, body = frame[:8], _cbor.decode(frame[8:]) if len(frame) > 8 else {}
        self.requests.append((header, body))
        op, group, sequence, command = header[0] & 7, int.from_bytes(header[4:6], "big"), header[6], header[7]
        if self.silent_on(group, command, body):
            return []
        reply: dict[str, Any]
        if (group, command) == (0, 6):
            reply = {"rc": 8} if self.buf_size is None else {"buf_size": self.buf_size, "buf_count": 1}
        elif (group, command) == (1, 0) and op == 2 and self.refuse_state_write_with is not None:
            reply = {"rc": self.refuse_state_write_with}
        elif (group, command) == (1, 0):
            reply = {"images": self.images}
        elif (group, command) == (0, 5):
            reply = {}
        elif (group, command) == (1, 1):
            reply = self._upload(body)
        else:
            reply = {"rc": 8}
        frames = [_frame(op + 1, group, (sequence - 1) & 0xFF, command, {"off": 999})] if self.stale_first else []
        frames.append(_frame(op + 1, group, sequence, command, reply))
        data = b"".join(frames)
        size = self.split or len(data)
        return [data[i : i + size] for i in range(0, len(data), size)]

    def _upload(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.refuse_upload_with is not None:
            return {"rc": self.refuse_upload_with}
        if body["off"] == 0:
            self.active = True
        if body["off"] == 0 and not self.stuck:
            self.received.clear()
            self.off = 0
        if not self.active:
            return {"rc": 6}  # RC_BADSTATE: no upload held, as before Firmware_Unified #116 after a drop
        if body["off"] == self.off and not self.stuck:
            self.received.extend(body["data"])
            self.off += len(body["data"])
        return {"off": self.off}


class _Link:
    """Stands in for a BleakClientWithServiceCache: GATT writes in, notifications out."""

    def __init__(self, server: _Server, *, has_smp: bool = True, backend: Any = None) -> None:
        self.server = server
        self.has_smp = has_smp
        self.characteristic = SimpleNamespace(max_write_without_response_size=WRITE_SIZE)
        self.services = SimpleNamespace(get_characteristic=lambda uuid: self.characteristic if self.has_smp else None)
        self._backend = backend if backend is not None else object()
        self.mtu_size = 23
        self.writes: list[bytes] = []
        self.events: list[str] = []
        self._rx = bytearray()
        self._notify: Any = None

    async def start_notify(self, characteristic: Any, callback: Any) -> None:
        self.events.append("notify")
        self._notify = callback

    async def clear_cache(self) -> bool:
        self.events.append("clear_cache")
        return True

    async def disconnect(self) -> None:
        self.events.append("disconnect")

    async def write_gatt_char(self, characteristic: Any, data: bytes, response: bool) -> None:
        assert response is False  # the SMP characteristic offers write-without-response only
        assert len(data) <= WRITE_SIZE
        self.writes.append(bytes(data))
        self._rx.extend(data)
        while len(self._rx) >= 8 and len(self._rx) >= 8 + int.from_bytes(self._rx[2:4], "big"):
            want = 8 + int.from_bytes(self._rx[2:4], "big")
            frame, self._rx = bytes(self._rx[:want]), self._rx[want:]
            for chunk in self.server.handle(frame):
                asyncio.get_running_loop().call_soon(self._notify, characteristic, bytearray(chunk))


class _Links(list[_Link]):
    """The links opened, and the ones the next connects will get."""

    def __init__(self) -> None:
        super().__init__()
        self.queued: list[_Link] = []


@pytest.fixture
def server() -> _Server:
    return _Server()


@pytest.fixture
def links(monkeypatch: pytest.MonkeyPatch, server: _Server) -> _Links:
    import bleak_retry_connector

    made = _Links()

    async def establish_connection(cls: Any, ble_device: Any, name: str, **kwargs: Any) -> Any:
        assert ble_device is DEVICE  # the caller's BLEDevice: Home Assistant and proxies need that
        link = made.queued.pop(0) if made.queued else _Link(server)
        made.append(link)
        return link

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", establish_connection)
    return made


# ── the link ─────────────────────────────────────────────────────────────────


async def test_a_request_is_one_frame_in_mtu_sized_writes_without_response(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        links[0].writes.clear()
        await smp.confirm(client)

    frame = b"".join(links[0].writes)
    payload = _cbor.encode({"confirm": True})
    # op 2 (write), flags 0, length, group 1 (image), sequence 1 (0 read the parameters), command 0
    assert frame == bytes([2, 0]) + len(payload).to_bytes(2, "big") + b"\x00\x01" + bytes([1, 0]) + payload


async def test_a_long_request_is_split_into_writes_of_one_mtu(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        links[0].writes.clear()
        await smp.upload(client, IMAGE[:1500])

    first_request = links[0].writes[: (2048 + WRITE_SIZE - 1) // WRITE_SIZE]
    assert [len(w) for w in first_request[:-1]] == [WRITE_SIZE] * (len(first_request) - 1)


async def test_the_device_buffer_size_bounds_each_request(server: _Server, links: _Links) -> None:
    server.buf_size = 1500

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert (client.write_size, client.max_request) == (WRITE_SIZE, 1500)


async def test_a_server_without_parameters_gets_one_write_per_request(server: _Server, links: _Links) -> None:
    server.buf_size = None

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert client.max_request == WRITE_SIZE


async def test_a_reply_split_across_notifications_is_reassembled(server: _Server, links: _Links) -> None:
    server.split = 5

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        states = await smp.image_states(client)

    assert states[0].version == "3.0.2" and states[0].active


async def test_a_late_reply_to_another_request_is_ignored(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        server.stale_first = True
        states = await smp.image_states(client)

    assert states[0].slot == 0  # not the stale {"off": 999}


@pytest.mark.parametrize("reply", [{"rc": 6}, {"err": {"group": 1, "rc": 6}}])  # SMP v1 and v2 error shapes
async def test_an_error_code_is_a_refusal(server: _Server, links: _Links, reply: dict[str, Any]) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        server.handle = lambda frame: [_frame(3, 1, frame[6], 0, reply)]  # type: ignore[method-assign]
        with pytest.raises(smp.SmpRefusedError, match="bad state") as caught:
            await smp.confirm(client)

    assert caught.value.rc == 6


async def test_no_reply_is_a_stall(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        server.silent_on = lambda group, command, body: True
        with pytest.raises(smp.SmpStallError, match="No reply"):
            await client.request(0, 1, 0, {}, timeout=0.05)


async def test_a_dropped_link_is_a_stall_at_once(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        server.silent_on = lambda group, command, body: True
        asyncio.get_running_loop().call_later(0.02, client._on_disconnect, links[0])
        with pytest.raises(smp.SmpStallError, match="dropped"):
            await client.request(0, 1, 0, {}, timeout=5.0)


async def test_a_display_without_the_smp_service_is_explained(server: _Server, links: _Links) -> None:
    links.queued.extend([_Link(server, has_smp=False), _Link(server, has_smp=False)])

    with pytest.raises(smp.SmpError, match="firmware 3.x"):
        async with smp.session(DEVICE):  # type: ignore[arg-type]
            pass


async def test_a_stale_service_cache_is_cleared_once_and_the_link_reopened(server: _Server, links: _Links) -> None:
    # An ESPHome proxy kept the display's 2.x service table (no SMP service) after it moved to 3.0;
    # seen on the Bedroom proxy with the E1002. One clear_cache and a new link find it.
    stale = _Link(server, has_smp=False)
    links.queued.append(stale)

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert client.max_request == 2048

    assert stale.events == ["clear_cache", "disconnect"]
    assert links[1].events[0] == "notify"


async def test_bluez_reads_the_negotiated_mtu(server: _Server, links: _Links) -> None:
    class BleakClientBlueZDBus:
        async def _acquire_mtu(self) -> None:
            link.mtu_size = 247

    link = _Link(server, backend=BleakClientBlueZDBus())
    link.characteristic.max_write_without_response_size = 20  # BlueZ, before the MTU is read
    links.queued.append(link)

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert client.write_size == 244


# ── upload ───────────────────────────────────────────────────────────────────


def _uploads(server: _Server) -> list[tuple[bytes, dict[str, Any]]]:
    return [(h, b) for h, b in server.requests if h[4:6] == b"\x00\x01" and h[7] == 1]


async def test_an_upload_fills_each_request_and_reports_progress(server: _Server, links: _Links) -> None:
    progress: list[int] = []

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        await smp.upload(client, IMAGE, on_progress=lambda done, total: progress.append(done))

    assert bytes(server.received) == IMAGE
    uploads = _uploads(server)
    first = uploads[0][1]
    assert (first["off"], first["len"], first["image"], first["sha"]) == (
        0,
        len(IMAGE),
        0,
        hashlib.sha256(IMAGE).digest(),
    )
    assert all("len" not in b for _, b in uploads[1:])
    sizes = [8 + int.from_bytes(h[2:4], "big") for h, _ in uploads]
    assert max(sizes) <= 2048 and min(sizes[:-1]) >= 2048 - 16  # each request (but the last) nearly full
    assert progress[-1] == len(IMAGE) and progress == sorted(progress)


async def test_a_capped_session_keeps_each_request_within_its_writes(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE, burst_writes=4) as client:  # type: ignore[arg-type]
        await smp.upload(client, IMAGE)

    sizes = [8 + int.from_bytes(h[2:4], "big") for h, _ in _uploads(server)]
    assert max(sizes) <= 4 * WRITE_SIZE and min(sizes[:-1]) >= 4 * WRITE_SIZE - 16
    assert bytes(server.received) == IMAGE


async def test_a_reply_that_never_comes_is_a_stall_worth_retrying(
    server: _Server, links: _Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smp, "_REQUEST_TIMEOUT_S", 0.05)
    server.silent_on = lambda group, command, body: command == 1 and body.get("off", 0) > 0

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpStallError, match=r"Upload stopped at \d+ of 10240"):
            await smp.upload(client, IMAGE)


async def test_a_device_that_refuses_the_image_is_not_a_stall(server: _Server, links: _Links) -> None:
    server.refuse_upload_with = 9

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpTransferError, match="corrupt") as caught:
            await smp.upload(client, IMAGE)

    assert not isinstance(caught.value, smp.SmpStallError)


async def test_a_device_that_stops_advancing_is_a_stall(server: _Server, links: _Links) -> None:
    server.stuck = True  # answers every request with the same offset

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpStallError, match="stopped advancing"):
            await smp.upload(client, IMAGE)

    assert len(_uploads(server)) == 3


# ── image states, test boot, reset ───────────────────────────────────────────


async def test_test_and_reset_marks_the_uploaded_slot_by_the_hash_the_device_reports(
    server: _Server, links: _Links
) -> None:
    # The device names an image by its own hash (ESP32: esp_partition_get_sha256 of the slot;
    # MCUboot: the image hash), not the file's SHA-256 (od_smp.cpp handle_state_write).
    device_hash = b"\xab" * 32
    server.images = [{"slot": 0, "hash": b"\x01" * 32, "active": True}, {"slot": 1, "hash": device_hash}]

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert await smp.test_and_reset(client) == device_hash

    write, reset = server.requests[-2:]
    assert (write[0][0], write[0][7], write[1]) == (2, 0, {"hash": device_hash, "confirm": False})
    assert (reset[0][4:6], reset[0][7]) == (b"\x00\x00", 5)


async def test_a_reset_that_restarts_before_replying_is_not_an_error(
    server: _Server, links: _Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smp, "_RESET_TIMEOUT_S", 0.05)
    server.images.append({"slot": 1, "hash": b"\xab" * 32})
    server.silent_on = lambda group, command, body: (group, command) == (0, 5)

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert await smp.test_and_reset(client) == b"\xab" * 32


async def test_test_and_reset_without_an_uploaded_slot_refuses(server: _Server, links: _Links) -> None:
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpError, match="slot 1"):
            await smp.test_and_reset(client)

    assert not any(h[7] == 5 and h[4:6] == b"\x00\x00" for h, _ in server.requests)


async def test_the_running_image_refused_for_a_test_boot_is_reported_and_not_reset(
    server: _Server, links: _Links
) -> None:
    # MCUboot hashes the image alone, so a re-sent running image has the running hash, and Zephyr
    # refuses to mark the running image for a test boot (rc 1; seen on the EN05 2026-11-18).
    server.images = [{"slot": 0, "hash": b"\x01" * 32, "active": True}, {"slot": 1, "hash": b"\x01" * 32}]
    server.refuse_state_write_with = 1

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpAlreadyRunningError, match="already runs this image"):
            await smp.test_and_reset(client)

    assert not any(h[7] == 5 and h[4:6] == b"\x00\x00" for h, _ in server.requests)  # no reset


async def test_the_running_image_accepted_for_a_test_boot_is_reset_into(server: _Server, links: _Links) -> None:
    # ESP32 takes the same image again (a reinstall): 3.0.2 -> 3.0.2 on the E1002 ended UPDATED.
    server.images = [{"slot": 0, "hash": b"\x01" * 32, "active": True}, {"slot": 1, "hash": b"\x01" * 32}]

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        assert await smp.test_and_reset(client) == b"\x01" * 32

    assert any(h[7] == 5 and h[4:6] == b"\x00\x00" for h, _ in server.requests)


async def test_another_refusal_of_the_mark_stays_a_refusal(server: _Server, links: _Links) -> None:
    server.images.append({"slot": 1, "hash": b"\xab" * 32})
    server.refuse_state_write_with = 1

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpRefusedError) as info:
            await smp.test_and_reset(client)

    assert not isinstance(info.value, smp.SmpAlreadyRunningError)


async def test_image_states_reads_every_field(server: _Server, links: _Links) -> None:
    server.images = [
        {"slot": 0, "version": "3.0.2", "hash": b"\x01" * 32, "active": True, "confirmed": True, "bootable": True},
        {"slot": 1, "version": "3.0.3", "pending": True},
    ]

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        running, other = await smp.image_states(client)

    assert running == smp.ImageState(0, "3.0.2", b"\x01" * 32, True, True, False, True)
    assert other == smp.ImageState(1, "3.0.3", None, False, False, True, False)


async def test_an_upload_resumes_at_the_offset_the_device_still_holds(server: _Server, links: _Links) -> None:
    """Firmware_Unified #116 (in 3.1.1): an ESP32 keeps an upload across a link drop."""
    image = bytes(range(256)) * 40
    server.received, server.off, server.active = bytearray(image[:4000]), 4000, True

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        await smp.upload(client, image, start=4000)

    uploads = [body for (header, body) in server.requests if header[7] == 1]
    assert uploads[0]["off"] == 4000 and "len" not in uploads[0] and "sha" not in uploads[0]
    assert bytes(server.received) == image


async def test_a_resume_the_device_refuses_starts_over_from_zero(server: _Server, links: _Links) -> None:
    """Firmware without #116 dropped the upload with the link and answers a resume RC_BADSTATE."""
    image = bytes(range(256)) * 40

    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        await smp.upload(client, image, start=4000)

    uploads = [body for (header, body) in server.requests if header[7] == 1]
    assert uploads[0]["off"] == 4000
    assert uploads[1]["off"] == 0 and uploads[1]["len"] == len(image) and "sha" in uploads[1]
    assert bytes(server.received) == image


async def test_a_stall_reports_the_last_offset_the_device_confirmed(
    server: _Server, links: _Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smp, "_REQUEST_TIMEOUT_S", 0.05)
    image = bytes(range(256)) * 40
    confirmed: list[int] = []

    def silent(group: int, command: int, body: dict[str, Any]) -> bool:
        return group == 1 and command == 1 and body["off"] >= 2000

    server.silent_on = silent
    async with smp.session(DEVICE) as client:  # type: ignore[arg-type]
        with pytest.raises(smp.SmpStallError) as raised:
            await smp.upload(client, image, on_progress=lambda done, total: confirmed.append(done))

    assert raised.value.offset == confirmed[-1] > 0
