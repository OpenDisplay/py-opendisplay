"""Helpers every CLI command module shares: consoles, errors, device addressing, the spinner."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .device import Device, connect
from .exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    OpenDisplayError,
)
from .sec2.keystore import DEFAULT_PATH, JsonFileKeyStore

_T = TypeVar("_T")

_console = Console(stderr=True)  # status, spinners, tables, errors → stderr
_stdout = Console()  # structured data (--json output) → stdout


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _error(msg: str) -> NoReturn:
    """Print a colored error to stderr and exit with code 1."""
    _console.print(f"[bold red]Error:[/bold red] {msg}")
    sys.exit(1)


def _handle_ble_error(exc: OpenDisplayError) -> NoReturn:
    """Translate a device exception into a user-facing error message and exit."""
    if isinstance(exc, AuthenticationRequiredError):
        _error("Device requires an encryption key. Pass --key HEX.")
    if isinstance(exc, AuthenticationFailedError):
        _error("Authentication failed. Check that --key is correct.")
    if isinstance(exc, BLETimeoutError):
        _error(f"BLE timeout: {exc}")
    if isinstance(exc, BLEConnectionError):
        _error(f"BLE connection failed: {exc}")
    _error(f"Device error: {exc}")


def _parse_hex_key(hex_str: str | None) -> bytes | None:
    """Convert hex string to 16-byte AES key, or None if not provided."""
    if hex_str is None:
        return None
    cleaned = hex_str.strip().replace(" ", "").replace(":", "")
    if len(cleaned) != 32:
        _error(f"--key must be exactly 32 hex characters (16 bytes), got {len(cleaned)}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        _error(f"--key contains invalid hex characters: {exc}")


def _device_kwargs(
    device: str | None,
    key: bytes | None,
    timeout: float,
    host: str | None = None,
    port: int | None = None,
    tls: bool = False,
    key_store: str | None = None,
) -> dict[str, Any]:
    """Build OpenDisplayDevice constructor kwargs from CLI args (plus ``key_store`` for ``_open_device``).

    With ``--host`` the device is addressed over TCP/LAN (WiFi). Otherwise a
    ``--device`` MAC address (contains ':') / macOS UUID (36-char, 4 dashes) or a
    human-readable device name is used over BLE.
    """
    kwargs: dict[str, Any] = {"timeout": timeout, "encryption_key": key}
    if key_store is not None:
        kwargs["key_store"] = key_store
    if host is not None:
        kwargs["host"] = host
        if port is not None:
            kwargs["port"] = port
        kwargs["tls"] = tls
        return kwargs
    if not device:
        _error("Provide --device (BLE) or --host (WiFi/LAN)")
    if ":" in device or (len(device) == 36 and device.count("-") == 4):
        kwargs["mac_address"] = device
    else:
        kwargs["device_name"] = device
    return kwargs


def _add_key_store_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--key-store",
        metavar="PATH",
        default=str(DEFAULT_PATH),
        help=f"Where firmware 3.0 device keys are kept (default: {DEFAULT_PATH})",
    )


def _connect_args(device_kwargs: dict[str, Any]) -> dict[str, Any]:
    """``_device_kwargs`` as ``opendisplay.connect()`` takes them, with the key store opened."""
    names = {"mac_address": "address", "device_name": "name"}
    out = {names.get(key, key): value for key, value in device_kwargs.items()}
    if isinstance(out.get("key_store"), (str, Path)):
        out["key_store"] = JsonFileKeyStore(Path(out["key_store"]))
    return out


def _open_device(device_kwargs: dict[str, Any]) -> Device:
    """Connect through ``opendisplay.connect()``: SEC2 on a claimed firmware 3.0 display, else as before.

    The commands use ``dev.legacy``, the ``OpenDisplayDevice`` inside, for what the new surface
    does not cover yet.
    """
    return connect(**_connect_args(device_kwargs))


def _add_device_options(parser: argparse.ArgumentParser) -> None:
    """Add shared device-addressing (--device / --host) + --key/--timeout options."""
    parser.add_argument("--device", metavar="ADDR", help="Device MAC address or name (BLE)")
    parser.add_argument("--host", default=None, metavar="IP", help="Device IP/hostname (WiFi/LAN TCP transport)")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="TCP port for --host (default: 2446 plaintext / 2447 TLS)",
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        help="Use TLS-PSK for the --host connection",
    )
    parser.add_argument("--key", default=None, metavar="HEX", help="Encryption key as 32 hex characters")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Fail at once if the display does not answer, instead of allowing about 60 s for it to finish redrawing",
    )
    _add_key_store_option(parser)
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECS",
        help="Connection timeout in seconds (default: 10.0)",
    )


def _spinner() -> Progress:
    """Return a transient spinner Progress (disappears when its context exits)."""
    return Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True, console=_console)
