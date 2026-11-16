"""`opendisplay backup-config` and `opendisplay migrate`: give a firmware 3.0 device drive data."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from .cli_common import _add_device_options, _console, _device_kwargs, _error, _handle_ble_error, _parse_hex_key, _run
from .device import connect
from .exceptions import OpenDisplayError
from .models.config import GlobalConfig, SecurityConfig, WifiConfig
from .models.config_json import config_from_json, config_to_json
from .models.panel_drive import LengthPrefixedPacket
from .panels import MigrationPlan, PanelDatabase, PanelEntry, apply_migration, plan_migration
from .protocol import config_parser as packet_ids
from .protocol.config_serializer import serialize_config

_SHOW_KEY_ON_SCREEN = 0x02  # SecurityConfig.flags bit 1
_SECRETS_REFUSAL = (
    "The backup carries secrets: {found}. An unclaimed firmware 3.0 device does not store secrets; "
    "claim it first, then enter WiFi / FindMy settings again. "
    "Pass --strip-secrets to write the backup without them."
)
_UNTESTED_PROMPT = (
    "Panel {name} ({panel:#06x}): its drive data is 'equivalent': it replays byte-identically to what "
    "firmware 2.x sent, but nobody has watched this panel draw with it. Continue? [y/N] "
)
_PACKET_LABELS = {0xAE: "drive mode", 0xAF: "waveform", 0xB0: "calibration", 0xB1: "panel", 0xB3: "display controller"}


def _connect_kwargs(device_kwargs: dict[str, Any]) -> dict[str, Any]:
    """The CLI's OpenDisplayDevice keyword names, as ``opendisplay.connect()`` takes them."""
    names = {"mac_address": "address", "device_name": "name"}
    return {names.get(key, key): value for key, value in device_kwargs.items()}


def _ask(prompt: str) -> bool:
    """Ask a yes/no question on the terminal; no answer (or no terminal) is no."""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ── secrets (SECURITY_V2.md § 10.4, the closed list) ─────────────────────────


def find_secrets(config: GlobalConfig) -> list[str]:
    """The secrets a config carries, by name; never their values."""
    found = []
    security = config.security_config
    if security is not None and (security.encryption_enabled or any(security.encryption_key)):
        found.append("encryption key")
    if config.wifi_config is not None and any(config.wifi_config.password):
        found.append("WiFi password")
    if config.findmy_config_raw:
        found.append("FindMy settings")
    return found


def strip_secrets(config: GlobalConfig) -> GlobalConfig:
    """The config without secrets, in the form an unclaimed 3.0 device stores.

    Zeroes the 0x27 key and ``encryption_enabled`` and, as the firmware's ``unclaimed_form``
    does, clears the show-key flag and the first reserved byte; zeroes the 0x26 password; drops
    the 0x2D packet (the library keeps it raw, so its key material cannot be zeroed in place).
    """
    security: SecurityConfig | None = config.security_config
    if security is not None:
        security = dataclasses.replace(
            security,
            encryption_enabled=0,
            encryption_key=bytes(16),
            flags=security.flags & ~_SHOW_KEY_ON_SCREEN,
            reserved=bytes(1) + security.reserved[1:],
        )
    wifi: WifiConfig | None = config.wifi_config
    if wifi is not None:
        wifi = dataclasses.replace(wifi, password=bytes(len(wifi.password)))
    return dataclasses.replace(config, security_config=security, wifi_config=wifi, findmy_config_raw=None)


# ── describing packets ───────────────────────────────────────────────────────


