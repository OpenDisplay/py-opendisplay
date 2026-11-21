"""``update()``: the six steps around a transfer, driven with fake transfers and a scripted device."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from opendisplay import redraw
from opendisplay.exceptions import BLEConnectionError, OTAError
from opendisplay.firmware import UpdateOutcome, UpdateRefusedError, smp, update, updater
from opendisplay.models.enums import ICType
from opendisplay.models.ident import FirmwareTarget
from opendisplay.sec2.wire import SecState

ADDRESS = "AA:BB:CC:DD:EE:FF"
ESP32_S3_IMAGE = b"\xe9" + bytes(11) + (9).to_bytes(2, "little") + bytes(200)
ESP32_C3_IMAGE = b"\xe9" + bytes(11) + (5).to_bytes(2, "little") + bytes(200)
MCUBOOT_IMAGE = bytes.fromhex("3db8f396") + bytes(200)
GBL_IMAGE = bytes.fromhex("eb17a603") + bytes(200)
OLD, NEW = b"\x0a" * 32, b"\x0b" * 32


def _fw(version: str) -> dict[str, Any]:
    major, minor, patch = (int(p) for p in version.split("."))
    return {"major": major, "minor": minor, "patch": patch, "sha": ""}


class _Legacy:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.mac_address = ADDRESS
        self._ble_device = None
        self._device_name = None

    async def trigger_dfu_bootloader(self) -> None:
        self.calls.append("enter_dfu")

    async def read_ident(self) -> None:
        self.calls.append("read_ident")

    def _redraw_keys(self) -> tuple[str, ...]:
        return (ADDRESS,)


class _Device:
    """Stands in for ``Device``: a profile, a security state, and a scripted reconnect."""

    def __init__(
        self,
        *,
        target: FirmwareTarget | int = FirmwareTarget.ESP32,
        version: str = "3.0.1",
        state: SecState = SecState.UNCLAIMED,
        claim_mode: bool = False,
        ic_type: int = ICType.ESP32_S3,
        versions_after: list[str] | None = None,
        reconnect_fails: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.legacy = _Legacy(self.calls)
        self.profile = SimpleNamespace(target=target, firmware=_fw(version))
        self.security = SimpleNamespace(status=SimpleNamespace(state=state, claim_mode=claim_mode))
        self.config = SimpleNamespace(cached=SimpleNamespace(system=SimpleNamespace(ic_type=ic_type)))
        self._versions_after = list(versions_after or [])
        self._reconnect_fails = reconnect_fails
        self.connected = True

    async def __aenter__(self) -> _Device:
        self.calls.append("connect")
        if self._reconnect_fails:
            raise BLEConnectionError("not found")
        if self._versions_after:
            self.profile = SimpleNamespace(target=self.profile.target, firmware=_fw(self._versions_after.pop(0)))
        self.connected = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.calls.append("disconnect")
        self.connected = False


def _slot(slot: int, version: str, hash_: bytes, *, active: bool, confirmed: bool) -> Any:
    return SimpleNamespace(slot=slot, version=version, hash=hash_, active=active, confirmed=confirmed, pending=False)


class _Smp:
    """Scripts the SMP functions ``update()`` calls: slot lists per session, an upload, a reset."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, device: _Device, states: list[list[Any]]) -> None:
        self.calls = device.calls
        self.states = states
        self.upload_errors: list[Exception] = []
        self.reset_errors: list[Exception] = []
        self.session_errors: list[Exception] = []

        @asynccontextmanager
        async def session(ble_device: Any, *, burst_writes: int | None = None) -> AsyncIterator[str]:
            self.calls.append(f"smp_session:{ble_device.name}" + (f":burst{burst_writes}" if burst_writes else ""))
            if self.session_errors:
                raise self.session_errors.pop(0)
            yield "client"

        async def image_states(client: Any) -> list[Any]:
            return self.states.pop(0)

        async def upload(client: Any, image: bytes, *, on_progress: Any = None) -> None:
            self.calls.append("upload")
            if self.upload_errors:
                raise self.upload_errors.pop(0)
            if on_progress is not None:
                on_progress(len(image), len(image))

        async def test_and_reset(client: Any) -> bytes:
            self.calls.append("test_and_reset")
            if self.reset_errors:
                raise self.reset_errors.pop(0)
            return NEW

        monkeypatch.setattr(smp, "session", session)
        monkeypatch.setattr(smp, "image_states", image_states)
        monkeypatch.setattr(smp, "upload", upload)
        monkeypatch.setattr(smp, "test_and_reset", test_and_reset)


