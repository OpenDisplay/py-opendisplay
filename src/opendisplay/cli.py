"""Command-line interface for py-opendisplay."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from epaper_dithering import DitherMode
from PIL import Image, UnidentifiedImageError
from rich.live import Live
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TaskProgressColumn, TextColumn
from rich.table import Table

from . import redraw
from .battery import voltage_to_percent
from .cli_common import (
    _add_device_options,
    _console,
    _device_kwargs,
    _error,
    _handle_ble_error,
    _open_device,
    _parse_hex_key,
    _run,
    _spinner,
    _stdout,
)
from .cli_drive import cmd_upload_mode
from .cli_info import _build_info_tree, _info_to_json, _InfoContext
from .cli_migrate import add_backup_config_parser, add_migrate_parser
from .cli_sec2 import add_sec2_parsers
from .cli_setup import add_setup_parsers
from .device import OpenDisplayDevice
from .discovery import discover_devices_with_adv
from .exceptions import (
    BLEConnectionError,
    BLETimeoutError,
    IdentNotSupportedError,
    OpenDisplayError,
)
from .models.advertisement import AdvertisementData
from .models.config import GlobalConfig
from .models.enums import (
    CapacityEstimator,
    ColorScheme,
    FitMode,
    ICType,
    RefreshMode,
    Rotation,
)
from .models.firmware import FirmwareVersion
from .models.ident import DeviceIdent
from .partial import PartialState
from .sec2.wire import SecStatus
from .sensors import SensorReading

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


_DITHER_CHOICES: dict[str, DitherMode] = {m.name.lower().replace("_", "-"): m for m in DitherMode}
_REFRESH_CHOICES: dict[str, RefreshMode] = {m.name.lower(): m for m in RefreshMode}
_FIT_CHOICES: dict[str, FitMode] = {m.name.lower(): m for m in FitMode}
_ROTATE_CHOICES: dict[str, Rotation] = {
    "0": Rotation.ROTATE_0,
    "90": Rotation.ROTATE_90,
    "180": Rotation.ROTATE_180,
    "270": Rotation.ROTATE_270,
}


def _parse_compression_value(flag: str, value: str) -> float:
    """Parse a 0.0–1.0 strength such as --tone or --gamut."""
    try:
        f = float(value)
    except ValueError:
        _error(f"{flag} must be a number from 0.0 to 1.0, got {value!r}")
    if not 0.0 <= f <= 1.0:
        _error(f"{flag} must be a number from 0.0 to 1.0, got {value!r}")
    return f


def _setup_logging(verbose: bool) -> None:
    """Configure root logging with RichHandler."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=_console, rich_tracebacks=True)],
        force=True,
    )
    logging.getLogger("bleak").setLevel(logging.INFO)
    logging.getLogger("PIL").setLevel(logging.INFO)


# ── scan ──────────────────────────────────────────────────────────────────────


def _add_scan_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("scan", help="Scan for nearby OpenDisplay devices (BLE, or --lan for WiFi/mDNS)")
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECS",
        help="Scan duration in seconds (default: 10.0)",
    )
    p.add_argument("--lan", action="store_true", help="Discover WiFi devices via mDNS instead of BLE")
    p.add_argument("--json", dest="output_json", action="store_true", help="Output results as JSON")
    p.set_defaults(func=_cmd_scan)


def _cmd_scan(args: argparse.Namespace) -> None:
    if args.lan:
        _run(_scan_lan(args.timeout, args.output_json))
    else:
        _run(_scan(args.timeout, args.output_json))


