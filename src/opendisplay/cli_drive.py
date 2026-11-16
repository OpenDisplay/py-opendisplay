"""`opendisplay upload --mode NAME`: an upload in a config-declared drive mode (firmware 3.0)."""

from __future__ import annotations

import argparse
from typing import Any

from epaper_dithering import DitherMode
from PIL import Image, UnidentifiedImageError

from .cli_common import _console, _device_kwargs, _error, _handle_ble_error, _parse_hex_key, _run
from .device import connect
from .exceptions import OpenDisplayError
from .models.enums import FitMode, RefreshMode, Rotation


def refused_options(args: argparse.Namespace) -> list[str]:
    """Upload options a drive-mode upload cannot honour, by flag name."""
    refused = []
    if args.state_file:
        refused.append("--state-file")
    if args.refresh_mode == "partial":
        refused.append("--refresh-mode partial")
    for flag, value, default in (
        ("--shadows", args.shadows, 0.0),
        ("--highlights", args.highlights, 0.0),
        ("--tone", float(args.tone) if _is_number(args.tone) else args.tone, 0.0),
        ("--gamut", float(args.gamut) if _is_number(args.gamut) else args.gamut, 0.0),
    ):
        if value != default:
            refused.append(flag)
    return refused


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def cmd_upload_mode(  # pylint: disable=too-many-arguments
    args: argparse.Namespace,
    refresh_mode: RefreshMode,
    dither_mode: DitherMode,
    fit: FitMode,
    rotate: Rotation,
) -> None:
    """Run ``upload --mode``: check the options, then connect and draw in the named mode."""
    refused = refused_options(args)
    if refused:
        _error(f"--mode cannot be combined with {', '.join(refused)}")
    kwargs = _device_kwargs(args.device, _parse_hex_key(args.key), args.timeout, args.host, args.port, args.tls)
    options = {
        "refresh_mode": refresh_mode,
        "dither_mode": dither_mode,
        "fit": fit,
        "rotate": rotate,
        "compress": not args.no_compress,
        "serpentine": not args.no_serpentine,
        "exposure": args.exposure,
        "saturation": args.saturation,
    }
    _run(_upload_mode(kwargs, args.image, args.mode, options))


async def _upload_mode(device_kwargs: dict[str, Any], image_path: str, mode: str, options: dict[str, Any]) -> None:
    try:
        image = Image.open(image_path)
    except FileNotFoundError:
        _error(f"Image file not found: {image_path}")
    except UnidentifiedImageError:
        _error(f"Cannot open image (unsupported format): {image_path}")
    names = {"mac_address": "address", "device_name": "name"}
    try:
        async with connect(**{names.get(k, k): v for k, v in device_kwargs.items()}) as dev:
            try:
                drive_mode = dev.display.drive_mode(mode)
            except ValueError as exc:
                _error(str(exc))
            if dev.display.nominal_colours(drive_mode):
                _console.print(
                    f"Drive mode '{drive_mode.name}' uses nominal colours, not colours measured on this panel."
                )
            await dev.display.show(image, mode=mode, **options)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print(f"Uploaded in drive mode '{mode}'.")