def _packet_types(config: GlobalConfig) -> set[int]:
    """The packet types a config holds."""
    present = {
        packet_ids.PACKET_TYPE_DISPLAY: bool(config.displays),
        packet_ids.PACKET_TYPE_LED: bool(config.leds),
        packet_ids.PACKET_TYPE_SENSOR: bool(config.sensors),
        packet_ids.PACKET_TYPE_DATABUS: bool(config.data_buses),
        packet_ids.PACKET_TYPE_BINARY_INPUT: bool(config.binary_inputs),
        packet_ids.PACKET_TYPE_WIFI_CONFIG: config.wifi_config is not None,
        packet_ids.PACKET_TYPE_SECURITY_CONFIG: config.security_config is not None,
        packet_ids.PACKET_TYPE_TOUCH_CONTROLLER: bool(config.touch_controllers),
        packet_ids.PACKET_TYPE_PASSIVE_BUZZER: bool(config.buzzers),
        packet_ids.PACKET_TYPE_NFC_CONFIG: bool(config.nfc_configs),
        packet_ids.PACKET_TYPE_FLASH_CONFIG: bool(config.flash_configs),
        packet_ids.PACKET_TYPE_DATA_EXTENDED: config.data_extended is not None,
        packet_ids.PACKET_TYPE_FINDMY: config.findmy_config_raw is not None,
    }
    types = {packet_ids.PACKET_TYPE_SYSTEM, packet_ids.PACKET_TYPE_MANUFACTURER, packet_ids.PACKET_TYPE_POWER}
    types |= {packet_type for packet_type, here in present.items() if here}
    return types | {p.packet_type for p in config.length_prefixed_packets}


def _describe(packet: LengthPrefixedPacket) -> str:
    label = _PACKET_LABELS.get(packet.packet_type, "packet")
    if packet.packet_type == 0xAE and len(packet.body) >= 14:
        label += f" {packet.body[1]} '{packet.body[2:14].split(bytes(1), 1)[0].decode('ascii', 'replace')}'"
    return f"0x{packet.packet_type:02X} {label}: {len(packet.to_bytes())} bytes"


def _print_plan(plan: MigrationPlan) -> None:
    entry = plan.entry
    if isinstance(entry, PanelEntry):
        _console.print(f"Panel {entry.name} ({entry.panel_ic_type:#06x}), drive data verified: {entry.verified}")
    elif entry is not None:
        _console.print(
            f"Panel {entry.name} ({entry.panel_ic_type:#06x}), board {entry.board}: {entry.controller_class}"
        )
    if plan.remap_panel_ic_type is not None:
        _console.print(f"Display panel id becomes {plan.remap_panel_ic_type:#06x}")
    for packet in plan.add:
        _console.print(f"  Add    {_describe(packet)}")
    for packet in plan.remove:
        _console.print(f"  Remove {_describe(packet)}")


# ── backup-config ────────────────────────────────────────────────────────────


def add_backup_config_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``backup-config``."""
    p = subparsers.add_parser(
        "backup-config", help="Save the device's configuration to a new JSON file (never overwrites)"
    )
    _add_device_options(p)
    p.add_argument("output", metavar="FILE", help="Path of the backup file to create")
    p.set_defaults(func=_cmd_backup_config)


def _cmd_backup_config(args: argparse.Namespace) -> None:
    path = Path(args.output)
    if path.exists():
        _error(f"{path} exists; a backup never overwrites a file. Choose another name.")
    kwargs = _device_kwargs(args.device, _parse_hex_key(args.key), args.timeout, args.host, args.port, args.tls)
    _run(_backup_config(kwargs, path))


async def _backup_config(device_kwargs: dict[str, Any], path: Path) -> None:
    try:
        async with connect(**_connect_kwargs(device_kwargs)) as dev:
            config = dev.config.cached
    except OpenDisplayError as exc:
        _handle_ble_error(exc)
    if config is None:
        _error("The device has no stored configuration to back up.")
    with path.open("x", encoding="utf-8") as file:
        json.dump(config_to_json(config), file, indent=2)
    _console.print(f"Backup written to [bold]{path}[/bold]")
    secrets = find_secrets(config)
    if secrets:
        _console.print(f"[yellow]The file contains the device's {', '.join(secrets)}. Keep this file private.[/yellow]")


# ── migrate ──────────────────────────────────────────────────────────────────


def add_migrate_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``migrate``."""
    p = subparsers.add_parser("migrate", help="Give a firmware 3.0 device the drive data for its panel")
    _add_device_options(p)
    p.add_argument("--dry-run", action="store_true", help="Show what would change and write nothing")
    p.add_argument("--from-backup", metavar="FILE", help="Start from a backup file instead of the device's config")
    p.add_argument("--panel", metavar="ID", type=lambda v: int(v, 0), help="Migrate to this panel_ic_type")
    p.add_argument(
        "--yes-untested", action="store_true", help="Do not ask before writing drive data not yet seen on a panel"
    )
    p.add_argument(
        "--strip-secrets",
        action="store_true",
        help="With --from-backup: write the backup without its key, WiFi password and FindMy settings",
    )
    p.set_defaults(func=_cmd_migrate)


