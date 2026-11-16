"""Planning and applying the migration of a config to firmware 3.0 drive data.

A port of od-v3.js ``planMigration``, ``planNativeController`` and ``applyMigration`` (Web
``v3/website`` ``b7df514``), with the same branches in the same order and the same reason
strings. Differences from the JS:

- The "burned lab packet" branches (ids 0x2E-0x31) are not ported: outside ``unparsed_tail`` the
  library has no such packets. A config with an unparsed tail is blocked when a write is needed.
- A display packet is never invented: without one the plan is blocked, also when the caller
  names a panel, because pins, size and wiring are board-specific.
- The device's limits come from its identity when it has one (``max_config``, ``max_seq_steps``,
  escapes), else from the database's caps for the class.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ..models.config import DisplayConfig, GlobalConfig
from ..models.ident import FirmwareTarget
from ..models.panel_drive import (
    PACKET_TYPE_DISPLAY_CALIBRATION,
    PACKET_TYPE_DISPLAY_CONTROLLER,
    PACKET_TYPE_DISPLAY_LUT,
    PACKET_TYPE_DISPLAY_MODE,
    PACKET_TYPE_DISPLAY_PANEL,
    LengthPrefixedPacket,
)
from ..models.profile import DeviceProfile, Generation
from ..protocol.config_serializer import serialize_config
from .database import (
    NativeController,
    PanelDatabase,
    PanelEntry,
    TargetClass,
    check_drive_packets_fit,
    needs_drive_data,
    target_class_for,
)

_DRIVE_TYPES: Final = frozenset(
    {PACKET_TYPE_DISPLAY_MODE, PACKET_TYPE_DISPLAY_LUT, PACKET_TYPE_DISPLAY_CALIBRATION, PACKET_TYPE_DISPLAY_PANEL}
)
_DEFAULT_MAX_CONFIG: Final = 4096
NO_DISPLAY_REASON: Final = (
    "no display configured: a display packet cannot be made from a panel id alone (pins, size and wiring "
    "are board-specific); restore a backup that has one, or configure the display with the website toolbox"
)
UNPARSED_TAIL_REASON: Final = "the config has packets this library cannot read; migrate it with the website toolbox"


@dataclass(frozen=True)
class MigrationPlan:  # pylint: disable=too-many-instance-attributes
    """What a migration would add to and remove from a config, and why.

    Attributes:
        needed: The config must change before the device can draw.
        blocked: It must change, but cannot here; ``reason`` says why. Nothing is written.
        reason: One sentence, always set.
        panel_ic_type: The panel the plan is for (after a remap).
        target_class: The class whose limits apply.
        entry: The database entry the packets come from.
        add: Packets to append.
        remove: Existing packets to drop.
        needs_confirmation: The drive data is "equivalent": it replays byte-identically to what
            2.x sent, but nobody has watched this panel draw with it.
        remap_panel_ic_type: The id that replaces the display's ``panel_ic_type`` in 0x20, when
            it changes (a Firmware_NRF 2.x id, or a panel the caller named).
    """

    needed: bool
    blocked: bool
    reason: str
    panel_ic_type: int | None
    target_class: TargetClass
    entry: PanelEntry | NativeController | None = None
    add: tuple[LengthPrefixedPacket, ...] = ()
    remove: tuple[LengthPrefixedPacket, ...] = ()
    needs_confirmation: bool = False
    remap_panel_ic_type: int | None = None


def _limits(profile: DeviceProfile) -> tuple[int | None, bool | None]:
    """The identity's ``max_seq_steps`` and WINDOW/FILL support, ``None`` where it says nothing."""
    ident = profile.ident
    if ident is None:
        return None, None
    window = None if ident.escapes is None else bool(ident.supports_window or ident.supports_fill)
    return ident.max_seq_steps or None, window


def _max_config(profile: DeviceProfile, caps: Mapping[str, int] | None) -> int:
    """od-v3.js ``limitsFromIdent``: the identity's limit wins, else the class table (``caps``)."""
    if profile.ident is not None and profile.ident.max_config:
        return profile.ident.max_config
    target_class = target_class_for(profile.target)
    key = {TargetClass.BG22: "bg22", TargetClass.NRF52811: "nordic"}.get(target_class, "esp32")
    return int((caps or {}).get(key, _DEFAULT_MAX_CONFIG))