async def _scan_lan(timeout: float, output_json: bool) -> None:
    from .discovery_ip import discover_ip_devices

    with _spinner() as progress:
        progress.add_task(f"Browsing mDNS for {timeout:.0f}s...", total=None)
        try:
            devices = await discover_ip_devices(scan_seconds=timeout)
        except RuntimeError as exc:
            _error(str(exc))

    if output_json:
        rows = [
            {
                "name": info.name,
                "host": info.host,
                "port": info.port,
                "mac": info.mac,
                "tls": info.tls,
                "fw": info.fw,
                "cm": info.cm,
            }
            for _, info in sorted(devices.items())
        ]
        _stdout.print_json(json.dumps({"devices": rows}))
        return

    if not devices:
        _console.print("No WiFi OpenDisplay devices found.")
        return

    table = Table(show_header=True)
    table.add_column("Name")
    table.add_column("Host")
    table.add_column("Port")
    table.add_column("MAC")
    table.add_column("TLS")
    for _, info in sorted(devices.items()):
        table.add_row(info.name, info.host, str(info.port), info.mac or "—", "yes" if info.tls else "no")
    _console.print(table)


def _scan_state(adv: AdvertisementData | None) -> str:
    """Describe what an advertisement says about the device's claim and config state.

    "key required" is status bit 3, which firmware 3.0 sets for a claimed device. 2.x firmware
    does not set it reliably, so an encrypted 2.x device can show no state here.
    """
    if adv is None:
        return ""
    states = []
    if adv.awaiting_claim:
        states.append("awaiting claim")
    if adv.session_required:
        states.append("key required")
    if adv.unconfigured:
        states.append("unconfigured")
    return ", ".join(states)


async def _scan(timeout: float, output_json: bool) -> None:
    with _spinner() as progress:
        progress.add_task(f"Scanning for {timeout:.0f}s...", total=None)
        try:
            devices = await discover_devices_with_adv(timeout=timeout)
        except OpenDisplayError as exc:
            _error(str(exc))

    if output_json:
        rows = []
        for name, (mac, adv) in sorted(devices.items()):
            rows.append(
                {
                    "name": name,
                    "mac": mac,
                    "battery_mv": adv.battery_mv if adv else None,
                    "temperature_c": adv.temperature_c if adv else None,
                    "state": _scan_state(adv),
                }
            )
        _stdout.print_json(json.dumps({"devices": rows}))
        return

    if not devices:
        _console.print("No OpenDisplay devices found.")
        return

    table = Table(show_header=True)
    table.add_column("Name")
    table.add_column("MAC")
    table.add_column("Battery")
    table.add_column("Temp")
    table.add_column("State")
    for name, (mac, adv) in sorted(devices.items()):
        if adv:
            pct = voltage_to_percent(adv.battery_mv, CapacityEstimator.LI_ION)
            battery_str = f"{pct}% ({adv.battery_mv} mV)" if pct is not None else f"{adv.battery_mv} mV"
            temp_str = f"{adv.temperature_c:.0f} °C"
        else:
            battery_str = "\u2014"
            temp_str = "\u2014"
        table.add_row(name, mac, battery_str, temp_str, _scan_state(adv))
    _console.print(table)


# ── info ──────────────────────────────────────────────────────────────────────


def _add_info_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("info", help="Read and display device information")
    _add_device_options(p)
    p.add_argument("--json", dest="output_json", action="store_true", help="Output as JSON")
    p.set_defaults(func=_cmd_info)


def _cmd_info(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(
        _info(
            _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls, args.key_store),
            args.output_json,
        )
    )


async def _read_sensors(device: OpenDisplayDevice, progress: Progress, task: TaskID) -> dict[int, SensorReading]:
    """Read live sensor values, keyed by instance number.

    Best-effort: firmware too old for READ_MSD (0x0044) still reports its sensor
    hardware, just without values, so a failure here degrades the report rather
    than failing the command.
    """
    if not device.config or not device.config.sensors:
        return {}

    progress.update(task, description="Reading sensors...")
    try:
        readings = await device.read_sensors()
    except OpenDisplayError as exc:
        _LOGGER.debug("Could not read sensor values: %s", exc)
        return {}
    return {reading.instance_number: reading for reading in readings}


