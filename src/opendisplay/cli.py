"""Command-line interface for py-opendisplay."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from epaper_dithering import DitherMode
from PIL import Image, UnidentifiedImageError
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TaskProgressColumn, TextColumn
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
from .models.config import GlobalConfig, SensorData
from .models.enums import (
    PANEL_IC_NAMES,
    BinaryInputType,
    BusType,
    CapacityEstimator,
    DisplayTechnology,
    FitMode,
    FlashIcType,
    ICType,
    LedType,
    NfcIcType,
    PartialUpdateSupport,
    PowerMode,
    RefreshMode,
    Rotation,
    SensorType,
    TouchIcType,
    WifiEncryption,
)
from .models.firmware import FirmwareVersion
from .now_playing import NowPlaying, prepare_now_playing
from .partial import PartialState
from .sensors import SensorReading

_LOGGER = logging.getLogger(__name__)

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


def _parse_compression_value(flag: str, value: str) -> float | str:
    """Parse a compression knob value: 'auto'/'off' or a float in [0.0, 1.0]."""
    if value in ("auto", "off"):
        return value
    try:
        f = float(value)
    except ValueError:
        _error(f'{flag} must be "auto", "off", or a float, got {value!r}')
    if not 0.0 <= f <= 1.0:
        _error(f"{flag} must be between 0.0 and 1.0, got {f}")
    return f


def _device_kwargs(
    device: str | None,
    key: bytes | None,
    timeout: float,
    host: str | None = None,
    port: int | None = None,
    tls: bool = False,
) -> dict[str, Any]:
    """Build OpenDisplayDevice constructor kwargs from CLI args.

    With ``--host`` the device is addressed over TCP/LAN (WiFi). Otherwise a
    ``--device`` MAC address (contains ':') / macOS UUID (36-char, 4 dashes) or a
    human-readable device name is used over BLE.
    """
    kwargs: dict[str, Any] = {"timeout": timeout, "encryption_key": key}
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
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECS",
        help="Connection timeout in seconds (default: 10.0)",
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


def _enum_name(enum_cls: type[Any], value: int | None, digits: int = 2) -> str | None:
    """Enum member name for ``value``, or a hex literal when unrecognised."""
    if value is None:
        return None
    try:
        name: str = enum_cls(value).name
    except ValueError:
        return f"0x{value:0{digits}x}"
    return name


def _led_name(led_type: int) -> str:
    return _enum_name(LedType, led_type) or ""


def _sensor_name(sensor_type: int) -> str:
    return _enum_name(SensorType, sensor_type, digits=4) or ""


@dataclass(frozen=True)
class _SensorRow:
    """A configured sensor paired with its current reading, if it has one."""

    sensor: SensorData
    reading: SensorReading | None


def _sensor_rows(ctx: _InfoContext) -> list[_SensorRow]:
    """Pair each configured sensor with its live reading."""
    if not ctx.config:
        return []
    return [
        _SensorRow(sensor=sensor, reading=ctx.sensor_readings.get(sensor.instance_number))
        for sensor in ctx.config.sensors
    ]


def _sensor_line(row: _SensorRow) -> str:
    """Tree line for one sensor, with live values appended when available."""
    sensor = row.sensor
    line = f"Sensor {sensor.instance_number} {_sensor_name(sensor.sensor_type)}  (bus {sensor.bus_id})"
    values = [
        f"{value:.1f} {unit}"
        for value, unit in (
            (row.reading.temperature_c if row.reading else None, "°C"),
            (row.reading.humidity_percent if row.reading else None, "%RH"),
        )
        if value is not None
    ]
    return f"{line}  {'  '.join(values)}" if values else line


def _sensor_entry(row: _SensorRow) -> dict[str, Any]:
    """JSON object for one sensor; live values are null when unavailable."""
    sensor = row.sensor
    return {
        "instance": sensor.instance_number,
        "type": _sensor_name(sensor.sensor_type),
        "bus": sensor.bus_id,
        "temperature_c": row.reading.temperature_c if row.reading else None,
        "humidity_percent": row.reading.humidity_percent if row.reading else None,
    }


@dataclass(frozen=True)
class _InfoContext:
    """Everything the info report renders, gathered once from the device."""

    mac: str
    device_name: str | None
    fw: FirmwareVersion
    config: GlobalConfig | None
    width: int
    height: int
    color_scheme_name: str
    rotation: Any
    board_type_name: str | None
    # Live sensor values by instance number; empty when none could be read.
    sensor_readings: dict[int, SensorReading] = field(default_factory=dict)

    @property
    def display(self) -> Any:
        """The primary display packet, or None when the device returned no config."""
        if self.config and self.config.displays:
            return self.config.displays[0]
        return None


def _is_meaningful(value: Any) -> bool:
    """Whether a value earns a line in the tree.

    Zero is kept — ``Sleep 0s`` and ``0 mAh`` are real answers — but ``None``
    (field absent for this device) and empty strings/lists are dropped so the
    report stays about what the device actually has.
    """
    if value is None:
        return False
    if isinstance(value, str | list | tuple):
        return len(value) > 0
    return True


@dataclass(frozen=True)
class _Field:
    """One line of the report: a tree label plus a JSON key."""

    label: str
    key: str
    value: Callable[[_InfoContext], Any]
    render: Callable[[Any], str] | None = None
    # Tree-only override, for lines that combine several JSON keys (e.g. WxH).
    tree_value: Callable[[_InfoContext], Any] | None = None
    # Hide this line in the tree when the value is falsy-but-present (e.g. 0 mC).
    hide_when_zero: bool = False

    def text(self, value: Any) -> str:
        """Format ``value`` for the tree."""
        return self.render(value) if self.render else str(value)

    def visible(self, value: Any) -> bool:
        """Whether this field earns a line in the tree."""
        if self.hide_when_zero and not value:
            return False
        return _is_meaningful(value)


@dataclass(frozen=True)
class _Section:
    """A keyed group of fields, rendered as a tree branch and a JSON object."""

    title: str
    key: str
    fields: tuple[_Field, ...]
    # When present and returning False, the whole section is omitted (JSON: null).
    available: Callable[[_InfoContext], bool] | None = None
    # Back-compat: emit the JSON key even when the section is unavailable.
    json_nullable: bool = False

    def is_available(self, ctx: _InfoContext) -> bool:
        """Whether this section applies to the device at all."""
        return self.available(ctx) if self.available else True


@dataclass(frozen=True)
class _ListSection:
    """A repeatable packet: one tree line and one JSON object per instance."""

    title: str
    key: str
    items: Callable[[_InfoContext], list[Any]]
    line: Callable[[Any], str]
    entry: Callable[[Any], dict[str, Any]]
    # Existing JSON nests leds/sensors/buttons under "hardware"; keep that shape.
    json_parent: str | None = None


# ── field extractors ─────────────────────────────────────────────────────────


def _panel_label(ctx: _InfoContext) -> str | None:
    """Panel description plus its raw id; hex alone when the id is unrecognised."""
    panel_id = _display_attr(ctx, "panel_ic_type")
    if panel_id is None:
        return None
    described = PANEL_IC_NAMES.get(panel_id)
    return f"{described} [dim](0x{panel_id:04x})[/dim]" if described else f"0x{panel_id:04x}"


_PARTIAL_UPDATE_LABELS: dict[PartialUpdateSupport, str] = {
    PartialUpdateSupport.NONE: "Not supported",
    PartialUpdateSupport.PARTIAL: "Supported",
    PartialUpdateSupport.FULL_FRAME: "Supported (full-frame stream required)",
}


def _partial_update_label(ctx: _InfoContext) -> str | None:
    display = ctx.display
    if display is None:
        return None
    try:
        return _PARTIAL_UPDATE_LABELS[PartialUpdateSupport(display.partial_update_support)]
    except ValueError:
        return f"0x{display.partial_update_support:02x}"


def _transmission_modes(ctx: _InfoContext) -> list[str]:
    display = ctx.display
    if display is None:
        return []
    return [
        label
        for flag, label in (
            (display.supports_streaming_decompression, "STREAMING"),
            (display.supports_zip, "ZIP"),
            (display.supports_g5, "G5"),
            (display.supports_direct_write, "DIRECT_WRITE"),
            (display.supports_pipe_write, "PIPE_WRITE"),
        )
        if flag
    ]


def _physical_size(ctx: _InfoContext) -> str | None:
    display = ctx.display
    if display is None or not (display.active_width_mm and display.active_height_mm):
        return None
    diagonal = display.screen_diagonal_inches
    suffix = f' ({diagonal:.1f}")' if diagonal is not None else ""
    return f"{display.active_width_mm}x{display.active_height_mm} mm{suffix}"


def _ic_label(ctx: _InfoContext) -> str:
    if not ctx.config:
        return "Unknown"
    return _enum_name(ICType, ctx.config.system.ic_type, digits=4) or "Unknown"


def _board_label(ctx: _InfoContext) -> str | None:
    if not ctx.config:
        return None
    mfr = ctx.config.manufacturer.manufacturer_name or "Unknown"
    board = f"{mfr} / {ctx.board_type_name or 'Unknown'}"
    if ctx.config.manufacturer.board_revision:
        board += f" (rev. {ctx.config.manufacturer.board_revision})"
    return board


def _power_mode_label(ctx: _InfoContext) -> str:
    if not ctx.config:
        return "Unknown"
    power = ctx.config.power
    mode = _enum_name(PowerMode, power.power_mode) or "Unknown"
    if power.battery_mah:
        mode += f" {power.battery_mah} mAh"
        chemistry = _enum_name(CapacityEstimator, power.capacity_estimator)
        if chemistry:
            mode += f" ({chemistry})"
    return mode


def _sleep_seconds(ctx: _InfoContext) -> float | None:
    """Sleep timeout in SECONDS for JSON; the packet stores milliseconds."""
    milliseconds = _config_attr(ctx, "power", "sleep_timeout_ms")
    return milliseconds / 1000 if milliseconds else None


def _sleep_tree_label(ctx: _InfoContext) -> str | None:
    milliseconds = _config_attr(ctx, "power", "sleep_timeout_ms")
    if milliseconds is None:
        return None
    return "Never" if milliseconds == 0 else f"{milliseconds / 1000:.0f}s"


def _deep_sleep_label(ctx: _InfoContext) -> str | None:
    if not ctx.config or not ctx.config.power.deep_sleep_time_seconds:
        return None
    power = ctx.config.power
    micro_amps = f" @ {power.deep_sleep_current_ua} µA" if power.deep_sleep_current_ua else ""
    return f"{power.deep_sleep_time_seconds}s{micro_amps}"


def _wifi_encryption(ctx: _InfoContext) -> str | None:
    wifi = ctx.config.wifi_config if ctx.config else None
    if wifi is None:
        return None
    encryption = wifi.encryption_type_enum
    if isinstance(encryption, WifiEncryption):
        return encryption.name
    return f"0x{encryption:02x}"


def _wifi_server(ctx: _InfoContext) -> str | None:
    wifi = ctx.config.wifi_config if ctx.config else None
    if wifi is None or not wifi.server_url_text:
        return None
    return f"{wifi.server_url_text}:{wifi.server_port}"


def _has_wifi(ctx: _InfoContext) -> bool:
    wifi = ctx.config.wifi_config if ctx.config else None
    return bool(wifi and wifi.ssid_text)


def _has_security(ctx: _InfoContext) -> bool:
    return bool(ctx.config and ctx.config.security_config)


def _has_identity(ctx: _InfoContext) -> bool:
    extended = ctx.config.data_extended if ctx.config else None
    if extended is None:
        return False
    return any(
        (
            extended.model_name_text,
            extended.serial_number_text,
            extended.friendly_name_text,
            extended.device_location_text,
            extended.device_id_text,
        )
    )


def _extended(ctx: _InfoContext, attr: str) -> str | None:
    extended = ctx.config.data_extended if ctx.config else None
    return getattr(extended, f"{attr}_text") if extended else None


def _config_attr(ctx: _InfoContext, section: str, attr: str) -> Any:
    return getattr(getattr(ctx.config, section), attr) if ctx.config else None


def _display_attr(ctx: _InfoContext, attr: str) -> Any:
    display = ctx.display
    return getattr(display, attr) if display else None


# ── the report spec ──────────────────────────────────────────────────────────

_INFO_SECTIONS: tuple[_Section, ...] = (
    _Section(
        title="Display",
        key="display",
        fields=(
            _Field(
                "Resolution",
                "width",
                lambda c: c.width,
                tree_value=lambda c: f"{c.width}x{c.height}px",
            ),
            _Field("", "height", lambda c: c.height),
            _Field("Physical", "active_width_mm", _physical_size),
            _Field("", "active_height_mm", lambda c: _display_attr(c, "active_height_mm")),
            _Field("", "diagonal_inches", lambda c: _display_attr(c, "screen_diagonal_inches")),
            _Field("Color", "color_scheme", lambda c: c.color_scheme_name, _color_scheme_label),
            _Field("Rotation", "rotation", lambda c: c.rotation, lambda v: f"{v}°"),
            _Field(
                "Panel",
                "panel_ic_type",
                lambda c: PANEL_IC_NAMES.get(_display_attr(c, "panel_ic_type")),
                tree_value=_panel_label,
            ),
            _Field(
                "Technology",
                "display_technology",
                lambda c: _enum_name(DisplayTechnology, _display_attr(c, "display_technology")),
            ),
            _Field("Partial", "partial_update", _partial_update_label),
            _Field(
                "Full update",
                "full_update_mc",
                lambda c: _display_attr(c, "full_update_mC"),
                lambda v: f"{v} mC",
                hide_when_zero=True,
            ),
            _Field("Transmission", "transmission_modes", _transmission_modes, " ".join),
        ),
    ),
    _Section(
        title="Hardware",
        key="hardware",
        fields=(
            _Field("MCU", "ic", _ic_label),
            _Field("Board", "board_type", _board_label),
            _Field("", "manufacturer", lambda c: _config_attr(c, "manufacturer", "manufacturer_name")),
            _Field("", "board_revision", lambda c: _config_attr(c, "manufacturer", "board_revision")),
        ),
    ),
    _Section(
        title="Power",
        key="power",
        fields=(
            _Field("Mode", "mode", _power_mode_label),
            _Field("", "battery_mah", lambda c: _config_attr(c, "power", "battery_mah")),
            _Field(
                "", "chemistry", lambda c: _enum_name(CapacityEstimator, _config_attr(c, "power", "capacity_estimator"))
            ),
            _Field("Sleep", "sleep_timeout_s", _sleep_seconds, tree_value=_sleep_tree_label),
            _Field(
                "Screen off",
                "screen_timeout_s",
                lambda c: _config_attr(c, "power", "screen_timeout_seconds"),
                lambda v: f"{v}s",
                hide_when_zero=True,
            ),
            _Field(
                "Min wake",
                "min_wake_time_s",
                lambda c: _config_attr(c, "power", "min_wake_time_seconds"),
                lambda v: f"{v}s",
                hide_when_zero=True,
            ),
            _Field(
                "Deep sleep",
                "deep_sleep_time_s",
                lambda c: _config_attr(c, "power", "deep_sleep_time_seconds") or None,
                tree_value=_deep_sleep_label,
            ),
            _Field("", "deep_sleep_current_ua", lambda c: _config_attr(c, "power", "deep_sleep_current_ua") or None),
            _Field("TX power", "tx_power_dbm", lambda c: _config_attr(c, "power", "tx_power"), lambda v: f"{v} dBm"),
        ),
    ),
    _Section(
        title="Security",
        key="security",
        available=_has_security,
        json_nullable=True,
        fields=(
            _Field(
                "Encryption",
                "encryption",
                lambda c: (
                    c.config.security_config.encryption_enabled_flag if c.config and c.config.security_config else None
                ),
                lambda v: "[green]Enabled[/green]" if v else "[dim]Disabled[/dim]",
            ),
            _Field(
                "Session",
                "session_timeout_s",
                lambda c: (
                    (c.config.security_config.session_timeout_seconds or None)
                    if c.config and c.config.security_config
                    else None
                ),
                lambda v: f"{v}s",
            ),
            _Field(
                "Rewrite",
                "rewrite_allowed",
                lambda c: c.config.security_config.rewrite_allowed if c.config and c.config.security_config else None,
                lambda v: "[green]Allowed[/green]" if v else "[red]Denied[/red]",
            ),
        ),
    ),
    _Section(
        title="WiFi",
        key="wifi",
        available=_has_wifi,
        json_nullable=True,
        fields=(
            _Field(
                "SSID", "ssid", lambda c: c.config.wifi_config.ssid_text if c.config and c.config.wifi_config else None
            ),
            _Field("Server", "server", _wifi_server),
            _Field("Encryption", "encryption", _wifi_encryption),
        ),
    ),
    _Section(
        title="Identity",
        key="identity",
        available=_has_identity,
        json_nullable=True,
        fields=(
            _Field("Model", "model_name", lambda c: _extended(c, "model_name")),
            _Field("Serial", "serial_number", lambda c: _extended(c, "serial_number")),
            _Field("Name", "friendly_name", lambda c: _extended(c, "friendly_name")),
            _Field("Location", "device_location", lambda c: _extended(c, "device_location")),
            _Field("Device ID", "device_id", lambda c: _extended(c, "device_id")),
        ),
    ),
)


def _bus_line(bus: Any) -> str:
    speed = f"{bus.bus_speed_hz / 1000:.0f} kHz" if bus.bus_speed_hz else "unset"
    bus_type = _enum_name(BusType, bus.bus_type) or "?"
    return f"Bus {bus.instance_number}        {bus_type} {speed}"


_INFO_LIST_SECTIONS: tuple[_ListSection, ...] = (
    _ListSection(
        title="LEDs",
        key="leds",
        json_parent="hardware",
        items=lambda c: c.config.leds if c.config else [],
        line=lambda led: f"LED {led.instance_number}     {_led_name(led.led_type)}",
        entry=lambda led: {"instance": led.instance_number, "type": _led_name(led.led_type)},
    ),
    _ListSection(
        title="Sensors",
        key="sensors",
        json_parent="hardware",
        items=_sensor_rows,
        line=_sensor_line,
        entry=_sensor_entry,
    ),
    _ListSection(
        title="Buttons",
        key="buttons",
        json_parent="hardware",
        items=lambda c: c.config.binary_inputs if c.config else [],
        line=lambda b: f"Button {b.instance_number}  {_enum_name(BinaryInputType, b.input_type)}",
        entry=lambda b: {
            "instance": b.instance_number,
            "input_type": b.input_type,
            "type": _enum_name(BinaryInputType, b.input_type),
        },
    ),
    _ListSection(
        title="Touch",
        key="touch_controllers",
        json_parent="hardware",
        items=lambda c: c.config.touch_controllers if c.config else [],
        line=lambda t: (
            f"Touch {t.instance_number}   {_enum_name(TouchIcType, t.touch_ic_type)}"
            f"  (i2c 0x{t.i2c_addr_7bit:02x}, bus {t.bus_id})"
        ),
        entry=lambda t: {
            "instance": t.instance_number,
            "type": _enum_name(TouchIcType, t.touch_ic_type),
            "i2c_addr": f"0x{t.i2c_addr_7bit:02x}",
            "bus": t.bus_id,
            "poll_interval_ms": t.poll_interval_ms,
        },
    ),
    _ListSection(
        title="Buzzers",
        key="buzzers",
        json_parent="hardware",
        items=lambda c: c.config.buzzers if c.config else [],
        line=lambda b: f"Buzzer {b.instance_number}  pin {b.drive_pin}, {b.duty_percent}% duty",
        entry=lambda b: {"instance": b.instance_number, "drive_pin": b.drive_pin, "duty_percent": b.duty_percent},
    ),
    _ListSection(
        title="Buses",
        key="buses",
        json_parent="hardware",
        items=lambda c: c.config.data_buses if c.config else [],
        line=_bus_line,
        entry=lambda bus: {
            "instance": bus.instance_number,
            "type": _enum_name(BusType, bus.bus_type),
            "speed_hz": bus.bus_speed_hz,
        },
    ),
    _ListSection(
        title="NFC",
        key="nfc",
        json_parent="hardware",
        items=lambda c: c.config.nfc_configs if c.config else [],
        line=lambda n: f"NFC {n.instance_number}     {_enum_name(NfcIcType, n.nfc_ic_type)}  (bus {n.bus_instance})",
        entry=lambda n: {
            "instance": n.instance_number,
            "type": _enum_name(NfcIcType, n.nfc_ic_type),
            "bus": n.bus_instance,
        },
    ),
    _ListSection(
        title="Flash",
        key="flash",
        json_parent="hardware",
        items=lambda c: c.config.flash_configs if c.config else [],
        line=lambda f: (
            f"Flash {f.instance_number}   {_enum_name(FlashIcType, f.flash_ic_type)}  (bus {f.bus_instance})"
        ),
        entry=lambda f: {
            "instance": f.instance_number,
            "type": _enum_name(FlashIcType, f.flash_ic_type),
            "bus": f.bus_instance,
        },
    ),
)


# ── rendering ────────────────────────────────────────────────────────────────


def _info_to_json(ctx: _InfoContext) -> dict[str, Any]:
    rendered: dict[str, Any] = {"mac": ctx.mac}

    for section in _INFO_SECTIONS:
        if not section.is_available(ctx):
            if section.json_nullable:
                rendered[section.key] = None
            continue
        rendered[section.key] = {field.key: field.value(ctx) for field in section.fields}

    for list_section in _INFO_LIST_SECTIONS:
        entries = [list_section.entry(item) for item in list_section.items(ctx)]
        target = rendered.setdefault(list_section.json_parent, {}) if list_section.json_parent else rendered
        target[list_section.key] = entries

    fw = ctx.fw
    rendered["firmware"] = {
        "major": fw["major"],
        "minor": fw["minor"],
        "patch": fw.get("patch", 0),
        "sha": fw["sha"],
    }
    return rendered


def _build_info_tree(ctx: _InfoContext) -> Tree:
    root_label = f"{ctx.device_name} ({ctx.mac})" if ctx.device_name else ctx.mac
    tree = Tree(root_label, guide_style="cyan dim")

    branches: dict[str, Tree] = {}
    for section in _INFO_SECTIONS:
        if not section.is_available(ctx):
            continue
        lines = [
            (field.label, field.text(value))
            for field in section.fields
            if field.label and field.visible(value := (field.tree_value or field.value)(ctx))
        ]
        nested = any(ls.json_parent == section.key and ls.items(ctx) for ls in _INFO_LIST_SECTIONS)
        if not lines and not nested:
            continue
        branch = tree.add(f"[bold]{section.title}[/bold]")
        branches[section.key] = branch
        for label, text in lines:
            branch.add(f"{label:<13} {text}")

    for list_section in _INFO_LIST_SECTIONS:
        items = list_section.items(ctx)
        if not items:
            continue
        parent = branches.get(list_section.json_parent or "", tree)
        group = parent.add(list_section.title)
        for item in items:
            group.add(list_section.line(item))

    fw = ctx.fw
    version = f"{fw['major']}.{fw['minor']}.{fw.get('patch', 0)}"
    tree.add(f"[bold]Firmware[/bold]          {version}  [dim](sha: {fw['sha']})[/dim]")
    return tree


def _add_info_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("info", help="Read and display device information")
    _add_device_options(p)
    p.add_argument("--json", dest="output_json", action="store_true", help="Output as JSON")
    p.set_defaults(func=_cmd_info)


def _cmd_info(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(_info(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls), args.output_json))


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


async def _info(device_kwargs: dict[str, Any], output_json: bool) -> None:
    try:
        with _spinner() as progress:
            task = progress.add_task("Connecting...", total=None)
            async with OpenDisplayDevice(**device_kwargs) as device:
                progress.update(task, description="Reading info...")
                fw = await device.read_firmware_version()
                config = device.config
                display = config.displays[0] if config and config.displays else None
                ctx = _InfoContext(
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
                )
    except OpenDisplayError as exc:
        _handle_ble_error(exc)

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
        help='Tone compression: "auto", "off", or 0.0–1.0 (default: 0)',
    )
    p.add_argument(
        "--gamut",
        default="0",
        metavar="VALUE",
        help='Gamut compression: "auto", "off", or 0.0–1.0 (default: 0)',
    )
    p.add_argument(
        "--state-file",
        metavar="PATH",
        default=None,
        help="Persistent partial-rendering state file. If the file exists, it's loaded and "
        "the upload attempts a partial transfer; on success the file is rewritten. If it "
        "does not exist, a fresh state is created (forcing a full upload first time).",
    )
    p.set_defaults(func=_cmd_upload)


def _cmd_upload(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    tone = _parse_compression_value("--tone", args.tone)
    gamut = _parse_compression_value("--gamut", args.gamut)
    _run(
        _upload(
            _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls),
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
    tone: float | str,
    gamut: float | str,
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
    _run(_reboot(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls)))


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


# ── sleep ─────────────────────────────────────────────────────────────────────


def _add_sleep_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("sleep", help="Put the device into deep sleep (command 0x0052)")
    _add_device_options(p)
    p.set_defaults(func=_cmd_sleep)


def _cmd_sleep(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    _run(_sleep(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls)))


async def _sleep(device_kwargs: dict[str, Any]) -> None:
    slept = False
    with _spinner() as progress:
        progress.add_task("Connecting...", total=None)
        try:
            async with OpenDisplayDevice(**device_kwargs) as device:
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
    _run(_export_config(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls), output))


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
    _run(_write_config(_device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls), args.input))


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


# ── sendspin ──────────────────────────────────────────────────────────────────


def _sendspin_state_dir(device: str) -> Path:
    """Default directory for a device's Sendspin identity and pairing state."""
    return Path.home() / ".config" / "opendisplay" / "sendspin" / device.replace(":", "").lower()


def _add_sendspin_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
    p.set_defaults(func=_cmd_sendspin)


def _cmd_sendspin(args: argparse.Namespace) -> None:
    key = _parse_hex_key(args.key)
    device_kwargs = _device_kwargs(args.device, key, args.timeout, args.host, args.port, args.tls)
    label = args.device or args.host
    state_dir = Path(args.state_dir) if args.state_dir else _sendspin_state_dir(label)
    with contextlib.suppress(KeyboardInterrupt):
        _run(_sendspin(device_kwargs, args.url, state_dir, args.name or f"OpenDisplay {label}"))


async def _upload_now_playing(device_kwargs: dict[str, Any], config: GlobalConfig, now: NowPlaying) -> None:
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
        )
    )
    async with OpenDisplayDevice(**device_kwargs, config=config) as device:
        await device.upload_prepared_image(prepared)


async def _sendspin(device_kwargs: dict[str, Any], url: str | None, state_dir: Path, name: str) -> None:
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
            await _upload_now_playing(device_kwargs, config, now)
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
    _add_sleep_parser(subparsers)
    _add_export_config_parser(subparsers)
    _add_write_config_parser(subparsers)
    _add_sendspin_parser(subparsers)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