def check_fits(config: GlobalConfig, profile: DeviceProfile, *, caps: Mapping[str, int] | None = None) -> list[str]:
    """Problems that keep ``config`` from being stored and run on this device; empty when fine.

    Ports od-v3.js ``checkDrivePacketsFit`` with the identity's limits when present, plus the
    config size against the device's ``max_config``.
    """
    max_steps, window = _limits(profile)
    problems = check_drive_packets_fit(
        config.length_prefixed_packets, target_class_for(profile.target), max_steps=max_steps, window=window
    )
    size, limit = len(serialize_config(config)), _max_config(profile, caps)
    if size > limit:
        problems.append(f"the config needs {size} bytes, the device stores at most {limit} bytes")
    return problems


def _drive_packets(config: GlobalConfig | None) -> tuple[LengthPrefixedPacket, ...]:
    """od-v3.js ``drivePacketsOf``: the 0xAE-0xB1 packets."""
    if config is None:
        return ()
    return tuple(p for p in config.length_prefixed_packets if p.packet_type in _DRIVE_TYPES)


def _target_name(target: FirmwareTarget | int) -> str:
    return target.name if isinstance(target, FirmwareTarget) else f"0x{target:02x}"


def _plan_native(
    base: MigrationPlan, config: GlobalConfig, native: NativeController, profile: DeviceProfile
) -> MigrationPlan:
    """od-v3.js ``planNativeController``: one 0xB3 and no 0xAE-0xB1 (ESP32 only)."""
    plan = dataclasses.replace(base, entry=native)
    if profile.target is not None and profile.target != FirmwareTarget.ESP32:
        return dataclasses.replace(
            plan,
            needed=True,
            blocked=True,
            reason=(
                f"{native.controller_class} panels are driven only by ESP32 firmware; "
                f"this device is {_target_name(profile.target)}."
            ),
        )
    ident = profile.ident
    builtin_driver = None if ident is None else ident.builtin_driver
    device_says = bool(ident is not None and ident.needs_migration)
    controllers = tuple(p for p in config.length_prefixed_packets if p.packet_type == PACKET_TYPE_DISPLAY_CONTROLLER)
    stale = _drive_packets(config)
    if controllers and builtin_driver is not False and not device_says:
        if not stale:
            return dataclasses.replace(plan, reason="already has its display controller packet")
        if config.unparsed_tail:
            return dataclasses.replace(plan, needed=True, blocked=True, reason=UNPARSED_TAIL_REASON)
        return dataclasses.replace(
            plan, needed=True, remove=stale, reason="remove packets a native-controller panel does not use"
        )
    if config.unparsed_tail:
        return dataclasses.replace(plan, needed=True, blocked=True, reason=UNPARSED_TAIL_REASON)
    return dataclasses.replace(
        plan,
        needed=True,
        reason=f"add the {native.controller_class} display controller packet (0xB3)",
        remove=stale + controllers,
        add=native.packets,
    )


