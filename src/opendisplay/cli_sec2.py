"""`opendisplay claim`, `unclaim` and `rotate-key`: firmware 3.0 device keys (SEC2)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Final

from .cli_common import _add_device_options, _console, _device_kwargs, _error, _handle_ble_error, _parse_hex_key, _run
from .device import connect
from .exceptions import OpenDisplayError
from .sec2.errors import Sec2CodeMismatchError
from .sec2.keystore import DEFAULT_PATH, JsonFileKeyStore

_CODE_PROMPT: Final = (
    "Enter the 6-digit code that now appears on the display. It can take up to a minute. If a code was already "
    "showing before you started, stop: someone else is claiming this display.\nCode: "
)
_NO_SCREEN_PROMPT: Final = (
    "This display reports that it has no screen, so its code cannot be checked. Claiming is then protected only "
    "by claim mode, which you started on the device. Continue only if the display really has no screen and "
    "nobody you distrust is nearby. Type 'yes' to continue: "
)
_UNCLAIM_PROMPT: Final = (
    "Unclaiming removes the display's key: it becomes open to anyone nearby until it is claimed again. Continue? [y/N] "
)


def _input(prompt: str) -> str:
    """Read one answer from the terminal; no terminal reads as an empty answer."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def _add_key_store_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--key-store",
        metavar="PATH",
        default=str(DEFAULT_PATH),
        help=f"Where firmware 3.0 device keys are kept (default: {DEFAULT_PATH})",
    )


def _connect_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs = _device_kwargs(args.device, _parse_hex_key(args.key), args.timeout, args.host, args.port, args.tls)
    names = {"mac_address": "address", "device_name": "name"}
    out = {names.get(k, k): v for k, v in kwargs.items()}
    out["key_store"] = JsonFileKeyStore(Path(args.key_store))
    return out


def add_sec2_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``claim``, ``unclaim`` and ``rotate-key``."""
    claim = subparsers.add_parser("claim", help="Claim a firmware 3.0 display in claim mode and keep its new key")
    _add_device_options(claim)
    _add_key_store_option(claim)
    claim.set_defaults(func=lambda args: _run(_claim(_connect_kwargs(args))))

    unclaim_parser = subparsers.add_parser("unclaim", help="Remove a firmware 3.0 display's key (it becomes open)")
    _add_device_options(unclaim_parser)
    _add_key_store_option(unclaim_parser)
    unclaim_parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    unclaim_parser.set_defaults(func=lambda args: _run(_unclaim(_connect_kwargs(args), args.yes)))

    rotate = subparsers.add_parser("rotate-key", help="Replace a firmware 3.0 display's key with a new one")
    _add_device_options(rotate)
    _add_key_store_option(rotate)
    rotate.set_defaults(func=lambda args: _run(_rotate(_connect_kwargs(args))))


async def _claim(kwargs: dict[str, Any]) -> None:
    try:
        async with connect(**kwargs) as dev:
            flow = await dev.security.claim()
            await flow.begin()
            code = await flow.exchange_nonces()
            if code.has_panel:
                await flow.confirm(_input(_CODE_PROMPT))
            elif _input(_NO_SCREEN_PROMPT).strip().lower() == "yes":
                await flow.confirm_without_code(acknowledged=True)
            else:
                await flow.abort()
                _error("Not claimed.")
    except Sec2CodeMismatchError as exc:
        _error(str(exc))
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print(f"Display claimed; its key is in [bold]{kwargs['key_store'].path}[/bold]. Keep that file safe.")


async def _unclaim(kwargs: dict[str, Any], yes: bool) -> None:
    if not yes and _input(_UNCLAIM_PROMPT).strip().lower() not in ("y", "yes"):
        _error("Not unclaimed.")
    try:
        async with connect(**kwargs) as dev:
            await dev.security.unclaim()
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print("Display unclaimed; it is open until it is claimed again.")


async def _rotate(kwargs: dict[str, Any]) -> None:
    try:
        async with connect(**kwargs) as dev:
            await dev.security.rotate_key()
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print(f"Key rotated; the new key is in [bold]{kwargs['key_store'].path}[/bold].")
