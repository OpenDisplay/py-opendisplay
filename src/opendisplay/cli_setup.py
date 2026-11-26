"""`opendisplay setup`, `opendisplay upgrade` and `opendisplay update`.

``setup`` and ``upgrade`` run the setup flow (``opendisplay.setup.SetupRun``) and turn each request
into a prompt. ``upgrade`` is the same flow for a 2.x device: it keeps its state in a file, since
the move to 3.0 may need a USB install in the middle, after which the same command goes on.
``update`` sends one firmware 3.x image with ``dev.firmware.update()``.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from .cli_common import _add_device_options, _console, _error, _handle_ble_error, _run
from .cli_sec2 import _connect_kwargs, _input
from .device import connect
from .exceptions import OpenDisplayError
from .firmware.download import ReleaseDownloader
from .models.config_json import config_from_json
from .setup import Step
from .setup import requests as rq
from .setup.runner import SetupError, SetupRun

_YES: Final = ("y", "yes")
_KEY_CHOICE: Final = (
    "Claim this display? It then answers only to clients that hold its key; without a key anyone "
    "nearby can change it. [y/N] "
)
_GESTURE: Final = (
    "Put the display in claim mode: power-cycle it twice quickly (off, on, off, on). "
    "Press Enter when it shows that it waits to be claimed. "
)
_STEP_LINES: Final = {
    Step.BACK_UP: "Backing up the config...",
    Step.INSTALL_3_0: "Installing firmware 3.0...",
    Step.CONFIRM_IMAGE: "Checking that firmware 3.0 runs...",
    Step.USE_WITHOUT_KEY: "Leaving claim mode (no key)...",
    Step.CONFIGURE_DISPLAY: "Configuring the display...",
    Step.VERIFY_PANEL: "Checking the panel with a test pattern...",
    Step.CLAIM: "Claiming the display...",
    Step.ROTATE_KEY: "Replacing the 2.x key...",
    Step.SECRETS: "Entering the secrets...",
}
_RESUME: Final = (
    "When the display runs 3.0, run the same command again (with --device set to its new name if it "
    "advertises under another one); it goes on from {state}."
)


async def _find(address: str) -> Any:
    """The display after a reboot, by address (a bleak scan, as the CLI connects everywhere)."""
    from bleak import BleakScanner  # pylint: disable=import-outside-toplevel

    return await BleakScanner.find_device_by_address(address, timeout=30.0)


def _ask(prompt: str) -> bool:
    return _input(prompt).strip().lower() in _YES


def add_setup_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``setup``, ``upgrade`` and ``update``."""
    for name, help_text in (
        ("setup", "Set up a firmware 3.0 display: configure, check the panel, claim (or use without a key)"),
        ("upgrade", "Move a firmware 2.x display to 3.0 and set it up (resumable with --state-file)"),
    ):
        p = subparsers.add_parser(name, help=help_text)
        _add_device_options(p)
        choice = p.add_mutually_exclusive_group()
        choice.add_argument("--claim", dest="want_key", action="store_const", const=True, help="Claim the display")
        choice.add_argument(
            "--no-key", dest="want_key", action="store_const", const=False, help="Use the display without a key"
        )
        p.set_defaults(want_key=None)
        p.add_argument("--state-file", metavar="PATH", help="Save the run here and go on from it when run again")
        p.add_argument("--backup", metavar="PATH", help="Where the 2.x config backup goes (it holds the key)")
        p.add_argument("--config", metavar="FILE", help="A config JSON to start from when the display has none")
        p.add_argument("--board", metavar="NAME", help="The board, when several share the chip (s3-n32r8, ...)")
        p.add_argument("--release", metavar="TAG", help="The Firmware_Unified release (default: the latest)")
        p.add_argument("--yes-untested", action="store_true", help="Do not ask before drive data not seen on a panel")
        p.set_defaults(func=lambda args: _run(_setup(args)), upgrade=name == "upgrade")

    update = subparsers.add_parser("update", help="Send a firmware 3.x image to a 3.x display over Bluetooth")
    _add_device_options(update)
    update.add_argument(
        "--file",
        metavar="IMAGE",
        help="Send this image file (default: the release's image for the board the display reports)",
    )
    update.add_argument("--board", metavar="NAME", help="The release board, when the display does not report one")
    update.add_argument("--release", metavar="TAG", help="The Firmware_Unified release (default: the latest)")
    update.add_argument("--yes", action="store_true", help="Do not ask, also on a device that cannot roll back")
    update.set_defaults(func=lambda args: _run(_update(args)))