async def _find_app(address: str) -> Any:
    return SimpleNamespace(name="OD4064C9", address=address)


def _before() -> list[Any]:
    return [_slot(0, "3.0.1", OLD, active=True, confirmed=True)]


# ── SMP: ESP32, nRF52840, nRF54 ──────────────────────────────────────────────


async def test_an_unclaimed_esp32_is_updated_confirmed_and_left_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device()
    after = [_slot(0, "3.0.2", NEW, active=True, confirmed=True), _slot(1, "3.0.1", OLD, active=False, confirmed=True)]
    _Smp(monkeypatch, device, [_before(), after])
    progress: list[tuple[int, int]] = []

    result = await update(
        device, ESP32_S3_IMAGE, find_device=_find_app, on_progress=lambda d, t: progress.append((d, t))
    )  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED
    assert (result.version_before, result.version_after) == ("3.0.1", "3.0.2")
    assert device.calls == [
        "disconnect",  # one central holds one link to a display: SMP gets its own
        "smp_session:OD4064C9",
        "upload",
        "test_and_reset",
        "connect",
        "read_ident",  # step 5: the first command confirms the new image
        "disconnect",
        "smp_session:OD4064C9",
        "connect",
    ]
    assert "enter_dfu" not in device.calls  # unclaimed, normal mode: nothing to unlock
    assert progress == [(len(ESP32_S3_IMAGE), len(ESP32_S3_IMAGE))]
    assert device.connected


async def test_the_reboot_is_noted_as_a_redraw_so_the_reconnect_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    # The display was not seen for 1.5-2 min after the reset on the E1002 (reboot + Spectra redraw).
    device = _Device()
    after = [_slot(0, "3.0.2", NEW, active=True, confirmed=True)]
    _Smp(monkeypatch, device, [_before(), after])
    noted: list[tuple[tuple[str | None, ...], float]] = []
    monkeypatch.setattr(redraw, "mark_redrawing", lambda *k, allowance: noted.append((k, allowance)))

    await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert noted and noted[0][0] == (ADDRESS,) and noted[0][1] >= 180


