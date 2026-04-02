"""Command-line interface for py-opendisplay."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Coroutine
from typing import Any, NoReturn, TypeVar

from epaper_dithering import DitherMode
from PIL import Image, UnidentifiedImageError
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.tree import Tree

from .battery import voltage_to_percent
from .device import OpenDisplayDevice
from .discovery import discover_devices_with_adv
from .exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BLEConnectionError,
    BLETimeoutError,
    OpenDisplayError,
)
from .models.enums import (
    CapacityEstimator,
    FitMode,
    ICType,
    LedType,
    PowerMode,
    RefreshMode,
    Rotation,
    SensorType,
    WifiEncryption,
)

_T = TypeVar("_T")

_console = Console(stderr=True)  # status, spinners, tables, errors → stderr
_stdout = Console()  # structured data (--json output) → stdout

_DITHER_CHOICES: dict[str, DitherMode] = {m.name.lower().replace("_", "-"): m for m in DitherMode}
_REFRESH_CHOICES: dict[str, RefreshMode] = {m.name.lower(): m for m in RefreshMode}
_FIT_CHOICES: dict[str, FitMode] = {m.name.lower(): m for m in FitMode}
_ROTATE_CHOICES: dict[str, Rotation] = {
    "0": Rotation.ROTATE_0,
    "90": Rotation.ROTATE_90,
    "180": Rotation.ROTATE_180,
    "270": Rotation.ROTATE_270,
}


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


def _parse_tone_compression(value: str) -> float | str:
    """Parse tone compression value: 'auto' or a float in [0.0, 1.0]."""
    if value == "auto":
        return "auto"
    try:
        f = float(value)
    except ValueError:
        _error(f'--tone-compression must be "auto" or a float, got {value!r}')
    if not 0.0 <= f <= 1.0:
        _error(f"--tone-compression must be between 0.0 and 1.0, got {f}")
    return f


def _device_kwargs(device: str, key: bytes | None, timeout: float) -> dict[str, Any]:
    """Build OpenDisplayDevice constructor kwargs from CLI args.

    Detects MAC addresses (contains ':') and macOS UUIDs (36-char with 4 dashes)
    vs. human-readable device names.
    """
    kwargs: dict[str, Any] = {"timeout": timeout, "encryption_key": key}
    if ":" in device or (len(device) == 36 and device.count("-") == 4):
        kwargs["mac_address"] = device
    else:
        kwargs["device_name"] = device
    return kwargs


def _add_device_options(parser: argparse.ArgumentParser) -> None:
    """Add shared --device, --key, --timeout options to a subcommand parser."""
    parser.add_argument("--device", required=True, metavar="ADDR", help="Device MAC address or name")
    parser.add_argument("--key", default=None, metavar="HEX", help="Encryption key as 32 hex characters")
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECS",
        help="BLE timeout in seconds (default: 10.0)",
    )


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


_COLOR_SCHEME_STYLES: dict[str, str] = {"R": "red", "Y": "yellow", "G": "green"}


def _color_scheme_label(name: str) -> str:
    """Return a rich-marked-up color scheme name with accent ink colors highlighted."""
    parts = []
    for ch in name:
        style = _COLOR_SCHEME_STYLES.get(ch)
        parts.append(f"[{style}]{ch}[/{style}]" if style else ch)
    return "".join(parts)


def _spinner() -> Progress:
    """Return a transient spinner Progress (disappears when its context exits)."""
    return Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True, console=_console)


# ── scan ──────────────────────────────────────────────────────────────────────


def _add_scan_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("scan", help="Scan for nearby OpenDisplay BLE devices")
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECS",
        help="Scan duration in seconds (default: 10.0)",
    )
    p.add_argument("--json", dest="output_json", action="store_true", help="Output results as JSON")
    p.set_defaults(func=_cmd_scan)


def _cmd_scan(args: argparse.Namespace) -> None:
    _run(_scan(args.timeout, args.output_json))


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
    for name, (mac, adv) in sorted(devices.items()):
        if adv:
            pct = voltage_to_percent(adv.battery_mv, CapacityEstimator.LI_ION)
            battery_str = f"{pct}% ({adv.battery_mv} mV)" if pct is not None else f"{adv.battery_mv} mV"
            temp_str = f"{adv.temperature_c:.0f} °C"
        else:
            battery_str = "\u2014"
            temp_str = "\u2014"
        table.add_row(name, mac, battery_str, temp_str)
    _console.print(table)


# ── info ──────────────────────────────────────────────────────────────────────


def _led_name(led_type: int) -> str:
    try:
        return LedType(led_type).name
    except ValueError:
        return f"0x{led_type:02x}"


def _sensor_name(sensor_type: int) -> str:
    try:
        return SensorType(sensor_type).name
    except ValueError:
        return f"0x{sensor_type:04x}"


def _info_to_json(data: dict[str, Any]) -> dict[str, Any]:
    security = data["security"]
    wifi = data["wifi"]
    enc_enum = wifi.encryption_type_enum if wifi else None
    fw = data["fw"]
    diagonal = data["diagonal"]
    panel_ic_type = data["panel_ic_type"]
    return {
        "mac": data["mac"],
        "display": {
            "width": data["width"],
            "height": data["height"],
            "active_width_mm": data["active_w_mm"],
            "active_height_mm": data["active_h_mm"],
            "diagonal_inches": round(diagonal, 1) if diagonal is not None else None,
            "color_scheme": data["color_str"],
            "rotation": data["rotation"],
            "panel_ic_type": f"0x{panel_ic_type:04x}" if panel_ic_type is not None else None,
            "full_update_mc": data["full_update_mc"],
            "transmission_modes": data["transmission_modes"],
        },
        "hardware": {
            "ic": data["ic_str"],
            "manufacturer": data["mfr_name"],
            "board_type": data["board_type_name"],
            "board_revision": data["board_revision"],
            "leds": [{"instance": led.instance_number, "type": _led_name(led.led_type)} for led in data["leds"]],
            "sensors": [
                {"instance": s.instance_number, "type": _sensor_name(s.sensor_type), "bus": s.bus_id}
                for s in data["sensors"]
            ],
            "buttons": [{"instance": b.instance_number, "input_type": b.input_type} for b in data["binary_inputs"]],
        },
        "power": {
            "mode": data["power_mode_str"],
            "battery_mah": data["battery_mah"],
            "chemistry": data["cap_str"],
            "sleep_timeout_s": data["sleep_timeout_ms"] / 1000 if data["sleep_timeout_ms"] else None,
            "deep_sleep_time_s": data["deep_sleep_time_s"] or None,
            "deep_sleep_current_ua": data["deep_sleep_ua"] or None,
            "tx_power_dbm": data["tx_power"],
        },
        "security": {
            "encryption": security.encryption_enabled_flag,
            "session_timeout_s": security.session_timeout_seconds or None,
            "rewrite_allowed": security.rewrite_allowed,
        }
        if security
        else None,
        "wifi": {
            "ssid": wifi.ssid_text,
            "server": f"{wifi.server_url_text}:{wifi.server_port}" if wifi.server_url_text else None,
            "encryption": enc_enum.name
            if isinstance(enc_enum, WifiEncryption)
            else (f"0x{enc_enum:02x}" if enc_enum is not None else None),
        }
        if wifi and wifi.ssid_text
        else None,
        "firmware": {"major": fw["major"], "minor": fw["minor"], "sha": fw["sha"]},
    }


def _build_info_tree(data: dict[str, Any]) -> Tree:
    mac = data["mac"]
    device_name = data["device_name"]
    fw = data["fw"]
    security = data["security"]
    wifi = data["wifi"]

    root_label = f"{device_name} ({mac})" if device_name else mac
    tree = Tree(root_label, guide_style="cyan dim")

    disp = tree.add("[bold]Display[/bold]")
    disp.add(f"Resolution    {data['width']}x{data['height']}px")
    if data["active_w_mm"] and data["active_h_mm"]:
        diag_suffix = f' ({data["diagonal"]:.1f}")' if data["diagonal"] is not None else ""
        disp.add(f"Physical      {data['active_w_mm']}x{data['active_h_mm']} mm{diag_suffix}")
    disp.add(f"Color         {_color_scheme_label(data['color_str'])}")
    disp.add(f"Rotation      {data['rotation']}°")
    if data["panel_ic_type"] is not None:
        disp.add(f"Panel         0x{data['panel_ic_type']:04x}")
    if data["full_update_mc"]:
        disp.add(f"Full update   {data['full_update_mc']} mC")
    if data["transmission_modes"]:
        disp.add(f"Transmission  {' '.join(data['transmission_modes'])}")

    hw = tree.add("[bold]Hardware[/bold]")
    hw.add(f"MCU           {data['ic_str']}")
    board_str = f"{data['mfr_name'] or 'Unknown'} / {data['board_type_name'] or 'Unknown'}"
    if data["board_revision"]:
        board_str += f" (rev. {data['board_revision']})"
    hw.add(f"Board         {board_str}")
    if data["leds"]:
        leds_branch = hw.add("LEDs")
        for led in data["leds"]:
            leds_branch.add(f"LED {led.instance_number}     {_led_name(led.led_type)}")
    if data["sensors"]:
        sensors_branch = hw.add("Sensors")
        for s in data["sensors"]:
            sensors_branch.add(f"Sensor {s.instance_number} {_sensor_name(s.sensor_type)}  (bus {s.bus_id})")
    if data["binary_inputs"]:
        buttons_branch = hw.add("Buttons")
        for b in data["binary_inputs"]:
            buttons_branch.add(f"Button {b.instance_number}  type 0x{b.input_type:02x}")

    pwr = tree.add("[bold]Power[/bold]")
    mode_line = data["power_mode_str"]
    if data["battery_mah"]:
        mode_line += f" {data['battery_mah']} mAh"
        if data["cap_str"]:
            mode_line += f" ({data['cap_str']})"
    pwr.add(f"Mode          {mode_line}")
    if data["sleep_timeout_ms"] is not None:
        sleep_str = "Never" if data["sleep_timeout_ms"] == 0 else f"{data['sleep_timeout_ms'] / 1000:.0f}s"
        pwr.add(f"Sleep         {sleep_str}")
    if data["deep_sleep_time_s"]:
        ua_str = f" @ {data['deep_sleep_ua']} µA" if data["deep_sleep_ua"] else ""
        pwr.add(f"Deep sleep    {data['deep_sleep_time_s']}s{ua_str}")
    if data["tx_power"] is not None:
        pwr.add(f"TX power      {data['tx_power']} dBm")

    if security:
        sec = tree.add("[bold]Security[/bold]")
        enc_label = "[green]Enabled[/green]" if security.encryption_enabled_flag else "[dim]Disabled[/dim]"
        sec.add(f"Encryption    {enc_label}")
        if security.session_timeout_seconds:
            sec.add(f"Session       {security.session_timeout_seconds}s")
        rewrite_label = "[green]Allowed[/green]" if security.rewrite_allowed else "[red]Denied[/red]"
        sec.add(f"Rewrite       {rewrite_label}")

    if wifi and wifi.ssid_text:
        wf = tree.add("[bold]WiFi[/bold]")
        wf.add(f"SSID          {wifi.ssid_text}")
        if wifi.server_url_text:
            wf.add(f"Server        {wifi.server_url_text}:{wifi.server_port}")
        enc_enum = wifi.encryption_type_enum
        enc_str = enc_enum.name if isinstance(enc_enum, WifiEncryption) else f"0x{enc_enum:02x}"
        wf.add(f"Encryption    {enc_str}")

    tree.add(f"[bold]Firmware[/bold]          {fw['major']}.{fw['minor']}  [dim](sha: {fw['sha']})[/dim]")
    return tree


def _add_info_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("info", help="Read and display device information")
    _add_device_options(p)
    p.add_argument("--json", dest="output_json", action="store_true", help="Output as JSON")
    p.set_defaults(func=_cmd_info)


def _cmd_info(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(_info(_device_kwargs(args.device, key, args.timeout), args.output_json))


async def _info(device_kwargs: dict[str, Any], output_json: bool) -> None:
    try:
        with _spinner() as progress:
            task = progress.add_task("Connecting...", total=None)
            async with OpenDisplayDevice(**device_kwargs) as device:
                progress.update(task, description="Reading info...")
                fw = await device.read_firmware_version()
                config = device.config
                display = config.displays[0] if config and config.displays else None

                transmission_modes: list[str] = []
                if display:
                    for flag, label in [
                        (display.supports_raw, "RAW"),
                        (display.supports_zip, "ZIP"),
                        (display.supports_g5, "G5"),
                        (display.supports_direct_write, "DIRECT_WRITE"),
                    ]:
                        if flag:
                            transmission_modes.append(label)

                ic_type_enum = config.system.ic_type_enum if config else None
                power_mode_enum = config.power.power_mode_enum if config else None
                cap_est = config.power.capacity_estimator_enum if config else None

                data: dict[str, Any] = {
                    "mac": device.mac_address,
                    "device_name": device.device_name,
                    "fw": fw,
                    "width": device.width,
                    "height": device.height,
                    "color_str": device.color_scheme.name,
                    "rotation": display.rotation_enum if display else device.rotation,
                    "active_w_mm": display.active_width_mm if display else None,
                    "active_h_mm": display.active_height_mm if display else None,
                    "diagonal": display.screen_diagonal_inches if display else None,
                    "panel_ic_type": display.panel_ic_type if display else None,
                    "full_update_mc": display.full_update_mC if display else None,
                    "transmission_modes": transmission_modes,
                    "ic_str": ic_type_enum.name
                    if isinstance(ic_type_enum, ICType)
                    else (f"0x{ic_type_enum:04x}" if ic_type_enum is not None else "Unknown"),
                    "power_mode_str": power_mode_enum.name
                    if isinstance(power_mode_enum, PowerMode)
                    else (str(power_mode_enum) if power_mode_enum is not None else "Unknown"),
                    "battery_mah": config.power.battery_mah if config else None,
                    "cap_str": cap_est.name if isinstance(cap_est, CapacityEstimator) else None,
                    "sleep_timeout_ms": config.power.sleep_timeout_ms if config else None,
                    "tx_power": config.power.tx_power if config else None,
                    "deep_sleep_time_s": config.power.deep_sleep_time_seconds if config else None,
                    "deep_sleep_ua": config.power.deep_sleep_current_ua if config else None,
                    "mfr_name": config.manufacturer.manufacturer_name if config else None,
                    "board_type_name": device.get_board_type_name() if config else None,
                    "board_revision": config.manufacturer.board_revision if config else None,
                    "security": config.security_config if config else None,
                    "wifi": config.wifi_config if config else None,
                    "leds": config.leds if config else [],
                    "sensors": config.sensors if config else [],
                    "binary_inputs": config.binary_inputs if config else [],
                }
    except OpenDisplayError as exc:
        _handle_ble_error(exc)

    if output_json:
        _stdout.print_json(json.dumps(_info_to_json(data)))
        return

    _console.print(_build_info_tree(data))


# ── upload ────────────────────────────────────────────────────────────────────


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
    p.add_argument(
        "--tone-compression",
        default="auto",
        metavar="VALUE",
        help='Dynamic range compression: "auto" or 0.0–1.0 (default: auto)',
    )
    p.set_defaults(func=_cmd_upload)


def _cmd_upload(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    tone = _parse_tone_compression(args.tone_compression)
    _run(
        _upload(
            _device_kwargs(args.device, key, args.timeout),
            args.image,
            _REFRESH_CHOICES[args.refresh_mode],
            _DITHER_CHOICES[args.dither_mode],
            _FIT_CHOICES[args.fit],
            _ROTATE_CHOICES[args.rotate],
            not args.no_compress,
            tone,
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
    tone_compression: float | str,
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

            async with OpenDisplayDevice(**device_kwargs) as device:
                spinner_progress.update(connect_task, visible=False)
                spinner_progress.update(upload_task, visible=True)
                bar_progress.update(bar_task, visible=True)

                def on_progress(sent: int, total: int) -> None:
                    bar_progress.update(bar_task, total=total, completed=sent)
                    if sent == total:
                        bar_progress.update(bar_task, visible=False)
                        spinner_progress.update(upload_task, visible=False)
                        spinner_progress.update(refresh_task, visible=True)

                await device.upload_image(
                    image,
                    refresh_mode=refresh_mode,
                    dither_mode=dither_mode,
                    compress=compress,
                    tone_compression=tone_compression,
                    fit=fit,
                    rotate=rotate,
                    progress_callback=on_progress,
                )

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
    _run(_reboot(_device_kwargs(args.device, key, args.timeout)))


async def _reboot(device_kwargs: dict[str, Any]) -> None:
    rebooted = False
    with _spinner() as progress:
        progress.add_task("Connecting...", total=None)
        try:
            async with OpenDisplayDevice(**device_kwargs) as device:
                await device.reboot()
                rebooted = True
        except (BLEConnectionError, BLETimeoutError):
            if not rebooted:
                _error("BLE connection failed before reboot command could be sent.")
            # else: expected drop after reboot
        except OpenDisplayError as exc:
            _handle_ble_error(exc)
    _console.print("Reboot command sent. Device will restart.")


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
    _run(_export_config(_device_kwargs(args.device, key, args.timeout), output))


async def _export_config(device_kwargs: dict[str, Any], output_path: str) -> None:
    try:
        with _spinner() as progress:
            task = progress.add_task("Connecting...", total=None)
            async with OpenDisplayDevice(**device_kwargs) as device:
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
    p.set_defaults(func=_cmd_write_config)


def _cmd_write_config(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(_write_config(_device_kwargs(args.device, key, args.timeout), args.input))


async def _write_config(device_kwargs: dict[str, Any], input_path: str) -> None:
    try:
        config = OpenDisplayDevice.import_config_json(input_path)
    except FileNotFoundError:
        _error(f"Config file not found: {input_path}")
    except (OSError, ValueError, KeyError) as exc:
        _error(f"Cannot read config file: {exc}")

    try:
        with _spinner() as progress:
            task = progress.add_task("Connecting...", total=None)
            async with OpenDisplayDevice(**device_kwargs) as device:
                progress.update(task, description="Writing config...")
                await device.write_config(config)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    _console.print("Config written [green]successfully[/green].")


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the opendisplay CLI."""
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
    _add_export_config_parser(subparsers)
    _add_write_config_parser(subparsers)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
