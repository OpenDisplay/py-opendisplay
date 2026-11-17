"""`opendisplay claim`, `unclaim` and `rotate-key`: firmware 3.0 device keys (SEC2)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Final

from .cli_common import (
    _add_device_options,
    _add_key_store_option,
    _console,
    _device_kwargs,
    _error,
    _handle_ble_error,
    _parse_hex_key,
    _run,
)
from .device import connect
from .exceptions import OpenDisplayError
from .landing import parse_landing_url
from .sec2.errors import Sec2CodeMismatchError
from .sec2.keystore import JsonFileKeyStore

_CODE_PROMPT: Final = (
    "Enter the 6-digit code that now appears on the display. It can take up to a minute. If a code was already "
    "showing before you started, stop: someone else is claiming this display.\nCode: "
)
_NO_SCREEN_PROMPT: Final = (
    "This display reports that it has no screen, so its code cannot be checked. Claiming is then protected only "
    "by claim mode, which you started on the device. Continue only if the display really has no screen and "
    "nobody you distrust is nearby. Type 'yes' to continue: "
)
# SECURITY_V2.md § 10.7, the wording every client uses before it reveals a key.
_EXPORT_PROMPT: Final = (
    "Anyone who has this key can change the display, update its firmware, and read your WiFi password "
    "and FindMy keys. Print it? [y/N] "
)
_DISPLAY_NAME = re.compile(r"^OD([0-9A-F]{6})$", re.IGNORECASE)
_UNCLAIM_PROMPT: Final = (
    "Unclaiming removes the display's key: it becomes open to anyone nearby until it is claimed again. Continue? [y/N] "
)


def _input(prompt: str) -> str:
    """Read one answer from the terminal; no terminal reads as an empty answer."""
    try:
        return input(prompt)
    except EOFError:
        return ""


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
    claim.set_defaults(func=lambda args: _run(_claim(_connect_kwargs(args))))

    unclaim_parser = subparsers.add_parser("unclaim", help="Remove a firmware 3.0 display's key (it becomes open)")
    _add_device_options(unclaim_parser)
    unclaim_parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    unclaim_parser.set_defaults(func=lambda args: _run(_unclaim(_connect_kwargs(args), args.yes)))

    rotate = subparsers.add_parser("rotate-key", help="Replace a firmware 3.0 display's key with a new one")
    _add_device_options(rotate)
    rotate.set_defaults(func=lambda args: _run(_rotate(_connect_kwargs(args))))

    key = subparsers.add_parser("key", help="Move a firmware 3.0 device key to or from another client")
    actions = key.add_subparsers(dest="key_action", required=True)
    export = actions.add_parser("export", help="Print a display's confirmed key (to give to another client)")
    export.add_argument("--device", required=True, metavar="NAME", help="Display name as in the key store (OD...)")
    export.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    _add_key_store_option(export)
    export.set_defaults(func=_cmd_key_export)
    import_parser = actions.add_parser("import", help="Store a key from another client for a display")
    import_parser.add_argument("--device", required=True, metavar="NAME", help="Display name (OD...)")
    import_parser.add_argument("key", metavar="KEY", help="32 hex digits, or an opendisplay.org/l/ link with a key")
    _add_key_store_option(import_parser)
    import_parser.set_defaults(func=_cmd_key_import)


def _cmd_key_export(args: argparse.Namespace) -> None:
    store = JsonFileKeyStore(Path(args.key_store))
    key = store.confirmed(args.device)
    if key is None:
        if store.keys_to_try(args.device):
            _error(f"The key for {args.device} is not confirmed yet: connect once so it opens a session.")
        _error(f"No key for {args.device} in {store.path}.")
    if not args.yes and _input(_EXPORT_PROMPT).strip().lower() not in ("y", "yes"):
        _error("Not exported.")
    print(key.hex())  # stdout, so it can be piped; never logged


def _imported_key(name: str, text: str) -> bytes:
    text = text.strip()
    if "/l" not in text:
        cleaned = text.replace(" ", "").replace(":", "")
        try:
            key = bytes.fromhex(cleaned)
        except ValueError:
            key = b""
        if len(key) != 16:
            _error("KEY must be 32 hex digits (16 bytes) or an opendisplay.org/l/ link with a key.")
        return key
    try:
        info = parse_landing_url(text)
    except ValueError as exc:
        _error(f"Not a valid opendisplay.org/l/ link: {exc}")
    if info.encryption_key is None:
        _error("This link carries no key: the display hides it. Export the key from a client that has it.")
    match = _DISPLAY_NAME.match(name)
    link_name = "OD" + info.device_id.hex().upper()
    if match is not None and match.group(1).upper() != info.device_id.hex().upper():
        _error(f"This link is for {link_name}, not {name}.")
    return info.encryption_key


def _cmd_key_import(args: argparse.Namespace) -> None:
    key = _imported_key(args.device, args.key)
    store = JsonFileKeyStore(Path(args.key_store))
    store.add_candidate(args.device, key, "import")
    _console.print(
        f"Key stored for {args.device} in [bold]{store.path}[/bold]; it becomes the confirmed key the first time "
        "it opens a session."
    )


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
    _console.print(
        f"Key rotated; the new key is in [bold]{kwargs['key_store'].path}[/bold]. Other clients that use this "
        "display need it: run [bold]opendisplay key export[/bold] and import it there."
    )
