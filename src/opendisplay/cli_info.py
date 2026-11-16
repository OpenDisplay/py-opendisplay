"""The ``opendisplay info`` report: what it shows and how it is rendered as a tree or as JSON."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.tree import Tree

from .models.config import GlobalConfig, SensorData
from .models.enums import (
    PANEL_IC_NAMES,
    BinaryInputType,
    BusType,
    CapacityEstimator,
    DisplayTechnology,
    FlashIcType,
    ICType,
    LedType,
    NfcIcType,
    PartialUpdateSupport,
    PowerMode,
    SensorType,
    TouchIcType,
    WifiEncryption,
)
from .models.firmware import FirmwareVersion
from .models.ident import DeviceIdent, FirmwareTarget
from .sensors import SensorReading

_COLOR_SCHEME_STYLES: dict[str, str] = {"R": "red", "Y": "yellow", "G": "green"}


def _color_scheme_label(name: str) -> str:
    """Return a rich-marked-up color scheme name with accent ink colors highlighted."""
    parts = []
    for ch in name:
        style = _COLOR_SCHEME_STYLES.get(ch)
        parts.append(f"[{style}]{ch}[/{style}]" if style else ch)
    return "".join(parts)


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
    # READ_IDENT answer; None when the firmware predates the command or it was not asked.
    ident: DeviceIdent | None = None

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


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _fw_target_label(ctx: _InfoContext) -> str | None:
    if ctx.ident is None:
        return None
    target = ctx.ident.fw_target
    return target.name if isinstance(target, FirmwareTarget) else f"unknown (0x{target:02x})"


def _ident_attr(name: str) -> Callable[[_InfoContext], Any]:
    """Extractor for one attribute of the identity, None when no identity was read."""
    return lambda ctx: getattr(ctx.ident, name) if ctx.ident else None


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
        # A device with no config (and no display record in its identity) has nothing to show here.
        available=lambda c: c.config is not None or c.width > 0,
        json_nullable=True,
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
        title="Protocol",
        key="ident",
        available=lambda c: c.ident is not None,
        json_nullable=True,
        fields=(
            _Field("Target", "fw_target", _fw_target_label),
            _Field("Protocol minor", "proto_minor", _ident_attr("proto_minor")),
            _Field("Config-driven", "config_driven", _ident_attr("config_driven"), _yes_no),
            _Field("Needs migration", "needs_migration", _ident_attr("needs_migration"), _yes_no),
            _Field("Built-in driver", "builtin_driver", _ident_attr("builtin_driver"), _yes_no),
            _Field("Max config", "max_config", _ident_attr("max_config"), lambda v: f"{v} bytes"),
            _Field("Max sequence steps", "max_seq_steps", _ident_attr("max_seq_steps")),
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