def _cmd_migrate(args: argparse.Namespace) -> None:
    backup = _load_backup(args.from_backup, args.strip_secrets) if args.from_backup else None
    kwargs = _device_kwargs(args.device, _parse_hex_key(args.key), args.timeout, args.host, args.port, args.tls)
    _run(_migrate(kwargs, args, backup))


def _load_backup(path: str, strip: bool) -> GlobalConfig:
    try:
        config = config_from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except FileNotFoundError:
        _error(f"Backup file not found: {path}")
    except (OSError, ValueError, KeyError) as exc:
        _error(f"Cannot read backup file: {exc}")
    secrets = find_secrets(config)
    if secrets and not strip:
        _error(_SECRETS_REFUSAL.format(found=", ".join(secrets)))
    if secrets:
        _console.print(f"Writing the backup without its {', '.join(secrets)}.")
        # TODO(piece 4): claim the device first, then write the secrets inside the SEC2 session.
        config = strip_secrets(config)
    return config


async def _migrate(device_kwargs: dict[str, Any], args: argparse.Namespace, backup: GlobalConfig | None) -> None:
    try:
        async with connect(**_connect_kwargs(device_kwargs)) as dev:
            await _migrate_connected(dev, args, backup)
    except OpenDisplayError as exc:
        _handle_ble_error(exc)


async def _migrate_connected(dev: Any, args: argparse.Namespace, backup: GlobalConfig | None) -> None:
    profile = dev.profile
    _console.print(
        f"Firmware {profile.generation.value}, target {getattr(profile.target, 'name', profile.target)}, "
        f"configured: {'yes' if profile.configured else 'no'}, needs migration: "
        f"{'yes' if profile.needs_migration else 'no'}"
    )
    start = backup if backup is not None else dev.config.cached
    if backup is not None:
        _console.print("The device's existing packets are replaced by the backup's.")
        if dev.config.cached is not None:
            lost = sorted(_packet_types(dev.config.cached) - _packet_types(backup))
            if lost:
                _console.print("The device has packet types the backup lacks: " + ", ".join(f"0x{t:02X}" for t in lost))

    db = PanelDatabase.load()
    plan = plan_migration(start, profile, db, panel_ic_type=args.panel)
    _console.print(plan.reason)
    if plan.blocked:
        _error(plan.reason)
    if not plan.needed:
        return
    _print_plan(plan)
    if args.dry_run:
        _console.print("Dry run: nothing written.")
        return
    entry = plan.entry
    if plan.needs_confirmation and not args.yes_untested and isinstance(entry, PanelEntry):
        if not _ask(_UNTESTED_PROMPT.format(name=entry.name, panel=entry.panel_ic_type)):
            _error("Not written.")
    assert start is not None  # a plan that is needed and not blocked has a starting config
    migrated = apply_migration(start, plan)
    await dev.config.write(migrated)
    stored = await dev.config.read()
    expected, actual = serialize_config(migrated), serialize_config(stored)
    if actual != expected:
        _error(
            f"The config read back ({len(actual)} bytes) does not match what was written ({len(expected)} bytes). "
            "Keep your backup; the write may have been interrupted."
        )
    _console.print(f"Config written and verified ({len(expected)} bytes).")