async def _read_ident(device: OpenDisplayDevice, fw: FirmwareVersion) -> DeviceIdent | None:
    """Read the device identity where the firmware can have it.

    Firmware 3.0 answers READ_IDENT, and so do config-driven EFR32BG22 builds before it.
    Other 2.x firmware stays silent on the command, so it is not asked and costs no timeout.
    """
    ic_type = device.config.system.ic_type if device.config and device.config.system else None
    if fw["major"] < 3 and ic_type != ICType.EFR32BG22:
        return None
    try:
        return await device.read_ident()
    except IdentNotSupportedError:
        return None
    except OpenDisplayError as exc:
        # The identity is an extra: a malformed reply must not cost the rest of the report.
        _LOGGER.warning("Could not read the device identity: %s", exc)
        return None


async def _read_sec_status(device: OpenDisplayDevice, fw: FirmwareVersion) -> SecStatus | None:
    """Read the security state on firmware 3.0; 2.x does not know the command and is not asked."""
    if fw["major"] < 3:
        return None
    try:
        return await device.read_sec_status()
    except OpenDisplayError as exc:
        # The security state is an extra: a missing or broken answer must not cost the report.
        _LOGGER.warning("Could not read the security state: %s", exc)
        return None


async def _info_with_config(device_kwargs: dict[str, Any]) -> _InfoContext:
    """Gather the report: the full one with a stored config, firmware and identity without."""
    with _spinner() as progress:
        task = progress.add_task("Connecting...", total=None)
        async with _open_device(device_kwargs) as dev:
            device = dev.legacy
            progress.update(task, description="Reading info...")
            fw = await device.read_firmware_version()
            config = device.config
            if config is None:
                return await _context_without_config(device, fw)
            display = config.displays[0] if config and config.displays else None
            return _InfoContext(
                mac=device.mac_address,
                device_name=device.device_name,
                fw=fw,
                config=config,
                width=device.width,
                height=device.height,
                color_scheme_name=device.color_scheme.name,
                rotation=display.rotation_enum if display else device.rotation,
                board_type_name=device.get_board_type_name() if config else None,
                sensor_readings=await _read_sensors(device, progress, task),
                ident=await _read_ident(device, fw),
                sec_status=await _read_sec_status(device, fw),
            )


async def _context_without_config(device: OpenDisplayDevice, fw: FirmwareVersion) -> _InfoContext:
    """What a device with no stored config can still say: firmware and identity.

    Display size and colours come from the identity's first display record when the firmware
    reports one (devices with factory data do), otherwise they are left out.
    """
    ident = await _read_ident(device, fw)
    record = ident.displays[0] if ident and ident.displays else None
    try:
        scheme_name = ColorScheme.from_value(record.color_scheme).name if record else ""
    except ValueError:
        scheme_name = ""
    return _InfoContext(
        mac=device.mac_address,
        device_name=device.device_name,
        fw=fw,
        config=None,
        width=record.pixel_width if record else 0,
        height=record.pixel_height if record else 0,
        color_scheme_name=scheme_name,
        rotation=0,
        board_type_name=None,
        ident=ident,
        sec_status=await _read_sec_status(device, fw),
    )


async def _info(device_kwargs: dict[str, Any], output_json: bool) -> None:
    try:
        ctx = await _info_with_config(device_kwargs)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    if ctx.config is None:
        _console.print("[yellow]The device has no stored configuration; showing firmware and identity only.[/yellow]")

    if output_json:
        _stdout.print_json(json.dumps(_info_to_json(ctx)))
        return

    _console.print(_build_info_tree(ctx))


# ── upload ────────────────────────────────────────────────────────────────────


def _load_partial_state(path: str) -> PartialState:
    """Load PartialState from path, or return a fresh one if the file is absent."""
    p = Path(path)
    if not p.exists():
        return PartialState()
    try:
        return PartialState.from_bytes(p.read_bytes())
    except (OSError, ValueError) as exc:
        _error(f"Failed to load --state-file {path}: {exc}")