def plan_migration(  # pylint: disable=too-many-return-statements
    config: GlobalConfig | None,
    profile: DeviceProfile,
    db: PanelDatabase,
    *,
    panel_ic_type: int | None = None,
) -> MigrationPlan:
    """Decide what a config needs before a firmware 3.0 device can draw (od-v3.js ``planMigration``).

    Args:
        config: The starting config, or ``None`` when the device has none.
        profile: The device's profile; its target picks the class, its identity the limits.
        db: The panel database.
        panel_ic_type: The panel to migrate to, overriding the config's.
    """
    target_class = target_class_for(profile.target)
    plan = MigrationPlan(needed=False, blocked=False, reason="", panel_ic_type=None, target_class=target_class)
    if profile.generation is not Generation.V3:
        return dataclasses.replace(plan, reason="not a 3.x device")
    display: DisplayConfig | None = config.displays[0] if config is not None and config.displays else None
    if config is None or display is None:
        return dataclasses.replace(plan, needed=True, blocked=True, reason=NO_DISPLAY_REASON)
    panel = panel_ic_type if panel_ic_type is not None else display.panel_ic_type
    # A config carried over from Firmware_NRF 2.x names the panel by its 2.x id; 3.0 names it by
    # the drive preset's id.
    if target_class is TargetClass.NRF52811:
        panel = db.nrf52811_panel_for(panel) or panel
    remap = panel if panel != display.panel_ic_type else None
    plan = dataclasses.replace(plan, panel_ic_type=panel, remap_panel_ic_type=remap)

    native = db.native_controller_for(panel)
    if native is not None:
        return _plan_native(plan, config, native, profile)
    if not needs_drive_data(panel):
        return dataclasses.replace(plan, remap_panel_ic_type=None, reason="this panel is not driven by drive packets")

    # Drive packets already present but not runnable on this class must be replaced.
    present = _drive_packets(config)
    has_panel = any(p.packet_type == PACKET_TYPE_DISPLAY_PANEL for p in present)
    has_mode = any(p.packet_type == PACKET_TYPE_DISPLAY_MODE for p in present)
    missing = not has_panel or not has_mode
    max_steps, window = _limits(profile)
    present_problems = check_drive_packets_fit(present, target_class, max_steps=max_steps, window=window)
    device_says = bool(profile.ident is not None and profile.ident.needs_migration)
    if not missing and not present_problems and not device_says and panel_ic_type is None and remap is None:
        return dataclasses.replace(plan, reason="already has drive data")

    plan = dataclasses.replace(plan, needed=True)
    if config.unparsed_tail:
        return dataclasses.replace(plan, blocked=True, reason=UNPARSED_TAIL_REASON)
    entry = db.entry_for(panel, target_class)
    if entry is None or not entry.usable:
        why = entry.blocked_reason if entry is not None and entry.blocked_reason else None
        return dataclasses.replace(
            plan,
            blocked=True,
            entry=entry,
            reason=(why or f"No 3.x drive data exists for panel_ic_type {panel} yet.")
            + " The display stays blank on 3.x firmware until it does.",
        )
    if missing:
        reason = "config has no drive packets (carried over from 2.x)"
    elif present_problems:
        reason = "stored drive packets do not run on this target (" + "; ".join(present_problems) + ")"
    else:
        reason = "device asked for migration"
    plan = dataclasses.replace(
        plan,
        entry=entry,
        reason=reason,
        remove=present,
        add=entry.packets,
        needs_confirmation=entry.verified != "glass",
    )
    problems = check_fits(_apply(config, plan), profile, caps=db.caps)
    if problems:
        reason = f"panel {panel:#06x}'s drive data does not fit this device: " + "; ".join(problems)
        return dataclasses.replace(plan, blocked=True, reason=reason)
    return plan


def _without(
    packets: Sequence[LengthPrefixedPacket], drop: Sequence[LengthPrefixedPacket]
) -> list[LengthPrefixedPacket]:
    remaining = Counter(drop)
    out = []
    for packet in packets:
        if remaining[packet]:
            remaining[packet] -= 1
            continue
        out.append(packet)
    return out


def _apply(config: GlobalConfig, plan: MigrationPlan) -> GlobalConfig:
    packets = _without(config.length_prefixed_packets, plan.remove) + list(plan.add)
    displays = list(config.displays)
    if plan.remap_panel_ic_type is not None and displays:
        displays[0] = dataclasses.replace(displays[0], panel_ic_type=plan.remap_panel_ic_type)
    return dataclasses.replace(config, length_prefixed_packets=packets, displays=displays)


def apply_migration(config: GlobalConfig, plan: MigrationPlan) -> GlobalConfig:
    """Apply a plan (od-v3.js ``applyMigration``): drop ``remove``, append ``add``, remap 0x20.

    Every other packet is kept as it is; legacy packets are written back byte for byte except
    the remapped ``panel_ic_type``.

    Raises:
        ValueError: The plan is blocked, or a sequence in ``add`` does not run on its class.
    """
    if plan.blocked:
        raise ValueError(f"Refusing to apply a blocked migration plan: {plan.reason}")
    if not plan.needed:
        return config
    problems = check_drive_packets_fit(plan.add, plan.target_class)
    if problems:
        raise ValueError("Refusing to write drive data that does not run on this target: " + "; ".join(problems) + ".")
    return _apply(config, plan)