async def test_a_claimed_device_is_unlocked_with_a_sealed_enter_dfu_first(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device(state=SecState.CLAIMED)
    _Smp(monkeypatch, device, [_before(), [_slot(0, "3.0.2", NEW, active=True, confirmed=True)]])

    await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert device.calls[:2] == ["enter_dfu", "disconnect"]  # sent on the open (sealed) session


async def test_a_claimed_device_is_unlocked_again_after_the_reboot_to_read_its_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reboot ends the unlock: a claimed ESP32 then answers SMP with rc 11 (denied), a claimed
    # nRF hides the SMP service (od_smp.cpp s_unlocked, od_smp_lock.h).
    device = _Device(state=SecState.CLAIMED)
    _Smp(monkeypatch, device, [_before(), [_slot(0, "3.0.2", NEW, active=True, confirmed=True)]])

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED
    after_reboot = device.calls[device.calls.index("read_ident") :]
    assert after_reboot[:3] == ["read_ident", "enter_dfu", "disconnect"]  # confirm, then unlock


async def test_a_failure_marking_the_image_is_not_updated_and_the_device_is_reconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _Device()
    fake = _Smp(monkeypatch, device, [_before()])
    fake.reset_errors = [smp.SmpRefusedError("group 1 command 0", 6)]

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.NOT_UPDATED
    assert "rc 6" in result.detail and "3.0.1" in result.detail
    assert device.calls.count("upload") == 1  # a refusal is not a stall: no second upload
    assert device.calls[-1] == "connect" and device.connected


async def test_an_nrf_image_stamped_0_0_0_reports_the_version_read_ident_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 3.0.2 nRF release was signed 0.0.0+0 (fixed upstream in build.sh since); READ_IDENT is right.
    device = _Device(target=FirmwareTarget.NRF52840, version="3.0.2", versions_after=["3.0.3"])
    before = [_slot(0, "0.0.0", OLD, active=True, confirmed=True)]
    after = [_slot(0, "0.0.0", NEW, active=True, confirmed=True)]
    _Smp(monkeypatch, device, [before, after])

    result = await update(device, MCUBOOT_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED
    assert (result.version_before, result.version_after) == ("3.0.2", "3.0.3")


async def test_the_running_image_sent_again_is_not_updated_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device(target=FirmwareTarget.NRF52840, version="3.0.2")
    fake = _Smp(monkeypatch, device, [[_slot(0, "3.0.2", OLD, active=True, confirmed=True)]])
    fake.reset_errors = [smp.SmpAlreadyRunningError("The display already runs this image")]

    result = await update(device, MCUBOOT_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.NOT_UPDATED
    assert result.detail.startswith("The display already runs this image")
    assert device.calls.count("upload") == 1 and device.connected


async def test_a_display_without_an_smp_link_is_not_updated_and_the_device_is_reconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _Device()
    fake = _Smp(monkeypatch, device, [])
    fake.session_errors = [smp.SmpError("The display has no SMP service; updates over Bluetooth need firmware 3.x")]

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.NOT_UPDATED
    assert (result.version_before, result.version_after) == ("3.0.1", "3.0.1")
    assert "no SMP service" in result.detail
    assert "upload" not in device.calls
    assert device.calls[-1] == "connect" and device.connected


async def test_an_interrupted_transfer_is_not_updated_and_the_device_is_reconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _Device()
    fake = _Smp(monkeypatch, device, [_before()])
    fake.upload_errors = [
        smp.SmpStallError("Upload stopped at 375593 of 1229968 bytes: link lost"),
        smp.SmpStallError("Upload stopped at 202400 of 1229968 bytes: link lost"),
    ]

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.NOT_UPDATED
    assert (result.version_before, result.version_after) == ("3.0.1", "3.0.1")
    assert "202400" in result.detail  # the last attempt's
    assert device.calls.count("upload") == 2  # one retry, not more
    assert "test_and_reset" not in device.calls  # no reset: the old image keeps running
    assert device.calls[-1] == "connect" and device.connected


async def test_a_stalled_upload_starts_over_with_shorter_bursts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Through the Bedroom proxy, 8 KB requests (33 unacknowledged writes) stalled; 8 writes did not.
    # An ESP32 aborts an upload when its link drops, so the retry starts from 0 on a new link.
    device = _Device()
    fake = _Smp(monkeypatch, device, [_before(), [_slot(0, "3.0.2", NEW, active=True, confirmed=True)]])
    fake.upload_errors = [smp.SmpStallError("Upload stopped at 375593 of 1229968 bytes: no reply")]

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED
    assert device.calls[:6] == [
        "disconnect",
        "smp_session:OD4064C9",
        "upload",
        "smp_session:OD4064C9:burst8",
        "upload",
        "test_and_reset",
    ]


async def test_an_image_the_device_refuses_is_not_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device()
    fake = _Smp(monkeypatch, device, [_before()])
    fake.upload_errors = [smp.SmpTransferError("The device refused the image: rc 9 (corrupt)")]

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.NOT_UPDATED and "corrupt" in result.detail
    assert device.calls.count("upload") == 1


async def test_a_device_back_on_its_old_image_is_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device()
    after = [_slot(0, "3.0.1", OLD, active=True, confirmed=True), _slot(1, "3.0.2", NEW, active=False, confirmed=False)]
    _Smp(monkeypatch, device, [_before(), after])

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.ROLLED_BACK
    assert (result.version_before, result.version_after) == ("3.0.1", "3.0.1")


async def test_a_device_that_does_not_come_back_says_what_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device(reconnect_fails=True)
    _Smp(monkeypatch, device, [_before()])

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.NOT_UPDATED
    assert result.version_after is None
    assert "confirm" in result.detail  # its first command will still confirm the new image


async def test_the_update_can_be_run_twice_with_the_same_image(monkeypatch: pytest.MonkeyPatch) -> None:
    # The E1002 test: the image it already runs. Old and new hash are the same; that is success.
    device = _Device(version="3.0.2")
    same = [_slot(0, "3.0.2", NEW, active=True, confirmed=True)]
    _Smp(monkeypatch, device, [same, list(same)])

    result = await update(device, ESP32_S3_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED


# ── Refusals (step 1) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "image", "message"),
    [
        ({"claim_mode": True}, ESP32_S3_IMAGE, "claim mode"),
        ({"version": "2.24.0"}, ESP32_S3_IMAGE, "first move to 3.0"),
        ({}, MCUBOOT_IMAGE, "not an ESP32 image"),
        ({}, ESP32_C3_IMAGE, "ESP32-C3"),
        ({"target": FirmwareTarget.NRF52840, "ic_type": ICType.NRF52840}, ESP32_S3_IMAGE, "MCUboot"),
        ({"target": FirmwareTarget.EFR32BG22, "ic_type": ICType.EFR32BG22}, ESP32_S3_IMAGE, ".gbl"),
        ({"target": FirmwareTarget.NRF52811, "ic_type": ICType.NRF52811}, MCUBOOT_IMAGE, "not supported"),
        ({"target": FirmwareTarget.TELINK}, MCUBOOT_IMAGE, "not supported"),
    ],
)
async def test_an_update_the_device_cannot_take_is_refused_before_anything_is_sent(
    kwargs: dict[str, Any], image: bytes, message: str
) -> None:
    device = _Device(**kwargs)

    with pytest.raises(UpdateRefusedError, match=message):
        await update(device, image, find_device=_find_app)  # type: ignore[arg-type]

    assert device.calls == []


async def test_an_nrf_device_takes_an_mcuboot_image_over_smp(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _Device(target=FirmwareTarget.NRF54, ic_type=0)
    _Smp(monkeypatch, device, [_before(), [_slot(0, "3.0.2", NEW, active=True, confirmed=True)]])

    result = await update(device, MCUBOOT_IMAGE, find_device=_find_app)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED and "upload" in device.calls


# ── BG22: the AppLoader ──────────────────────────────────────────────────────


def _bg22(**kwargs: Any) -> _Device:
    return _Device(target=FirmwareTarget.EFR32BG22, ic_type=ICType.EFR32BG22, **kwargs)


def _silabs(monkeypatch: pytest.MonkeyPatch, device: _Device, error: Exception | None = None) -> list[Any]:
    sent: list[Any] = []

    async def perform_silabs_ota(gbl: bytes, ble_device: Any, on_progress: Any = None, **_: Any) -> None:
        device.calls.append(f"silabs_ota:{ble_device.name}")
        sent.append(gbl)
        if error is not None:
            raise error
        if on_progress is not None:
            on_progress(50.0)
            on_progress(100.0)

    monkeypatch.setattr(updater, "perform_silabs_ota", perform_silabs_ota)
    return sent


async def _find_apploader(address: str) -> Any:
    return SimpleNamespace(name="OTA", address=address)


async def test_an_unclaimed_bg22_still_needs_enter_dfu_to_reach_its_apploader(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _bg22(versions_after=["3.0.2"])
    sent = _silabs(monkeypatch, device)
    progress: list[tuple[int, int]] = []

    result = await update(
        device, GBL_IMAGE, find_device=_find_apploader, on_progress=lambda d, t: progress.append((d, t))
    )  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.UPDATED
    assert (result.version_before, result.version_after) == ("3.0.1", "3.0.2")
    assert device.calls == ["enter_dfu", "disconnect", "silabs_ota:OTA", "connect", "read_ident"]
    assert sent == [GBL_IMAGE]
    assert progress == [(len(GBL_IMAGE) // 2, len(GBL_IMAGE)), (len(GBL_IMAGE), len(GBL_IMAGE))]


async def test_a_bg22_that_reappears_as_its_apploader_is_waiting_for_an_image(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _bg22(reconnect_fails=True)
    _silabs(monkeypatch, device)

    result = await update(device, GBL_IMAGE, find_device=_find_apploader)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.WAITING_IN_BOOTLOADER
    assert ".gbl" in result.detail  # the next action: send an image again


async def test_a_failed_bg22_transfer_leaves_it_waiting_in_the_apploader(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _bg22()
    _silabs(monkeypatch, device, error=OTAError("link lost at 40%"))

    result = await update(device, GBL_IMAGE, find_device=_find_apploader)  # type: ignore[arg-type]

    assert result.outcome is UpdateOutcome.WAITING_IN_BOOTLOADER
    assert "link lost" in result.detail and result.version_after is None


# ── Device.firmware ──────────────────────────────────────────────────────────


def test_can_roll_back_is_read_from_the_target() -> None:
    from opendisplay.device.api import FirmwareView

    assert FirmwareView(_Device()).can_roll_back is True  # type: ignore[arg-type]
    assert FirmwareView(_bg22()).can_roll_back is False  # type: ignore[arg-type]


async def test_enable_ota_unlocks_only_where_the_firmware_needs_it() -> None:
    from opendisplay.device.api import FirmwareView

    unclaimed, claimed, bg22 = _Device(), _Device(state=SecState.CLAIMED), _bg22()
    for device in (unclaimed, claimed, bg22):
        await FirmwareView(device).enable_ota()  # type: ignore[arg-type]

    assert (unclaimed.calls, claimed.calls, bg22.calls) == ([], ["enter_dfu"], ["enter_dfu"])


async def test_enable_ota_refuses_in_claim_mode() -> None:
    from opendisplay.device.api import FirmwareView

    with pytest.raises(UpdateRefusedError, match="claim mode"):
        await FirmwareView(_Device(claim_mode=True)).enable_ota()  # type: ignore[arg-type]