# ── files ────────────────────────────────────────────────────────────────────


def _backup_sink(path: Path) -> Callable[[bytes], None]:
    """Write the backup to ``path``, or ``path-1``, ``path-2``... if taken: never over an earlier one."""

    def sink(data: bytes) -> None:
        target, n = path, 0
        while target.exists():
            n += 1
            target = path.with_name(f"{path.stem}-{n}{path.suffix}")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        _console.print(f"Config backup written to [bold]{target}[/bold]. It holds the display's key: keep it safe.")

    return sink


def _state_path(args: argparse.Namespace) -> Path | None:
    if args.state_file:
        return Path(args.state_file)
    if args.upgrade:
        return Path(f"opendisplay-upgrade-{args.device or args.host}.json")
    return None


# ── the requests as prompts ──────────────────────────────────────────────────


class _Paused(Exception):
    """The run waits for something outside the terminal (a USB install)."""


def _confirm(request: rq.NeedConfirmation, args: argparse.Namespace) -> bool:
    if request.reason == "untested_panel" and args.yes_untested:
        return True
    _console.print(request.detail)
    if request.reason == "no_rollback":
        return _input("Type yes to install anyway: ").strip().lower() == "yes"
    return _ask("Continue? [y/N] " if request.reason != "panel_ok" else "[y/N] ")


def _choose(candidates: tuple[str, ...], given: str | None, what: str) -> str:
    if given is not None and given in candidates:
        return given
    _console.print(f"Which {what}? " + ", ".join(candidates))
    return _input(f"{what.capitalize()}: ").strip()


def _answer(request: rq.Request, args: argparse.Namespace) -> Any:  # pylint: disable=too-many-return-statements
    """The answer to ``request``, from the options or the terminal."""
    if isinstance(request, rq.NeedKeyChoice):
        return _ask(_KEY_CHOICE)
    if isinstance(request, rq.NeedGesture):
        _input(_GESTURE)
        return None
    if isinstance(request, rq.NeedClaimCode):
        if not request.has_panel:
            _error("The display cannot show a claim code; claim it with `opendisplay claim` instead.")
        if request.retry:
            _console.print("That code did not match; the display shows a new one.")
        return _input("The 6-digit code on the display: ")
    if isinstance(request, rq.NeedConfirmation):
        return _confirm(request, args)
    if isinstance(request, rq.NeedBoardChoice):
        return _choose(request.candidates, args.board, "board")
    if isinstance(request, rq.NeedPanelChoice):
        _console.print(request.reason)
        return int(_input("Panel id (for example 0x0037): ").strip(), 0)
    if isinstance(request, rq.NeedConfig):
        path = args.config or _input("A config JSON to start from (Config Builder export): ").strip()
        return config_from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    _wired_install(request)
    raise _Paused


def _wired_install(request: rq.NeedWiredInstall) -> None:
    _console.print(f"Install firmware 3.0 on board [bold]{request.board}[/bold] over USB:")
    for name in request.files:
        _console.print(f"  {name}")
    _console.print(f"Flasher: {request.url}" + (f"  Release: {request.release_url}" if request.release_url else ""))
    if request.protocol:
        _console.print(f"Method: {request.protocol}")
    if request.erases_config:
        _console.print("This erases the display's config; the backup restores it afterwards.")


