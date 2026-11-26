"""``SetupRun``: perform a setup plan, pausing on requests, saved and resumed as JSON.

Every ``run_until_input`` opens the device afresh, reads where it stands and plans again
(``plan_setup``), so a saved run never repeats a step the device shows as done: a run saved while
asking for a wired install, resumed after the device was updated some other way, goes on from the
3.0 steps (Review Focus 4). Only steps the device cannot show (the backup, the panel check, the
image confirm) are remembered as done.

A claim code is confirmed on the link that asked for it: the run holds that link open across the
``NeedClaimCode`` request. A saved run does not keep it; resumed, it starts the claim again.

Secrets never enter the saved run: the backup goes to the caller's ``backup_sink`` as it is, and
the run keeps a copy without secrets to configure the device from if its config is lost.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any, Final

from epaper_dithering import DitherMode
from PIL import Image, ImageDraw

from .. import redraw
from ..display_palettes import IDEAL_PALETTE_FILES, load_palette_file
from ..exceptions import OpenDisplayError
from ..firmware import smp
from ..firmware.releases import BoardRelease, ReleaseManifest
from ..models.config import GlobalConfig
from ..models.config_json import config_from_json, config_to_json
from ..models.enums import ColorScheme, ICType
from ..ota import perform_silabs_ota
from ..panels import PanelDatabase, apply_migration, plan_migration
from ..protocol.config_serializer import serialize_config
from ..sec2.errors import Sec2CodeMismatchError
from . import requests as rq
from .configure import strip_secrets, write_and_verify
from .planner import Step, plan_setup
from .state import device_state

_LOGGER = logging.getLogger(__name__)

INSTALL_URL: Final = "https://opendisplay.org/firmware/install/"
RELEASES_URL: Final = "https://github.com/OpenDisplay/Firmware_Unified/releases/tag/"
_REBOOT_ALLOWANCE_S: Final = 180.0
_MAX_STEPS: Final = 24
_FORMAT: Final = 1
_CHIPS: Final[dict[int, str]] = {
    ICType.NRF52840: "nrf52840",
    ICType.ESP32_S3: "esp32s3",
    ICType.ESP32_C3: "esp32c3",
    ICType.ESP32_C6: "esp32c6",
    ICType.EFR32BG22: "efr32bg22",
}
_REMEMBERED: Final = frozenset({Step.BACK_UP, Step.CONFIRM_IMAGE, Step.VERIFY_PANEL, Step.SECRETS})
DeviceFactory = Callable[[], AbstractAsyncContextManager[Any]]
ManifestSource = ReleaseManifest | Callable[[], Awaitable[ReleaseManifest]]
FetchImage = Callable[[str], Awaitable[bytes]]
FindDevice = Callable[[str], Awaitable[Any]]


class SetupError(OpenDisplayError):
    """The setup cannot go on; the message says why. A saved run can be resumed once fixed."""


def panel_pattern(width: int, height: int, scheme: int | None) -> tuple[Image.Image, tuple[str, ...]]:
    """Vertical bands of every ink the scheme has, and their names left to right.

    The inks are the scheme's idealised palette, so every scheme gets its own bands, the split
    ones included. An unknown scheme gets black and white.
    """
    try:
        known = ColorScheme.from_value(scheme) if scheme is not None else ColorScheme.MONO
    except ValueError:
        known = ColorScheme.MONO
    palette = load_palette_file(IDEAL_PALETTE_FILES[known])
    colours = [ink.nominal or shown for ink, shown in zip(palette.entries, palette.preview_rgb(), strict=True)]
    names = tuple(ink.name or "#" + bytes(colour).hex() for ink, colour in zip(palette.entries, colours, strict=True))
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    step = width // len(colours)
    for i, colour in enumerate(colours):
        right = width - 1 if i == len(colours) - 1 else (i + 1) * step - 1
        draw.rectangle((i * step, 0, right, height - 1), fill=colour)
    return image, names


class SetupRun:  # pylint: disable=too-many-instance-attributes
    """One setup or upgrade of one device. Make it with :meth:`start` or :meth:`from_json`."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        want_key: bool | None,
        rotate_legacy_key: bool,
        backup_sink: Callable[[bytes], None],
        manifest: ManifestSource | None,
        fetch_image: FetchImage | None,
        find_device: FindDevice | None,
    ) -> None:
        self.want_key = want_key
        self.rotate_legacy_key = rotate_legacy_key
        self._sink = backup_sink
        self._manifest_source = manifest
        self._manifest: ReleaseManifest | None = manifest if isinstance(manifest, ReleaseManifest) else None
        self._fetch = fetch_image
        self._find = find_device
        self._db: PanelDatabase | None = None  # per run: read once, from the release when it can
        self._done: set[Step] = set()
        self._backup: dict[str, Any] | None = None  # without secrets
        self._board: str | None = None
        self._panel: int | None = None
        self._accepted: set[str] = set()
        self._claim_code: str | None = None
        self._claim_retry = False
        self._live: tuple[AsyncExitStack, Any] | None = None  # the claim link and its device
        self._live_flow: Any = None
        self._stop_pending = False
        self._verify_owed = False  # the run wrote a config; its panel check is still to come
        #: The request the run waits for, or ``None``.
        self.pending: rq.Request | None = None
        #: The confirmation the user declined, which stopped the run; cleared when it runs again.
        self.stopped: str | None = None
        #: Whether the device is set up.
        self.finished = False
        #: The planner's text keys for the device as last seen.
        self.notes: tuple[str, ...] = ()
        #: Called with each step just before it runs (a step that pauses runs again on resume).
        self.on_step: Callable[[Step], None] | None = None

    # ── making, saving, answering ────────────────────────────────────────────

    @classmethod
    def start(  # pylint: disable=too-many-arguments
        cls,
        *,
        want_key: bool | None,
        backup_sink: Callable[[bytes], None],
        manifest: ManifestSource | None = None,
        fetch_image: FetchImage | None = None,
        find_device: FindDevice | None = None,
        rotate_legacy_key: bool = True,
    ) -> SetupRun:
        """A new run.

        Args:
            want_key: Claim the device (``True``), use it without a key (``False``), or ask
                (``None``: an open device then pauses on ``NeedKeyChoice``).
            backup_sink: Receives the 2.x config backup, with its secrets, as JSON bytes. The
                run never writes it anywhere else.
            manifest: The release to install from a 2.x device (``ReleaseManifest.parse``), or
                an async callable returning it, called once and only for a 2.x device.
            fetch_image: Returns a release file's bytes by name, for an install over Bluetooth.
            find_device: Finds the display's ``BLEDevice`` by address (Home Assistant passes its
                own); needed for an install over Bluetooth.
            rotate_legacy_key: Replace a 2.x key on a claimed device.
        """
        return cls(
            want_key=want_key,
            rotate_legacy_key=rotate_legacy_key,
            backup_sink=backup_sink,
            manifest=manifest,
            fetch_image=fetch_image,
            find_device=find_device,
        )

    def to_json(self) -> str:
        """The run as JSON, without the claim link and without secrets."""
        return json.dumps(
            {
                "format": _FORMAT,
                "want_key": self.want_key,
                "rotate_legacy_key": self.rotate_legacy_key,
                "done": sorted(step.value for step in self._done),
                "backup": self._backup,
                "board": self._board,
                "panel": self._panel,
                "accepted": sorted(self._accepted),
                "claim_retry": self._claim_retry,
                "pending": rq.to_dict(self.pending) if self.pending is not None else None,
                "stopped": self.stopped,
                "stop_pending": self._stop_pending,
                "verify_owed": self._verify_owed,
                "finished": self.finished,
                "notes": list(self.notes),
            }
        )

    @classmethod
    def from_json(  # pylint: disable=too-many-arguments
        cls,
        data: str,
        *,
        backup_sink: Callable[[bytes], None],
        manifest: ManifestSource | None = None,
        fetch_image: FetchImage | None = None,
        find_device: FindDevice | None = None,
    ) -> SetupRun:
        """A run ``to_json`` saved; the callables are the caller's again.

        Raises:
            ValueError: Not a saved run, or another format.
        """
        doc = json.loads(data)
        if doc.get("format") != _FORMAT:
            raise ValueError(f"Unsupported saved setup run format {doc.get('format')!r}")
        run = cls.start(
            want_key=doc["want_key"],
            backup_sink=backup_sink,
            manifest=manifest,
            fetch_image=fetch_image,
            find_device=find_device,
            rotate_legacy_key=doc["rotate_legacy_key"],
        )
        run._done = {Step(value) for value in doc["done"]}
        run._backup = doc["backup"]
        run._board = doc["board"]
        run._panel = doc["panel"]
        run._accepted = set(doc["accepted"])
        run._claim_retry = doc["claim_retry"]
        run.pending = rq.from_dict(doc["pending"]) if doc["pending"] is not None else None
        run.stopped = doc["stopped"]
        run._stop_pending = doc["stop_pending"]
        run._verify_owed = doc.get("verify_owed", False)
        run.finished = doc["finished"]
        run.notes = tuple(doc["notes"])
        return run

    def answer(self, request: rq.Request | None, value: Any) -> None:  # pylint: disable=too-many-branches
        """Answer the pending ``request``; the next ``run_until_input`` goes on from it.

        Raises:
            ValueError: ``request`` is not the pending one.
        """
        if request is None or request != self.pending:
            raise ValueError(f"{request!r} is not the pending request ({self.pending!r})")
        self.pending = None
        if isinstance(request, rq.NeedKeyChoice):
            self.want_key = bool(value)
        elif isinstance(request, rq.NeedBoardChoice):
            self._board = str(value)
        elif isinstance(request, rq.NeedPanelChoice):
            self._panel = int(value)
        elif isinstance(request, rq.NeedClaimCode):
            self._claim_code = str(value)
        elif isinstance(request, rq.NeedConfig):
            if not isinstance(value, GlobalConfig):
                raise ValueError("NeedConfig takes a GlobalConfig")
            self._backup = config_to_json(strip_secrets(value))
        elif isinstance(request, rq.NeedConfirmation):
            if value:
                self._accepted.add(f"{request.reason}:{request.subject}")
                if request.reason == "panel_ok":
                    self._done.add(Step.VERIFY_PANEL)
            else:
                self.stopped, self._stop_pending = request.reason, True
        # NeedGesture and NeedWiredInstall need no value: the device shows what was done.

    # ── running ──────────────────────────────────────────────────────────────

    async def run_until_input(self, device_factory: DeviceFactory) -> rq.Request | None:
        """Go on until the run needs an answer (the request), finishes, or was stopped (``None``).

        Args:
            device_factory: Opens the device, connected: ``opendisplay.connect(...)`` or the
                caller's own; called again for every step.

        Raises:
            SetupError: The setup cannot go on (see the message); the run can be saved and resumed.
        """
        if self._stop_pending:
            self._stop_pending = False
            return None
        self.stopped = None
        await self._finish_claim()
        last: Step | None = None
        for _ in range(_MAX_STEPS):
            async with AsyncExitStack() as stack:
                dev = await stack.enter_async_context(device_factory())
                step, request = await self._next(dev, stack.aclose)
                if isinstance(request, rq.NeedClaimCode):
                    self._live = (stack.pop_all(), dev)
            if step is None or request is not None:
                self.pending = request
                return request
            if step is last and step not in _REMEMBERED:
                raise SetupError(f"The step '{step.value}' did not take; the device still needs it")
            last = step
        raise SetupError("The setup did not finish within its step limit")

    async def _next(self, dev: Any, close: Callable[[], Awaitable[None]]) -> tuple[Step | None, rq.Request | None]:
        """Plan from the device and perform the first step not done: (step, request)."""
        profile = dev.profile
        legacy = profile.firmware["major"] < 3
        status = dev.security.status
        state = device_state(firmware=profile.firmware, ident=profile.ident, sec_status=status, advertisement=None)
        plan = plan_setup(
            state=state,
            profile=None if legacy else profile,
            sec_status=status,
            want_key=self.want_key,
            rotate_legacy_key=self.rotate_legacy_key,
        )
        self.notes = plan.notes
        steps = plan.steps
        if self._verify_owed and Step.VERIFY_PANEL not in steps and Step.VERIFY_PANEL not in self._done:
            # Once configured, the device no longer plans the check of the config this run wrote.
            steps = (Step.VERIFY_PANEL, *(steps or (Step.DONE,)))
        if not steps:
            if "claim_choice" in plan.notes:
                return None, rq.NeedKeyChoice()
            self.finished = True
            return None, None
        step = next((s for s in steps if s not in self._done), Step.DONE)
        if step is Step.DONE:
            self.finished = True
            return None, None
        if self.on_step is not None:
            self.on_step(step)
        request = await self._perform(step, dev, close)
        if request is None and step in _REMEMBERED:
            self._done.add(step)
        return step, request

    async def _perform(  # pylint: disable=too-many-return-statements
        self, step: Step, dev: Any, close: Callable[[], Awaitable[None]]
    ) -> rq.Request | None:
        if step is Step.BACK_UP:
            self._back_up(dev)
            return None
        if step is Step.INSTALL_3_0:
            return await self._install(dev, close)
        if step is Step.CONFIRM_IMAGE:
            await dev.legacy.read_ident()
            return None
        if step is Step.USE_WITHOUT_KEY:
            await dev.security.use_without_key()
            return None
        if step is Step.CONFIGURE_DISPLAY:
            return await self._configure(dev)
        if step is Step.VERIFY_PANEL:
            return await self._verify(dev)
        if step is Step.CLAIM:
            return await self._claim(dev)
        if step is Step.ROTATE_KEY:
            await dev.security.rotate_key()
            return None
        return None  # SECRETS: entered by the caller once claimed; nothing for the run to write

    # ── the steps ────────────────────────────────────────────────────────────

    def _back_up(self, dev: Any) -> None:
        config = dev.config.cached
        if config is not None:
            self._sink(json.dumps(config_to_json(config), indent=2).encode())
            self._backup = config_to_json(strip_secrets(config))

    async def _release(self) -> ReleaseManifest:
        if self._manifest is None:
            if self._manifest_source is None or isinstance(self._manifest_source, ReleaseManifest):
                raise SetupError("Installing 3.0 needs the release manifest (SetupRun.start(manifest=...))")
            self._manifest = await self._manifest_source()
        return self._manifest

    async def _panel_db(self) -> PanelDatabase:
        """The release's drive database when this run has a release and a fetcher, else the bundled one."""
        if self._db is None:
            manifest = self._manifest
            if manifest is None and self._fetch is not None and callable(self._manifest_source):
                try:
                    manifest = self._manifest = await self._manifest_source()
                except Exception as err:  # pylint: disable=broad-exception-caught  # offline: the bundled copy
                    _LOGGER.warning("Release manifest not read (%s); using the bundled drive presets", err)
            if manifest is not None and self._fetch is not None:
                self._db = await PanelDatabase.for_release(manifest, self._fetch)
            else:
                self._db = PanelDatabase.load()
            _LOGGER.debug("Drive presets: %s", self._db.source)
        return self._db

    async def _choose_board(self, dev: Any) -> BoardRelease | rq.NeedBoardChoice:
        await self._release()
        assert self._manifest is not None
        if self._board is not None:
            chosen = self._manifest.board(self._board)
            if chosen is None:
                raise SetupError(f"The release has no board {self._board!r}")
            return chosen
        config = dev.config.cached
        chip = _CHIPS.get(config.system.ic_type) if config is not None else None
        boards = self._manifest.boards_for_chip(chip) if chip is not None else list(self._manifest.boards)
        if len(boards) == 1:
            return boards[0]
        return rq.NeedBoardChoice(candidates=tuple(b.board for b in boards))

    async def _install(self, dev: Any, close: Callable[[], Awaitable[None]]) -> rq.Request | None:
        board = await self._choose_board(dev)
        if isinstance(board, rq.NeedBoardChoice):
            return board
        assert self._manifest is not None
        if not board.from_2x_files:
            return rq.NeedWiredInstall(
                board=board.board,
                files=board.usb_install_files,
                protocol=board.usb_install_protocol,
                erases_config=board.usb_install_erases_config,
                url=INSTALL_URL,
                release_url=RELEASES_URL + self._manifest.version,
            )
        if board.from_2x_protocol == "silabs-apploader" and f"no_rollback:{board.board}" not in self._accepted:
            return rq.NeedConfirmation(
                reason="no_rollback",
                subject=board.board,
                detail="A BG22 has no old image to return to: if the new image does not start, it waits in "
                "its AppLoader (advertising as 'OTA') for a .gbl, and without USB only a debugger recovers it.",
            )
        image = await self._image(board.from_2x_files[0])
        address = dev.legacy.mac_address
        keys = dev.legacy._redraw_keys()  # pylint: disable=protected-access
        if self._find is None:
            raise SetupError("Installing over Bluetooth needs find_device (SetupRun.start(find_device=...))")
        if board.from_2x_protocol == "silabs-apploader":
            await dev.legacy.trigger_dfu_bootloader()  # 2.x: reboots into the AppLoader
            await close()
            await perform_silabs_ota(image, await self._find(address), None)
        else:  # mcumgr-smp: Firmware_NRF54 1.1.x serves SMP in its app
            await close()
            async with smp.session(await self._find(address)) as client:
                await smp.upload(client, image)
                await smp.test_and_reset(client)
        redraw.mark_redrawing(*keys, allowance=_REBOOT_ALLOWANCE_S)
        return None

    async def _image(self, name: str) -> bytes:
        if self._fetch is None:
            raise SetupError("Installing over Bluetooth needs fetch_image (SetupRun.start(fetch_image=...))")
        image = await self._fetch(name)
        artefact = self._manifest.artefact(name) if self._manifest is not None else None
        if artefact is not None and hashlib.sha256(image).hexdigest() != artefact.sha256:
            raise SetupError(f"{name} does not match the release manifest's SHA-256; not sent")
        return image

    @staticmethod
    def _for_device(dev: Any, config: GlobalConfig) -> GlobalConfig:
        """The saved backup (already without secrets) as this device stores it.

        A claimed device keeps its own key: the backup goes without its 0x27. An unclaimed one
        stores the 0x27 in its unclaimed form (show-key flag cleared), which ``strip_secrets``
        gave it already.
        """
        if dev.security.sec2_session_open:
            return dataclasses.replace(config, security_config=None)
        return config

    async def _configure(self, dev: Any) -> rq.Request | None:
        profile = dev.profile
        start: GlobalConfig | None = dev.config.cached if profile.configured else None
        from_backup = start is None and self._backup is not None
        if from_backup:
            assert self._backup is not None
            start = self._for_device(dev, config_from_json(self._backup))
        if start is None:
            return rq.NeedConfig()
        plan = plan_migration(start, profile, await self._panel_db(), panel_ic_type=self._panel)
        if plan.blocked:
            return rq.NeedPanelChoice(reason=plan.reason)
        subject = f"{plan.panel_ic_type:#06x}" if plan.panel_ic_type is not None else ""
        if plan.needs_confirmation and f"untested_panel:{subject}" not in self._accepted:
            return rq.NeedConfirmation(reason="untested_panel", detail=plan.reason, subject=subject)
        config = apply_migration(start, plan) if plan.needed else start
        stored = dev.config.cached
        if stored is not None and serialize_config(stored) == serialize_config(config):
            self._done.add(Step.CONFIGURE_DISPLAY)  # nothing to write; the device says otherwise
            return None
        await write_and_verify(dev, config)
        self._verify_owed = True
        return None

    async def _verify(self, dev: Any) -> rq.Request:
        status = dev.security.status
        if status is not None and status.claim_mode:
            # Claim mode refuses every command that draws (SECURITY_V2 section 7): the panel
            # check is the device's own setup screen, which shows SETUP MODE and its ID.
            name = dev.legacy.device_name
            return rq.NeedConfirmation(
                reason="panel_ok",
                detail="Does the display show SETUP MODE" + (f" and the ID {name}?" if name else "?"),
            )
        profile = dev.profile
        image, names = panel_pattern(profile.width or 400, profile.height or 300, profile.color_scheme)
        await dev.display.show(image, dither_mode=DitherMode.NONE)
        return rq.NeedConfirmation(
            reason="panel_ok",
            detail="The display shows bands, left to right: " + ", ".join(names) + ". Do they look right?",
        )

    async def _claim(self, dev: Any) -> rq.Request:
        status = dev.security.status
        if status is None or not status.claim_mode:
            return rq.NeedGesture()
        flow = await dev.security.claim()
        await flow.begin()
        code = await flow.exchange_nonces()
        self._live_flow = flow
        return rq.NeedClaimCode(has_panel=bool(code.has_panel), retry=self._claim_retry)

    async def _finish_claim(self) -> None:
        """Confirm a claim on the link that asked for the code, then close it."""
        if self._live is None:
            return
        stack, _ = self._live
        flow, self._live = self._live_flow, None
        code, self._claim_code = self._claim_code, None
        try:
            if code is None:
                await flow.abort()  # run again without an answer: start the claim over
                return
            try:
                await flow.confirm(code)
                self._claim_retry = False
            except Sec2CodeMismatchError:
                self._claim_retry = True  # confirm() sent ABORT; the next claim asks again
        finally:
            await stack.aclose()
