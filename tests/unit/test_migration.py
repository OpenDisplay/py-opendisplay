"""Planning and applying the migration of a config to firmware 3.0 drive data (od-v3.js planMigration)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from test_profile import FW2, FW3, IDENT_NEEDS_MIGRATION, _config, _display

from opendisplay.models.config import GlobalConfig
from opendisplay.models.enums import ICType
from opendisplay.models.panel_drive import LengthPrefixedPacket
from opendisplay.models.profile import build_profile
from opendisplay.panels import PanelDatabase, TargetClass
from opendisplay.panels.migration import MigrationPlan, apply_migration, check_fits, plan_migration
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import serialize_config
from opendisplay.protocol.responses import parse_read_ident

DB = PanelDatabase.load()
ALPHA_CONFIG = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
# ESP32 identities: flags 0x29, max_config 600 / max_seq_steps 896; and max_seq_steps 3.
IDENT_MAX_CONFIG_600 = parse_read_ident(bytes.fromhex("0046040100295802800303"))
IDENT_MAX_STEPS_3 = parse_read_ident(bytes.fromhex("004604010029a00f030003"))
GLASS_PANEL = 0x001E  # glass on the default class
EQUIVALENT_PANEL = 0x0001  # EP42_400x300


def _esp32(panel_ic_type: int = EQUIVALENT_PANEL, **overrides: object) -> GlobalConfig:
    """A 2.x ESP32 config for one panel, without drive data."""
    return _config(displays=[_display(panel_ic_type=panel_ic_type)], **overrides)


def _legacy_part(config: GlobalConfig) -> bytes:
    """The serialized legacy packets (0x00-0x7F): everything before the length-prefixed range."""
    blob = serialize_config(dataclasses.replace(config, length_prefixed_packets=[]))
    return blob[3:-2]


def test_2x_config_on_3_0_gets_the_panels_drive_packets() -> None:
    config = _esp32()
    plan = plan_migration(config, build_profile(FW3, config, IDENT_NEEDS_MIGRATION), DB)

    assert isinstance(plan, MigrationPlan)
    assert (plan.needed, plan.blocked) == (True, False)
    assert plan.reason == "config has no drive packets (carried over from 2.x)"
    assert plan.target_class is TargetClass.DEFAULT
    assert plan.add == DB.entry_for(EQUIVALENT_PANEL, TargetClass.DEFAULT).packets  # type: ignore[union-attr]
    assert (plan.remove, plan.remap_panel_ic_type) == ((), None)

    migrated = apply_migration(config, plan)

    assert migrated.display_modes[0].mode_index == 0
    assert migrated.length_prefixed_packets == list(plan.add)
    # Legacy packets are written back byte for byte.
    assert serialize_config(migrated)[3 : 3 + len(_legacy_part(config))] == _legacy_part(config)


def test_running_migrate_twice_is_harmless() -> None:
    config = _esp32()
    migrated = apply_migration(config, plan_migration(config, build_profile(FW3, config, None), DB))

    again = plan_migration(migrated, build_profile(FW3, migrated, None), DB)

    assert (again.needed, again.blocked, again.reason) == (False, False, "already has drive data")


def test_not_a_3_0_device_needs_nothing() -> None:
    config = _esp32()
    plan = plan_migration(config, build_profile(FW2, config, None), DB)

    assert (plan.needed, plan.blocked, plan.reason) == (False, False, "not a 3.x device")


def test_already_migrated_config_needs_nothing() -> None:  # Review Focus 3
    plan = plan_migration(ALPHA_CONFIG, build_profile(FW3, ALPHA_CONFIG, None), DB)

    assert (plan.needed, plan.blocked, plan.reason) == (False, False, "already has drive data")
    assert plan.target_class is TargetClass.BG22
    assert (plan.add, plan.remove) == ((), ())


def test_unknown_panel_blocks() -> None:  # Review Focus 1
    config = _esp32(panel_ic_type=768)
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert (plan.needed, plan.blocked) == (True, True)
    assert plan.reason == (
        "No 3.x drive data exists for panel_ic_type 768 yet. The display stays blank on 3.x firmware until it does."
    )
    assert plan.add == ()


def test_entry_too_large_for_the_device_blocks() -> None:  # Review Focus 2
    config = _esp32()
    plan = plan_migration(config, build_profile(FW3, config, IDENT_MAX_CONFIG_600), DB)

    assert (plan.needed, plan.blocked) == (True, True)
    assert "600" in plan.reason and "bytes" in plan.reason


def test_entry_with_more_steps_than_the_device_allows_blocks() -> None:  # Review Focus 2: identity limits win
    config = _esp32()
    plan = plan_migration(config, build_profile(FW3, config, IDENT_MAX_STEPS_3), DB)

    assert plan.blocked
    assert "limit 3" in plan.reason


def test_equivalent_panel_asks_for_confirmation() -> None:
    for panel, expected in ((EQUIVALENT_PANEL, True), (GLASS_PANEL, False)):
        config = _esp32(panel_ic_type=panel)
        plan = plan_migration(config, build_profile(FW3, config, None), DB)

        assert plan.needed and not plan.blocked
        assert plan.needs_confirmation is expected


def test_it8951_board_gets_one_controller_packet() -> None:  # Review Focus 4
    config = _esp32(panel_ic_type=3000)
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert (plan.needed, plan.blocked) == (True, False)
    assert plan.reason == "add the IT8951 display controller packet (0xB3)"
    assert [p.packet_type for p in plan.add] == [0xB3]
    assert plan.needs_confirmation is False
    migrated = apply_migration(config, plan)
    assert [p.packet_type for p in migrated.length_prefixed_packets] == [0xB3]


def test_it8951_board_that_has_its_controller_packet_needs_nothing() -> None:
    config = _esp32(panel_ic_type=3000)
    migrated = apply_migration(config, plan_migration(config, build_profile(FW3, config, None), DB))

    plan = plan_migration(migrated, build_profile(FW3, migrated, None), DB)

    assert (plan.needed, plan.reason) == (False, "already has its display controller packet")


def test_it8951_board_drops_drive_packets_it_does_not_use() -> None:
    native = DB.native_controller_for(3000)
    assert native is not None
    stale = DB.entry_for(EQUIVALENT_PANEL, TargetClass.DEFAULT).packets  # type: ignore[union-attr]
    config = _esp32(panel_ic_type=3000, length_prefixed_packets=[*native.packets, *stale])

    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert (plan.needed, plan.reason) == (True, "remove packets a native-controller panel does not use")
    assert plan.remove == stale and plan.add == ()


def test_it8951_board_on_another_target_blocks() -> None:
    config = _esp32(panel_ic_type=3000, system=dataclasses.replace(_config().system, ic_type=ICType.NRF52840))
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert (plan.needed, plan.blocked) == (True, True)
    assert plan.reason == "IT8951 panels are driven only by ESP32 firmware; this device is NRF52840."


def test_panel_override_replaces_existing_drive_packets() -> None:
    config = _esp32()
    migrated = apply_migration(config, plan_migration(config, build_profile(FW3, config, None), DB))
    old = tuple(migrated.length_prefixed_packets)

    plan = plan_migration(migrated, build_profile(FW3, migrated, None), DB, panel_ic_type=GLASS_PANEL)

    assert plan.needed and not plan.blocked
    assert plan.remove == old
    assert plan.add == DB.entry_for(GLASS_PANEL, TargetClass.DEFAULT).packets  # type: ignore[union-attr]
    assert plan.remap_panel_ic_type == GLASS_PANEL
    assert apply_migration(migrated, plan).displays[0].panel_ic_type == GLASS_PANEL


def test_drive_packets_that_do_not_run_on_the_target_are_replaced() -> None:
    # 0x0001's default-class packets use WINDOW, which BG22 does not implement.
    default = DB.entry_for(EQUIVALENT_PANEL, TargetClass.DEFAULT).packets  # type: ignore[union-attr]
    system = dataclasses.replace(_config().system, ic_type=ICType.EFR32BG22)
    config = _esp32(system=system, length_prefixed_packets=list(default))
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert (plan.needed, plan.blocked, plan.target_class) == (True, False, TargetClass.BG22)
    assert plan.reason.startswith("stored drive packets do not run on this target (")
    assert "WINDOW/FILL" in plan.reason
    assert plan.remove == default
    assert plan.add == DB.entry_for(EQUIVALENT_PANEL, TargetClass.BG22).packets  # type: ignore[union-attr]


def test_device_flag_forces_a_migration_of_present_drive_data() -> None:
    config = apply_migration(_esp32(), plan_migration(_esp32(), build_profile(FW3, _esp32(), None), DB))

    plan = plan_migration(config, build_profile(FW3, config, IDENT_NEEDS_MIGRATION), DB)

    assert (plan.needed, plan.reason) == (True, "device asked for migration")


def test_config_with_an_unparsed_tail_blocks() -> None:
    config = _esp32(unparsed_tail=b"\x00\x7d\x01\x02")
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert plan.blocked
    assert plan.reason == "the config has packets this library cannot read; migrate it with the website toolbox"


def test_no_config_and_no_panel_id_blocks() -> None:
    plan = plan_migration(None, build_profile(FW3, None, IDENT_NEEDS_MIGRATION), DB)

    assert (plan.needed, plan.blocked) == (True, True)
    assert plan.reason.startswith("no display configured")


def test_config_without_a_display_blocks_even_with_a_panel_id() -> None:
    config = _config(displays=[])
    plan = plan_migration(config, build_profile(FW3, config, None), DB, panel_ic_type=EQUIVALENT_PANEL)

    assert plan.blocked
    assert plan.reason.startswith("no display configured")
    assert "board-specific" in plan.reason and "backup" in plan.reason and "toolbox" in plan.reason


def test_panel_not_driven_by_drive_packets_needs_nothing() -> None:
    config = _esp32(panel_ic_type=1500)
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert (plan.needed, plan.blocked, plan.reason) == (False, False, "this panel is not driven by drive packets")


def test_nrf52811_config_from_2x_is_remapped_to_the_3_0_panel_id() -> None:
    system = dataclasses.replace(_config().system, ic_type=ICType.NRF52811)
    config = _config(system=system, displays=[_display(panel_ic_type=1001)])
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert plan.target_class is TargetClass.NRF52811
    assert (plan.needed, plan.blocked, plan.panel_ic_type, plan.remap_panel_ic_type) == (True, False, 0xE002, 0xE002)
    assert plan.add == DB.entry_for(0xE002, TargetClass.NRF52811).packets  # type: ignore[union-attr]

    migrated = apply_migration(config, plan)

    assert migrated.displays[0].panel_ic_type == 0xE002
    # The one rewritten field; every other legacy byte is unchanged.
    expected = dataclasses.replace(config, displays=[dataclasses.replace(config.displays[0], panel_ic_type=0xE002)])
    assert _legacy_part(migrated) == _legacy_part(expected)


def test_check_fits_uses_the_identity_limits() -> None:
    config = apply_migration(_esp32(), plan_migration(_esp32(), build_profile(FW3, _esp32(), None), DB))

    assert check_fits(config, build_profile(FW3, config, None)) == []
    assert any("limit 3" in p for p in check_fits(config, build_profile(FW3, config, IDENT_MAX_STEPS_3)))
    assert any("600" in p for p in check_fits(config, build_profile(FW3, config, IDENT_MAX_CONFIG_600)))


def test_apply_refuses_packets_that_do_not_run_on_the_target() -> None:
    config = _esp32()
    plan = plan_migration(config, build_profile(FW3, config, None), DB)
    bad = dataclasses.replace(plan, target_class=TargetClass.NRF52811)  # 0x0001 default packets use WINDOW

    with pytest.raises(ValueError, match="does not run on this target"):
        apply_migration(config, bad)


def test_apply_refuses_a_blocked_plan() -> None:
    config = _esp32(panel_ic_type=768)
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    with pytest.raises(ValueError, match="blocked"):
        apply_migration(config, plan)


def test_apply_keeps_packets_that_are_not_drive_data() -> None:
    other = LengthPrefixedPacket(number=0, packet_type=0xB2, body=b"\x01\x02")  # action table
    config = _esp32(length_prefixed_packets=[other])
    plan = plan_migration(config, build_profile(FW3, config, None), DB)

    assert other in apply_migration(config, plan).length_prefixed_packets


def test_it8951_controller_packet_is_replaced_when_the_firmware_does_not_use_it() -> None:
    # Identity flags 0x29: BUILTIN_DRIVER (0x04) clear, so the stored 0xB3 is not in force.
    native = DB.native_controller_for(3000)
    assert native is not None
    old = LengthPrefixedPacket(number=7, packet_type=0xB3, body=bytes(12))
    config = _esp32(panel_ic_type=3000, length_prefixed_packets=[old])

    plan = plan_migration(config, build_profile(FW3, config, IDENT_MAX_STEPS_3), DB)

    assert (plan.needed, plan.blocked) == (True, False)
    assert plan.remove == (old,)
    assert plan.add == native.packets


def test_naming_the_configured_panel_rewrites_its_drive_data() -> None:
    config = apply_migration(_esp32(), plan_migration(_esp32(), build_profile(FW3, _esp32(), None), DB))

    plan = plan_migration(config, build_profile(FW3, config, None), DB, panel_ic_type=EQUIVALENT_PANEL)

    assert (plan.needed, plan.blocked, plan.remap_panel_ic_type) == (True, False, None)
    assert plan.remove == tuple(config.length_prefixed_packets)