def _save_partial_state(path: str, state: PartialState) -> None:
    """Atomically write PartialState to path (write to .tmp then rename)."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(state.to_bytes())
    os.replace(tmp, p)


def _add_upload_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("upload", help="Upload an image to the device")
    _add_device_options(p)
    p.add_argument("image", metavar="IMAGE_PATH", help="Path to the image file")
    p.add_argument(
        "--refresh-mode",
        choices=list(_REFRESH_CHOICES),
        default="full",
        help="Display refresh mode (default: full)",
    )
    p.add_argument(
        "--dither-mode",
        choices=list(_DITHER_CHOICES),
        default="burkes",
        help="Dithering algorithm (default: burkes)",
    )
    p.add_argument(
        "--fit",
        choices=list(_FIT_CHOICES),
        default="contain",
        help="Image fit strategy (default: contain)",
    )
    p.add_argument(
        "--rotate",
        choices=list(_ROTATE_CHOICES),
        default="0",
        help="Additional image rotation in degrees on top of device config (default: 0)",
    )
    p.add_argument("--no-compress", action="store_true", help="Disable zlib compression")
    p.add_argument("--no-serpentine", action="store_true", help="Disable serpentine scan direction")
    p.add_argument("--exposure", type=float, default=1.0, metavar="VALUE", help="Exposure multiplier (default: 1.0)")
    p.add_argument(
        "--saturation", type=float, default=1.0, metavar="VALUE", help="Saturation multiplier (default: 1.0)"
    )
    p.add_argument("--shadows", type=float, default=0.0, metavar="VALUE", help="Shadow lift 0.0–1.0 (default: 0.0)")
    p.add_argument(
        "--highlights", type=float, default=0.0, metavar="VALUE", help="Highlight rolloff 0.0–1.0 (default: 0.0)"
    )
    p.add_argument(
        "--tone",
        default="0",
        metavar="VALUE",
        help="Black-point lift, 0.0–1.0 (default: 0)",
    )
    p.add_argument(
        "--gamut",
        default="0",
        metavar="VALUE",
        help="Gamut compression, 0.0–1.0 (default: 0)",
    )
    p.add_argument(
        "--state-file",
        metavar="PATH",
        default=None,
        help="Persistent partial-rendering state file. If the file exists, it's loaded and "
        "the upload attempts a partial transfer; on success the file is rewritten. If it "
        "does not exist, a fresh state is created (forcing a full upload first time).",
    )
    p.add_argument(
        "--mode",
        metavar="NAME",
        default=None,
        help="Upload in a drive mode the device's config declares, e.g. colour16 (firmware 3.0; see `info`)",
    )
    p.set_defaults(func=_cmd_upload)


def _cmd_upload(args: argparse.Namespace) -> None:
    if args.mode is not None:
        cmd_upload_mode(
            args,
            _REFRESH_CHOICES[args.refresh_mode],
            _DITHER_CHOICES[args.dither_mode],
            _FIT_CHOICES[args.fit],
            _ROTATE_CHOICES[args.rotate],
        )
        return
    key = _parse_hex_key(args.key)
    tone = _parse_compression_value("--tone", args.tone)
    gamut = _parse_compression_value("--gamut", args.gamut)
    _run(
        _upload(
            _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls, args.key_store),
            args.image,
            _REFRESH_CHOICES[args.refresh_mode],
            _DITHER_CHOICES[args.dither_mode],
            _FIT_CHOICES[args.fit],
            _ROTATE_CHOICES[args.rotate],
            not args.no_compress,
            not args.no_serpentine,
            args.exposure,
            args.saturation,
            args.shadows,
            args.highlights,
            tone,
            gamut,
            args.state_file,
        )
    )


async def _upload(
    device_kwargs: dict[str, Any],
    image_path: str,
    refresh_mode: RefreshMode,
    dither_mode: DitherMode,
    fit: FitMode,
    rotate: Rotation,
    compress: bool,
    serpentine: bool,
    exposure: float,
    saturation: float,
    shadows: float,
    highlights: float,
    tone: float,
    gamut: float,
    state_file: str | None,
) -> None:
    try:
        image = Image.open(image_path)
    except FileNotFoundError:
        _error(f"Image file not found: {image_path}")
    except UnidentifiedImageError:
        _error(f"Cannot open image (unsupported format): {image_path}")

    try:
        # Two separate Progress instances so the bar row has no leading columns
        # and starts at the left edge of the terminal.
        spinner_progress = Progress(
            SpinnerColumn(finished_text="[green]✓[/green]"),
            TextColumn("{task.description}"),
            console=_console,
        )
        bar_progress = Progress(
            BarColumn(),
            TaskProgressColumn(),
            console=_console,
        )

        class _Display:  # pylint: disable=too-few-public-methods
            # Render spinner always; bar only when it has a visible task.
            def __rich_console__(self, _con, _opts):  # type: ignore[no-untyped-def]
                yield spinner_progress
                if any(t.visible for t in bar_progress.tasks):
                    yield bar_progress

        with Live(_Display(), console=_console, refresh_per_second=10, transient=False):
            connect_task = spinner_progress.add_task("Connecting...", total=None)
            upload_task = spinner_progress.add_task("Uploading...", total=None, visible=False)
            refresh_task = spinner_progress.add_task("Refreshing display...", total=None, visible=False)
            bar_task = bar_progress.add_task("", total=None, visible=False)

            async with _open_device(device_kwargs) as dev:
                device = dev.legacy
                spinner_progress.update(connect_task, visible=False)
                spinner_progress.update(upload_task, visible=True)
                bar_progress.update(bar_task, visible=True)

                def on_progress(sent: int, total: int) -> None:
                    bar_progress.update(bar_task, total=total, completed=sent)
                    if sent == total:
                        bar_progress.update(bar_task, visible=False)
                        spinner_progress.update(upload_task, visible=False)
                        spinner_progress.update(refresh_task, visible=True)

                state = _load_partial_state(state_file) if state_file else None

                await device.upload_image(
                    image,
                    refresh_mode=refresh_mode,
                    dither_mode=dither_mode,
                    compress=compress,
                    serpentine=serpentine,
                    exposure=exposure,
                    saturation=saturation,
                    shadows=shadows,
                    highlights=highlights,
                    tone=tone,
                    gamut=gamut,
                    fit=fit,
                    rotate=rotate,
                    progress_callback=on_progress,
                    state=state,
                )

                if state_file and state is not None:
                    _save_partial_state(state_file, state)

            spinner_progress.update(refresh_task, visible=False)
            spinner_progress.update(upload_task, visible=True, description="[green]Done.[/green]", total=1, completed=1)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)


# ── reboot ────────────────────────────────────────────────────────────────────


def _add_reboot_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("reboot", help="Reboot the device")
    _add_device_options(p)
    p.set_defaults(func=_cmd_reboot)


def _cmd_reboot(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(_reboot(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls, args.key_store)))


async def _reboot(device_kwargs: dict[str, Any]) -> None:
    rebooted = False
    with _spinner() as progress:
        progress.add_task("Connecting...", total=None)
        try:
            async with _open_device(device_kwargs) as dev:
                device = dev.legacy
                await device.reboot()
                rebooted = True
        except (BLEConnectionError, BLETimeoutError):
            if not rebooted:
                _error("BLE connection failed before reboot command could be sent.")
            # else: expected drop after reboot
        except OpenDisplayError as exc:
            _handle_ble_error(exc)
    _console.print("Reboot command sent. Device will restart.")


# ── sleep ─────────────────────────────────────────────────────────────────────


def _add_sleep_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("sleep", help="Put the device into deep sleep (command 0x0052)")
    _add_device_options(p)
    p.set_defaults(func=_cmd_sleep)


def _cmd_sleep(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(_sleep(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls, args.key_store)))


async def _sleep(device_kwargs: dict[str, Any]) -> None:
    slept = False
    with _spinner() as progress:
        progress.add_task("Connecting...", total=None)
        try:
            async with _open_device(device_kwargs) as dev:
                device = dev.legacy
                await device.deep_sleep()
                slept = True
        except (BLEConnectionError, BLETimeoutError):
            if not slept:
                _error("BLE connection failed before deep sleep command could be sent.")
            # else: expected drop after the device sleeps
        except OpenDisplayError as exc:
            _handle_ble_error(exc)
    _console.print("Deep sleep command sent. Device will sleep until its next wake.")


# ── export-config ─────────────────────────────────────────────────────────────


def _default_export_path(device: str) -> str:
    """Derive a default filename from the device identifier."""
    return f"opendisplay_{device.replace(':', '').lower()}.json"


def _add_export_config_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("export-config", help="Export device configuration to a JSON file")
    _add_device_options(p)
    p.add_argument(
        "output",
        metavar="OUTPUT_PATH",
        nargs="?",
        default=None,
        help="Path to write the JSON config file (default: opendisplay_<device>.json)",
    )
    p.set_defaults(func=_cmd_export_config)


def _cmd_export_config(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    output = args.output or _default_export_path(args.device)
    _run(
        _export_config(
            _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls, args.key_store), output
        )
    )


async def _export_config(device_kwargs: dict[str, Any], output_path: str) -> None:
    try:
        with _spinner() as progress:
            task = progress.add_task("Connecting...", total=None)
            async with _open_device(device_kwargs) as dev:
                device = dev.legacy
                progress.update(task, description="Reading config...")
                device.export_config_json(output_path)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print(f"Config exported to [bold]{output_path}[/bold]")


# ── write-config ──────────────────────────────────────────────────────────────


def _add_write_config_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("write-config", help="Write configuration from a JSON file to the device")
    _add_device_options(p)
    p.add_argument("input", metavar="INPUT_PATH", help="Path to the JSON config file")
    unknown = p.add_mutually_exclusive_group()
    unknown.add_argument(
        "--keep-unknown-packets",
        dest="unknown_packets",
        action="store_const",
        const="keep",
        help="If the device has config packets this tool does not recognise and the file has none, "
        "carry the device's over unchanged",
    )
    unknown.add_argument(
        "--discard-unknown-packets",
        dest="unknown_packets",
        action="store_const",
        const="discard",
        help="Write the file as it is, deleting config packets this tool does not recognise from the device",
    )
    p.set_defaults(func=_cmd_write_config)


def _cmd_write_config(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    device_kwargs = _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls, args.key_store)
    _run(_write_config(device_kwargs, args.input, args.unknown_packets))


def _packets_lost_by_writing(on_device: GlobalConfig, config: GlobalConfig) -> dict[str, Any]:
    """The verbatim-carried packets the device has and ``config`` lacks, by ``GlobalConfig`` field.

    Three kinds of packet are carried verbatim instead of being modelled: the length-prefixed
    range, FindMy, and the unparsed tail. A JSON file that passed through a tool that does not
    know the field holding one of them has lost that kind, so each is checked separately: a
    file that carries one kind must not hide that it would delete another.
    """
    lost: dict[str, Any] = {}
    if on_device.length_prefixed_packets and not config.length_prefixed_packets:
        lost["length_prefixed_packets"] = list(on_device.length_prefixed_packets)
    if on_device.findmy_config_raw and not config.findmy_config_raw:
        lost["findmy_config_raw"] = on_device.findmy_config_raw
    if on_device.unparsed_tail and not config.unparsed_tail:
        lost["unparsed_tail"] = on_device.unparsed_tail
    return lost


def _lost_packet_bytes(lost: dict[str, Any]) -> int:
    packets = lost.get("length_prefixed_packets", [])
    return (
        sum(len(packet.to_bytes()) for packet in packets)
        + len(lost.get("findmy_config_raw", b""))
        + len(lost.get("unparsed_tail", b""))
    )


_UNKNOWN_PACKETS_REFUSAL = (
    "The device holds {size} bytes of config packets that the file does not carry. "
    "Writing it would delete them; on some firmware they hold the panel drive data. "
    "Pass --keep-unknown-packets to carry them over unchanged, or --discard-unknown-packets to delete them."
)


async def _write_config(device_kwargs: dict[str, Any], input_path: str, unknown_packets: str | None = None) -> None:
    """Write a JSON config file to the device.

    A file that went through a tool unaware of ``length_prefixed_packets`` and
    ``unparsed_tail`` no longer has the packets they carry verbatim. If the
    device has such packets and the file has none, ``unknown_packets`` decides:
    ``"keep"`` carries the device's over, ``"discard"`` writes the file as it
    is, and ``None`` refuses.
    """
    try:
        config = OpenDisplayDevice.import_config_json(input_path)
    except FileNotFoundError:
        _error(f"Config file not found: {input_path}")
    except (OSError, ValueError, KeyError) as exc:
        _error(f"Cannot read config file: {exc}")

    refusal = None
    kept = 0
    try:
        with _spinner() as progress:
            task = progress.add_task("Connecting...", total=None)
            async with _open_device(device_kwargs) as dev:
                device = dev.legacy
                on_device = device.config
                lost = _packets_lost_by_writing(on_device, config) if on_device else {}
                if lost:
                    at_risk = _lost_packet_bytes(lost)
                    if unknown_packets == "keep":
                        config = replace(config, **lost)
                        kept = at_risk
                    elif unknown_packets != "discard":
                        refusal = _UNKNOWN_PACKETS_REFUSAL.format(size=at_risk)
                if refusal is None:
                    progress.update(task, description="Writing config...")
                    await device.write_config(config)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    if refusal is not None:
        _error(refusal)
    if kept:
        _console.print(f"Kept {kept} bytes of config packets from the device that the file does not carry.")
    _console.print("Config written [green]successfully[/green].")


# ── entry point ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="opendisplay",
        description="OpenDisplay BLE command-line tool",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_scan_parser(subparsers)
    _add_info_parser(subparsers)
    _add_upload_parser(subparsers)
    _add_reboot_parser(subparsers)
    _add_sleep_parser(subparsers)
    _add_export_config_parser(subparsers)
    _add_write_config_parser(subparsers)
    add_backup_config_parser(subparsers)
    add_migrate_parser(subparsers)
    add_sec2_parsers(subparsers)
    add_setup_parsers(subparsers)
    return parser


_WAITING_LINE = "Waiting for the display to finish redrawing..."


def _prepare(args: argparse.Namespace) -> None:
    """Allow for a redraw: a person running the CLI has often just touched the display.

    The target is noted as possibly redrawing, so connects and replies wait up to
    ``redraw.REDRAW_ALLOWANCE_S`` for it, and one line says so when a wait actually happens.
    ``--no-wait`` keeps the library's fast failure.
    """
    if getattr(args, "no_wait", True):
        return
    redraw.mark_redrawing(getattr(args, "device", None), getattr(args, "host", None))
    told: list[bool] = []

    def tell() -> None:
        if not told:
            told.append(True)
            _console.print(_WAITING_LINE)

    redraw.waiting_hook = tell


def main() -> None:
    """Entry point for the opendisplay CLI."""
    args = _build_parser().parse_args()
    _setup_logging(args.verbose)
    _prepare(args)
    args.func(args)


if __name__ == "__main__":
    main()
