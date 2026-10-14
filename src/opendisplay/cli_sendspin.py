"""The ``opendisplay sendspin`` subcommand: show what Music Assistant is playing."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
from pathlib import Path
from typing import Any

from .cli_common import (
    _add_device_options,
    _console,
    _device_kwargs,
    _error,
    _handle_ble_error,
    _parse_hex_key,
    _run,
    _spinner,
)
from .device import OpenDisplayDevice
from .exceptions import OpenDisplayError
from .models.config import GlobalConfig
from .now_playing import NowPlaying, prepare_now_playing


def _sendspin_state_dir(device: str) -> Path:
    """Default directory for a device's Sendspin identity and pairing state."""
    return Path.home() / ".config" / "opendisplay" / "sendspin" / device.replace(":", "").lower()


def add_sendspin_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``sendspin`` subcommand."""
    p = subparsers.add_parser(
        "sendspin",
        help="Show what Music Assistant is playing via Sendspin (needs py-opendisplay[sendspin])",
    )
    _add_device_options(p)
    p.add_argument(
        "--url",
        default=None,
        metavar="WS_URL",
        help="Sendspin server, e.g. ws://10.0.0.5:8927/sendspin (default: discover via mDNS)",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        metavar="DIR",
        help="Where the Sendspin identity and pairing state live (default: ~/.config/opendisplay/sendspin/<device>)",
    )
    p.add_argument("--name", default=None, help="Player name shown in Music Assistant (default: OpenDisplay <device>)")
    p.add_argument(
        "--font",
        default=None,
        metavar="FILE",
        help="TTF/OTF font for the track text (default: Pillow's built-in font, which shows accents as boxes)",
    )
    p.set_defaults(func=_cmd_sendspin)


def _cmd_sendspin(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    device_kwargs = _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls)
    label = args.device or args.host
    state_dir = Path(args.state_dir) if args.state_dir else _sendspin_state_dir(label)
    font = Path(args.font).expanduser() if args.font else None
    if font is not None and not font.is_file():
        _error(f"Font file not found: {font}")
    with contextlib.suppress(KeyboardInterrupt):
        _run(_sendspin(device_kwargs, args.url, state_dir, args.name or f"OpenDisplay {label}", font))


async def _upload_now_playing(
    device_kwargs: dict[str, Any], config: GlobalConfig, now: NowPlaying, font: Path | None = None
) -> None:
    """Lay out *now* for the panel and upload it with the best quality it offers.

    Only the cover is dithered, with the panel's measured palette when one exists
    and DBS refinement; the text stays crisp. The frame is prepared before
    connecting, so the device is never held connected (and silent, since it stops
    advertising) while the CPU works.
    """
    display = config.displays[0]
    prepared = await asyncio.to_thread(
        functools.partial(
            prepare_now_playing,
            now,
            config,
            use_measured_palettes=True,
            dbs=True,
            compress=display.supports_zip or display.supports_streaming_decompression,
            font=font,
        )
    )
    async with OpenDisplayDevice(**device_kwargs, config=config) as device:
        await device.upload_prepared_image(prepared)


async def _sendspin(
    device_kwargs: dict[str, Any], url: str | None, state_dir: Path, name: str, font: Path | None
) -> None:
    try:
        from aiosendspin.noise.trust_store import FileClientPairingStore

        from .sendspin import SendspinNowPlayingBridge, load_or_create_identity
    except ImportError as exc:
        _error(str(exc))

    try:
        with _spinner() as progress:
            progress.add_task("Reading device config...", total=None)
            async with OpenDisplayDevice(**device_kwargs) as device:
                config = device.config
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    if config is None or not config.displays:
        _error("Device reported no display configuration.")

    identity = await asyncio.to_thread(load_or_create_identity, state_dir / "identity.key")
    store = await FileClientPairingStore.open(state_dir / "pairing.json")

    async def on_now_playing(now: NowPlaying) -> None:
        label = " - ".join(p for p in (now.artist, now.title) if p) or "new artwork"
        _console.print(f"Now playing: {label}. Uploading...")
        try:
            await _upload_now_playing(device_kwargs, config, now, font)
        except OpenDisplayError as exc:
            _console.print(f"[yellow]Upload failed:[/yellow] {exc}")
            raise
        _console.print("[green]Shown on the display.[/green]")

    bridge = SendspinNowPlayingBridge(
        config,
        name=name,
        identity=identity,
        pairing_store=store,
        on_now_playing=on_now_playing,
        url=url,
    )
    await bridge.start()
    _console.print(f"Waiting for music as [bold]{name}[/bold]. Group it with a speaker in Music Assistant.")
    _console.print("Press Ctrl-C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await bridge.stop()
