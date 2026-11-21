"""`opendisplay raw`: send one command frame and print the reply (a debugging aid).

The frame is ``[cmd:2 big-endian][payload]``. On a device with a session it goes out encrypted
like any other command. The reply is printed as it arrived; nothing checks or interprets it, and
nothing stops a command that changes or erases the device.
"""

from __future__ import annotations

import argparse
import re

from .cli_common import _add_device_options, _console, _handle_ble_error, _run
from .cli_sec2 import _connect_kwargs
from .device import connect
from .exceptions import OpenDisplayError


def _command(text: str) -> int:
    value = int(text, 16) if re.fullmatch(r"(0x)?[0-9a-fA-F]{1,4}", text) else -1
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"{text!r} is not a 2-byte hex command such as 0045")
    return value


def _payload(text: str) -> bytes:
    try:
        return bytes.fromhex(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"{text!r} is not hex bytes") from err


def add_raw_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``raw``."""
    p = subparsers.add_parser("raw", help="Debug: send one command frame and print the reply as hex")
    _add_device_options(p)
    p.add_argument("--cmd", required=True, type=_command, metavar="HEX", help="The command code, e.g. 0045")
    p.add_argument("--payload", type=_payload, default=b"", metavar="HEX", help="Bytes after the command code")
    p.add_argument("--no-reply", action="store_true", help="Send and wait for nothing (e.g. a reboot)")
    p.add_argument("--reply-timeout", type=float, default=10.0, metavar="S", help="How long to wait for the reply")
    p.set_defaults(func=lambda args: _run(_raw(args)))


async def _raw(args: argparse.Namespace) -> None:
    frame = args.cmd.to_bytes(2, "big") + args.payload
    try:
        async with connect(**_connect_kwargs(args)) as dev:
            reply = await dev.legacy.send_raw(frame, reply=not args.no_reply, timeout=args.reply_timeout)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print(f"sent  {frame.hex(' ')}")
    if reply is not None:
        print(f"reply {reply.hex(' ')}")