# ── setup / upgrade ──────────────────────────────────────────────────────────


async def _setup(args: argparse.Namespace, *, open_device: Any = None, releases: Any = None) -> None:
    releases = releases or ReleaseDownloader(args.release)
    state = _state_path(args)
    sink = _backup_sink(Path(args.backup or f"opendisplay-backup-{args.device or args.host}.json"))
    common: dict[str, Any] = {
        "backup_sink": sink,
        "manifest": releases.manifest,
        "fetch_image": releases.fetch,
        "find_device": _find,
    }
    if state is not None and state.exists():
        run = SetupRun.from_json(state.read_text(encoding="utf-8"), **common)
        _console.print(f"Going on from {state}.")
    else:
        run = SetupRun.start(want_key=args.want_key, **common)
    factory = open_device or (lambda: connect(**_connect_kwargs(args)))
    announced: set[Step] = set()

    def announce(step: Step) -> None:
        if step not in announced:  # a step that paused runs again once answered
            announced.add(step)
            _console.print(_STEP_LINES.get(step, f"{step.value}..."))

    run.on_step = announce

    def save() -> None:
        if state is not None:
            state.write_text(run.to_json(), encoding="utf-8")

    try:
        while (request := await run.run_until_input(factory)) is not None:
            save()
            try:
                value = _answer(request, args)
            except _Paused:
                if state is None:
                    _error("This step needs a USB install: run again with --state-file PATH to go on afterwards.")
                _console.print(_RESUME.format(state=state))
                return
            run.answer(request, value)
    except SetupError as exc:
        save()
        _error(str(exc))
    except OpenDisplayError as exc:
        save()
        _handle_ble_error(exc)
    save()
    if run.stopped:
        _console.print(f"Stopped ({run.stopped}). Run the same command again to go on.")
    elif run.finished:
        _console.print("The display is set up.")
        if state is not None and state.exists():
            state.unlink()


# ── update ───────────────────────────────────────────────────────────────────


async def _release_image(args: argparse.Namespace, dev: Any) -> bytes:
    """The release's Bluetooth image for the display's board (``--board``, else READ_IDENT's)."""
    ident = dev.profile.ident
    board = args.board or (ident.board if ident is not None else None)
    if board is None:
        _error("The display does not report its board (firmware before 3.1). Pass --board NAME or --file IMAGE.")
    releases = ReleaseDownloader(args.release)
    manifest = await releases.manifest()
    name = manifest.ble_ota_file(board)
    if name is None:
        known = ", ".join(b.board for b in manifest.boards)
        _error(f"Release {manifest.version} has no Bluetooth image for board {board!r} (boards: {known}).")
    _console.print(f"Release {manifest.version}, board {board}: {name}")
    data: bytes = await releases.fetch(name)  # checked against the manifest's SHA-256
    return data


async def _update(args: argparse.Namespace) -> None:
    image = Path(args.file).read_bytes() if args.file else None
    try:
        async with connect(**_connect_kwargs(args)) as dev:
            if image is None:
                image = await _release_image(args, dev)
            if dev.firmware.can_roll_back:
                _console.print("If the new image does not start, the display returns to its old one by itself.")
            else:
                _console.print(
                    "This display cannot return to its old image by itself: if the new one does not start, "
                    "it waits in its bootloader, and without USB only a debugger recovers it."
                )
                if not args.yes and _input("Type yes to send it anyway: ").strip().lower() != "yes":
                    _error("Not sent.")
            last = [-1]

            def progress(done: int, total: int) -> None:
                if done * 10 // total != last[0]:
                    last[0] = done * 10 // total
                    _console.print(f"  {done * 100 // total}%")

            result = await dev.firmware.update(image, on_progress=progress)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print(f"{result.outcome.value}: {result.detail}")
    if result.outcome.value != "updated":
        raise SystemExit(1)
