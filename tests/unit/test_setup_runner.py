"""The setup runner: one scripted run per starting state, each saved and restored mid-way.

A ``_World`` is the display across connections (firmware, security state, stored config); every
``run_until_input`` opens a fresh ``_Dev`` on it, as the runner re-reads the device each time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_profile import _config, _display

from opendisplay.firmware.releases import ReleaseManifest
from opendisplay.models.config import GlobalConfig, SecurityConfig
from opendisplay.models.enums import ColorScheme, ICType
from opendisplay.models.ident import FirmwareTarget
from opendisplay.sec2.errors import Sec2CodeMismatchError
from opendisplay.sec2.wire import SecState, SecStatus
from opendisplay.setup import Step
from opendisplay.setup import requests as rq
from opendisplay.setup import runner as runner_mod
from opendisplay.setup.runner import SetupError, SetupRun

KEY = bytes(range(1, 17))
MANIFEST = ReleaseManifest.parse(
    (Path(__file__).parents[1] / "fixtures" / "firmware" / "manifest-3.0.2.json").read_bytes()
)


def _keyed(ic_type: int = ICType.ESP32_S3) -> GlobalConfig:
    security = SecurityConfig(
        encryption_enabled=1,
        encryption_key=KEY,
        session_timeout_seconds=0,
        flags=0,
        reset_pin=0xFF,
        reserved=bytes(43),
    )
    return _config(ic_type=ic_type, displays=[_display(panel_ic_type=0x0037)], security_config=security)


@dataclasses.dataclass
class _World:  # pylint: disable=too-many-instance-attributes
    major: int = 3
    target: int = FirmwareTarget.ESP32
    state: SecState = SecState.UNCLAIMED
    claim_mode: bool = False
    fresh: bool = False
    key_legacy: bool = False
    config: GlobalConfig | None = None
    needs_migration: bool = False
    code: str = "123456"
    calls: list[str] = dataclasses.field(default_factory=list)
    on_wired_install: Any = None  # what the USB install leaves behind

    def status(self) -> SecStatus | None:
        if self.major < 3:
            return None
        flags = (0x01 if self.claim_mode else 0) | (0x20 if self.fresh else 0)
        return SecStatus(
            2,
            self.state,
            flags,
            300 if self.claim_mode else 0,
            bytes(8),
            3600,
            (0x04 if self.key_legacy else 0) | (0x01 if self.config else 0),
            3,
        )


class _Flow:
    def __init__(self, world: _World) -> None:
        self.world = world

    async def begin(self) -> None:
        self.world.calls.append("claim_begin")

    async def exchange_nonces(self) -> Any:
        return SimpleNamespace(has_panel=self.world.config is not None)

    async def confirm(self, typed: str) -> None:
        if "".join(typed.split()) != self.world.code:  # as kdf.normalize_typed_code ignores spaces
            await self.abort()  # as ClaimFlow.confirm does
            raise Sec2CodeMismatchError()
        self.world.calls.append("claim_confirm")
        self.world.state, self.world.claim_mode, self.world.fresh = SecState.CLAIMED, False, False

    async def abort(self) -> None:
        self.world.calls.append("claim_abort")


class _Dev:
    def __init__(self, world: _World) -> None:
        w = self.world = world
        self.profile = SimpleNamespace(
            firmware={"major": w.major, "minor": 0, "patch": 2, "sha": ""},
            ident=None,
            target=w.target,
            configured=w.config is not None,
            needs_migration=w.needs_migration,
            color_scheme=ColorScheme.BWRY,
            width=800,
            height=480,
        )

        async def use_without_key() -> None:
            w.calls.append("use_without_key")
            w.claim_mode = w.fresh = False

        async def claim() -> _Flow:
            return _Flow(w)

        async def rotate_key() -> bytes:
            w.calls.append("rotate_key")
            w.key_legacy = False
            return bytes(16)

        async def read() -> GlobalConfig | None:
            return w.config

        async def write(config: GlobalConfig) -> None:
            w.calls.append("config_write")
            w.config, w.needs_migration = config, False

        async def trigger_dfu_bootloader() -> None:
            w.calls.append("enter_dfu")

        async def read_ident() -> None:
            w.calls.append("read_ident")

        async def show(image: Any, **kw: Any) -> None:
            w.calls.append(f"show:{image.size[0]}x{image.size[1]}")

        self.security = SimpleNamespace(
            status=w.status(),
            sec2_session_open=w.state == SecState.CLAIMED,
            use_without_key=use_without_key,
            claim=claim,
            rotate_key=rotate_key,
        )
        self.config = SimpleNamespace(cached=w.config, read=read, write=write)
        self.legacy = SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:FF",
            device_name="OD123456",
            trigger_dfu_bootloader=trigger_dfu_bootloader,
            read_ident=read_ident,
            _redraw_keys=lambda: ("AA:BB:CC:DD:EE:FF",),
        )
        self.display = SimpleNamespace(show=show)


def _factory(world: _World) -> Any:
    @asynccontextmanager
    async def open_device() -> AsyncIterator[_Dev]:
        world.calls.append("connect")
        yield _Dev(world)
        world.calls.append("disconnect")

    return open_device


@pytest.fixture(autouse=True)
def _migration(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """plan_migration as a script: needed when the device needs it, equivalent data for 0x0037."""
    seen: list[Any] = []

    def plan(config: Any, profile: Any, db: Any, *, panel_ic_type: int | None = None) -> Any:
        seen.append(panel_ic_type)
        _DBS_USED.append(db.source)
        needed = config is not None and (profile.needs_migration or not profile.configured)
        blocked = config is None or not config.displays
        return SimpleNamespace(
            needed=needed or blocked,
            blocked=blocked,
            reason="no panel" if blocked else "Panel EP397YR_800x480 (0x0037)",
            needs_confirmation=needed,
            entry=SimpleNamespace(name="EP397YR_800x480", panel_ic_type=panel_ic_type or 0x0037),
            panel_ic_type=panel_ic_type or 0x0037,
        )

    monkeypatch.setattr(runner_mod, "plan_migration", plan)
    monkeypatch.setattr(runner_mod, "apply_migration", lambda config, plan: config)
    monkeypatch.setattr(runner_mod.PanelDatabase, "load", classmethod(lambda cls, path=None: _DB_BUNDLED))

    async def for_release(cls: Any, manifest: Any, fetch: Any) -> Any:
        return SimpleNamespace(source=f"release {manifest.version}")

    monkeypatch.setattr(runner_mod.PanelDatabase, "for_release", classmethod(for_release))
    return seen


_DB_BUNDLED = SimpleNamespace(source="bundled")


_DBS_USED: list[str] = []


def _restart(run: SetupRun, **kw: Any) -> SetupRun:
    """Save and restore, as Home Assistant does across a restart."""
    data = run.to_json()
    json.loads(data)  # plain JSON
    return SetupRun.from_json(
        data,
        backup_sink=kw.get("sink", lambda b: None),
        manifest=MANIFEST,
        fetch_image=kw.get("fetch"),
        find_device=kw.get("find"),
    )


# ── 3.0 devices ──────────────────────────────────────────────────────────────


async def test_a_virgin_device_is_configured_checked_and_claimed() -> None:
    world = _World(claim_mode=True, fresh=True)
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None, manifest=MANIFEST)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfig)  # no config on the device and no backup
    run.answer(req, _config(displays=[_display(panel_ic_type=0x0037)]))

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "untested_panel"
    run = _restart(run)
    run.answer(run.pending, True)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "panel_ok"
    # Claim mode refuses every draw: the check is the device's own SETUP MODE screen.
    assert "config_write" in world.calls and not any(c.startswith("show:") for c in world.calls)
    assert "SETUP MODE" in req.detail and "OD123456" in req.detail
    run.answer(req, True)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedClaimCode) and req.has_panel
    run.answer(req, "123456")

    assert await run.run_until_input(_factory(world)) is None
    assert run.finished and world.state is SecState.CLAIMED
    assert world.calls.count("claim_confirm") == 1


async def test_a_claim_code_is_confirmed_on_the_link_that_asked_for_it() -> None:
    world = _World(claim_mode=True, config=_keyed())  # GESTURE, configured
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedClaimCode)
    connects = world.calls.count("connect")
    assert world.calls[-1] != "disconnect"  # held open across the request
    run.answer(req, "123456")
    await run.run_until_input(_factory(world))

    assert world.calls.index("claim_confirm") < world.calls.index("disconnect", world.calls.index("claim_begin"))
    assert world.calls.count("connect") >= connects


async def test_a_wrong_claim_code_is_asked_again() -> None:
    world = _World(claim_mode=True, config=_keyed())
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))
    run.answer(req, "000000")
    again = await run.run_until_input(_factory(world))

    assert isinstance(again, rq.NeedClaimCode) and again.retry
    assert "claim_abort" in world.calls and world.state is SecState.UNCLAIMED


async def test_an_open_device_asks_for_the_key_choice_then_needs_the_gesture() -> None:
    world = _World(config=_keyed())
    run = SetupRun.start(want_key=None, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedKeyChoice)
    run.answer(req, True)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedGesture) and req.gesture == "claim_mode"
    world.claim_mode = True  # the user power-cycles twice
    run.answer(req, None)

    assert isinstance(await run.run_until_input(_factory(world)), rq.NeedClaimCode)


async def test_claim_mode_without_a_key_is_left_and_the_device_configured() -> None:
    # The EN05 on 2026-11-18: fresh 3.0, no config, in claim mode, owner wants no key.
    world = _World(claim_mode=True, fresh=True)
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))

    assert world.calls.count("use_without_key") == 1 and not world.claim_mode
    assert isinstance(req, rq.NeedConfig)  # no config, no backup: the caller supplies one


async def test_an_undecided_run_in_claim_mode_checks_the_setup_screen_and_draws_nothing() -> None:
    # The EE02 on 2026-11-26: a fresh 3.1.1 device refused the test pattern (NACK 0x70).
    world = _World(claim_mode=True, fresh=True)
    run = SetupRun.start(want_key=None, backup_sink=lambda b: None, manifest=MANIFEST)

    req = await run.run_until_input(_factory(world))
    run.answer(req, _config(displays=[_display(panel_ic_type=0x0037)]))
    req = await run.run_until_input(_factory(world))
    run.answer(req, True)  # untested panel

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "panel_ok"
    assert "SETUP MODE" in req.detail
    assert not any(c.startswith("show:") for c in world.calls)


async def test_a_config_the_run_wrote_is_checked_on_the_panel_also_without_a_key() -> None:
    # The E1002 on 2026-11-18: once configured, the device no longer plans a panel check itself.
    world = _World(claim_mode=True, fresh=True)
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfig)
    run.answer(req, _config(displays=[_display(panel_ic_type=0x0037)]))
    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "untested_panel"
    run.answer(req, True)

    req = await run.run_until_input(_factory(world))
    assert "config_write" in world.calls
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "panel_ok"
    run.answer(req, False)  # not yet: the user stops, and goes on later
    assert await run.run_until_input(_factory(world)) is None and run.stopped == "panel_ok"

    run = _restart(run)  # the owed check survives a save
    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "panel_ok"
    run.answer(req, True)

    assert await run.run_until_input(_factory(world)) is None
    assert run.finished and world.calls.count("show:800x480") == 2


async def test_each_step_is_announced_before_it_runs() -> None:
    world = _World(claim_mode=True, fresh=True)
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None)
    seen: list[tuple[Step, int]] = []
    run.on_step = lambda step: seen.append((step, world.calls.count("use_without_key")))

    await run.run_until_input(_factory(world))

    assert seen[0] == (Step.USE_WITHOUT_KEY, 0)  # announced, then performed
    assert [s for s, _ in seen] == [Step.USE_WITHOUT_KEY, Step.CONFIGURE_DISPLAY]


async def test_a_claimed_device_with_a_2x_key_is_rotated() -> None:
    world = _World(state=SecState.CLAIMED, key_legacy=True, config=_keyed())
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None)

    assert await run.run_until_input(_factory(world)) is None
    assert run.finished and world.calls.count("rotate_key") == 1


async def test_a_locked_device_finishes_with_the_factory_reset_note() -> None:
    world = _World(state=SecState.LOCKED, config=_keyed())
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None)

    assert await run.run_until_input(_factory(world)) is None
    assert run.finished and "factory_reset" in run.notes


async def test_a_declined_panel_check_stops_the_run_and_asks_again_on_resume() -> None:
    world = _World(claim_mode=True, fresh=True, config=_keyed())  # FRESH: verify, claim
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "panel_ok"
    run.answer(req, False)

    assert await run.run_until_input(_factory(world)) is None
    assert not run.finished and run.stopped == "panel_ok"
    run = _restart(run)
    again = await run.run_until_input(_factory(world))
    assert isinstance(again, rq.NeedConfirmation) and again.reason == "panel_ok"


# ── from 2.x ─────────────────────────────────────────────────────────────────


async def test_a_2x_esp32_is_backed_up_then_asked_for_a_wired_install_and_resumed_on_3_0() -> None:
    world = _World(major=2, target=FirmwareTarget.ESP32, config=_keyed())
    sunk: list[bytes] = []
    run = SetupRun.start(want_key=False, backup_sink=sunk.append, manifest=MANIFEST)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedBoardChoice)  # several ESP32-S3 boards
    assert "s3-n32r8-extuart" in req.candidates
    run.answer(req, "s3-n32r8-extuart")

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedWiredInstall)
    assert req.files == ("opendisplay-s3-n32r8-extuart-merged.bin",) and req.erases_config
    assert req.url.startswith("https://opendisplay.org/firmware/install")

    # The backup went to the sink with its key; the saved run holds none.
    assert len(sunk) == 1 and KEY.hex() in json.dumps(json.loads(sunk[0]))
    saved = run.to_json()
    assert KEY.hex() not in saved and KEY.hex().upper() not in saved

    run = _restart(run, sink=sunk.append)
    world.major, world.claim_mode, world.fresh, world.config = 3, True, True, None  # flashed, config gone
    run.answer(run.pending, None)

    req = await run.run_until_input(_factory(world))
    assert "use_without_key" in world.calls
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "untested_panel"  # from the backup
    assert len(sunk) == 1  # not backed up twice


async def test_a_saved_wired_install_request_is_not_repeated_once_the_device_runs_3_0() -> None:
    world = _World(major=2, config=_keyed())
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None, manifest=MANIFEST)
    run.answer(await run.run_until_input(_factory(world)), "s3-n32r8-extuart")
    assert isinstance(await run.run_until_input(_factory(world)), rq.NeedWiredInstall)

    run = _restart(run)
    world.major, world.config = 3, _keyed()  # updated meanwhile, config kept, open
    req = await run.run_until_input(_factory(world))  # the request is not answered

    assert not isinstance(req, rq.NeedWiredInstall)


async def test_a_2x_bg22_is_installed_over_bluetooth_after_the_no_rollback_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(major=2, target=FirmwareTarget.EFR32BG22, config=_keyed(ICType.EFR32BG22))
    sent: list[tuple[int, Any]] = []

    async def fake_ota(image: bytes, ble_device: Any, progress: Any) -> None:
        sent.append((len(image), ble_device))
        world.major, world.needs_migration = 3, True  # BG22 keeps its config; it needs drive data

    monkeypatch.setattr(runner_mod, "perform_silabs_ota", fake_ota)
    monkeypatch.setattr(runner_mod.redraw, "mark_redrawing", lambda *k, allowance: None)
    fetched: list[str] = []

    image = b"\xeb\x17\xa6\x03" + bytes(60)

    async def fetch(name: str) -> bytes:
        fetched.append(name)
        return image

    async def find(address: str) -> Any:
        return SimpleNamespace(address=address, name="OTA")

    run = SetupRun.start(
        want_key=False, backup_sink=lambda b: None, manifest=_manifest_with(image), fetch_image=fetch, find_device=find
    )
    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "no_rollback"
    assert not sent
    run.answer(req, True)

    req = await run.run_until_input(_factory(world))
    assert fetched == ["opendisplay-bg22-ota.gbl"] and len(sent) == 1
    assert "enter_dfu" in world.calls
    assert isinstance(req, rq.NeedConfirmation) and req.reason == "untested_panel"


def _manifest_with(image: bytes, name: str = "opendisplay-bg22-ota.gbl") -> ReleaseManifest:
    """The 3.0.2 manifest with ``name`` standing for ``image`` (size and SHA-256)."""
    digest = hashlib.sha256(image).hexdigest()
    artefacts = tuple(
        dataclasses.replace(a, size=len(image), sha256=digest) if a.file == name else a for a in MANIFEST.artefacts
    )
    return dataclasses.replace(MANIFEST, artefacts=artefacts)


async def test_an_image_that_does_not_match_the_manifest_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(major=2, target=FirmwareTarget.EFR32BG22, config=_keyed(ICType.EFR32BG22))
    sent: list[bytes] = []

    async def fake_ota(image: bytes, ble_device: Any, progress: Any) -> None:
        sent.append(image)

    async def fetch(name: str) -> bytes:
        return b"not the release"

    async def find(address: str) -> Any:
        return SimpleNamespace(address=address)

    monkeypatch.setattr(runner_mod, "perform_silabs_ota", fake_ota)
    run = SetupRun.start(
        want_key=False, backup_sink=lambda b: None, manifest=MANIFEST, fetch_image=fetch, find_device=find
    )
    run.answer(await run.run_until_input(_factory(world)), True)  # no_rollback

    with pytest.raises(SetupError, match="SHA-256"):
        await run.run_until_input(_factory(world))
    assert not sent and "enter_dfu" not in world.calls


async def test_a_2x_device_without_a_manifest_is_an_error() -> None:
    world = _World(major=2, config=_keyed())
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None)

    with pytest.raises(SetupError, match="manifest"):
        await run.run_until_input(_factory(world))


async def test_a_claimed_device_that_lost_its_config_is_configured_from_the_backup_without_its_0x27() -> None:
    world = _World(state=SecState.CLAIMED, config=None)
    run = SetupRun.start(want_key=True, backup_sink=lambda b: None)
    run.answer(await run.run_until_input(_factory(world)), _keyed())  # NeedConfig
    run.answer(await run.run_until_input(_factory(world)), True)  # untested_panel

    await run.run_until_input(_factory(world))

    assert world.config is not None and world.config.security_config is None  # the device keeps its key


async def test_a_panel_the_database_cannot_place_is_asked_for(_migration: list[Any]) -> None:
    world = _World(config=_config(displays=[]), needs_migration=True)
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None)

    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedPanelChoice) and req.reason == "no panel"
    run.answer(req, 0x0037)
    await run.run_until_input(_factory(world))

    assert _migration[-1] == 0x0037


async def test_a_step_the_device_never_takes_is_an_error_not_a_loop() -> None:
    world = _World(claim_mode=True, fresh=True, config=_keyed())

    async def ignored() -> None:
        world.calls.append("use_without_key")  # claim mode stays on

    run = SetupRun.start(want_key=False, backup_sink=lambda b: None)

    @asynccontextmanager
    async def open_device() -> AsyncIterator[_Dev]:
        dev = _Dev(world)
        dev.security.use_without_key = ignored
        yield dev

    with pytest.raises(SetupError, match="use_without_key"):
        await run.run_until_input(open_device)
    assert world.calls.count("use_without_key") == 2


async def test_a_2x_nrf54_is_installed_over_smp(monkeypatch: pytest.MonkeyPatch) -> None:
    from opendisplay.firmware import smp  # pylint: disable=import-outside-toplevel

    world = _World(major=1, target=FirmwareTarget.NRF54, config=_keyed(ic_type=0xFF))
    image = bytes.fromhex("3db8f396") + bytes(60)
    done: list[str] = []

    @asynccontextmanager
    async def session(ble_device: Any, **kw: Any) -> AsyncIterator[str]:
        done.append(f"session:{ble_device.address}")
        yield "client"

    async def upload(client: Any, data: bytes, **kw: Any) -> None:
        done.append(f"upload:{len(data)}")

    async def test_and_reset(client: Any) -> bytes:
        done.append("test_and_reset")
        world.major = 3
        return b""

    monkeypatch.setattr(smp, "session", session)
    monkeypatch.setattr(smp, "upload", upload)
    monkeypatch.setattr(smp, "test_and_reset", test_and_reset)
    monkeypatch.setattr(runner_mod.redraw, "mark_redrawing", lambda *k, allowance: None)

    async def fetch(name: str) -> bytes:
        return image

    async def find(address: str) -> Any:
        return SimpleNamespace(address=address)

    name = "opendisplay-xiao_nrf54l15-ota.bin"
    run = SetupRun.start(
        want_key=False,
        backup_sink=lambda b: None,
        manifest=_manifest_with(image, name),
        fetch_image=fetch,
        find_device=find,
    )
    req = await run.run_until_input(_factory(world))
    assert isinstance(req, rq.NeedBoardChoice)  # an unknown chip: every board is offered
    run.answer(req, "xiao_nrf54l15")
    await run.run_until_input(_factory(world))

    assert done == ["session:AA:BB:CC:DD:EE:FF", f"upload:{len(image)}", "test_and_reset"]
    assert "enter_dfu" not in world.calls  # 1.1.x serves SMP in its app


async def test_the_manifest_may_be_fetched_only_when_a_2x_device_needs_it() -> None:
    asked: list[str] = []

    async def manifest() -> ReleaseManifest:
        asked.append("manifest")
        return MANIFEST

    open_device = _World(config=_keyed())  # 3.0, open, configured: nothing to install
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None, manifest=manifest)
    assert await run.run_until_input(_factory(open_device)) is None
    assert asked == []

    legacy = _World(major=2, config=_keyed())
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None, manifest=manifest)
    assert isinstance(await run.run_until_input(_factory(legacy)), rq.NeedBoardChoice)
    assert isinstance(await run.run_until_input(_factory(legacy)), rq.NeedBoardChoice)
    assert asked == ["manifest"]  # once


@pytest.mark.parametrize("scheme", list(ColorScheme))
def test_panel_pattern_has_a_band_per_ink_of_the_scheme(scheme: ColorScheme) -> None:
    """Every scheme, the split ones included, gets one solid band per ink of its palette."""
    from opendisplay.display_palettes import IDEAL_PALETTE_FILES, load_palette_file

    inks = load_palette_file(IDEAL_PALETTE_FILES[scheme]).entries
    image, names = runner_mod.panel_pattern(1200, 1600, int(scheme))
    assert names == tuple(e.name for e in inks)
    step = 1200 // len(inks)
    for i, ink in enumerate(inks):
        assert image.getpixel((i * step + step // 2, 800)) == ink.nominal


def test_panel_pattern_of_a_split_scheme_shows_all_six_inks() -> None:
    """BWGBRY_SPLIT once fell back to black and white (EE02, 2026-11-26)."""
    _, names = runner_mod.panel_pattern(1200, 1600, int(ColorScheme.BWGBRY_SPLIT))
    assert set(names) == {"black", "white", "yellow", "red", "blue", "green"}


def test_panel_pattern_of_an_unknown_scheme_is_black_and_white() -> None:
    _, names = runner_mod.panel_pattern(400, 300, None)
    assert names == ("black", "white")
    _, names = runner_mod.panel_pattern(400, 300, 99)
    assert names == ("black", "white")


async def test_the_drive_data_comes_from_the_release_when_the_run_can_fetch_it() -> None:
    world = _World(claim_mode=True, fresh=True)

    async def fetch(name: str) -> bytes:
        return b""

    _DBS_USED.clear()
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None, manifest=MANIFEST, fetch_image=fetch)
    req = await run.run_until_input(_factory(world))
    run.answer(req, _config(displays=[_display(panel_ic_type=0x0037)]))
    await run.run_until_input(_factory(world))
    assert _DBS_USED and set(_DBS_USED) == {f"release {MANIFEST.version}"}


async def test_without_a_fetcher_the_bundled_drive_data_is_used() -> None:
    world = _World(claim_mode=True, fresh=True)
    _DBS_USED.clear()
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None, manifest=MANIFEST)
    req = await run.run_until_input(_factory(world))
    run.answer(req, _config(displays=[_display(panel_ic_type=0x0037)]))
    await run.run_until_input(_factory(world))
    assert _DBS_USED and set(_DBS_USED) == {"bundled"}


async def test_a_manifest_that_cannot_be_read_leaves_the_bundled_drive_data() -> None:
    world = _World(claim_mode=True, fresh=True)

    async def manifest() -> ReleaseManifest:
        raise OSError("offline")

    async def fetch(name: str) -> bytes:
        raise AssertionError("nothing to fetch without a manifest")

    _DBS_USED.clear()
    run = SetupRun.start(want_key=False, backup_sink=lambda b: None, manifest=manifest, fetch_image=fetch)
    req = await run.run_until_input(_factory(world))
    run.answer(req, _config(displays=[_display(panel_ic_type=0x0037)]))
    await run.run_until_input(_factory(world))
    assert _DBS_USED and set(_DBS_USED) == {"bundled"}
