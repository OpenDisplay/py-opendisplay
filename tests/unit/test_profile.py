"""DeviceProfile: one description of a device, built from firmware version, config and identity."""

from __future__ import annotations

import dataclasses
import struct
from pathlib import Path
from typing import Any

import pytest

from opendisplay import DeviceProfile, Generation, ProfileMode, build_profile
from opendisplay.models.config import (
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.models.enums import ColorScheme, ICType
from opendisplay.models.firmware import FirmwareVersion
from opendisplay.models.ident import FirmwareTarget
from opendisplay.models.panel_drive import LengthPrefixedPacket
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.responses import parse_read_ident

FW2: FirmwareVersion = {"major": 2, "minor": 27, "patch": 1, "sha": "a" * 40}
FW3: FirmwareVersion = {"major": 3, "minor": 0, "patch": 0, "sha": "0" * 40}

# Captured from the E1002 on Firmware_Unified 3.0.2 with no config: ESP32, no display records,
# flags 0x29, max_config 4000.
IDENT_V3_NO_DISPLAY = parse_read_ident(bytes.fromhex("004604010029a00f800003"))
# One 800x480 display record and flags 0x0B (config-driven, needs migration, ...), max_config 4000.
IDENT_NEEDS_MIGRATION = parse_read_ident(bytes.fromhex("004604010123002003e001040ba00f800003"))

ALPHA_CONFIG = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)


def _display(**overrides: Any) -> DisplayConfig:
    base: dict[str, Any] = dict(
        instance_number=0,
        display_technology=1,
        panel_ic_type=0x0023,
        pixel_width=800,
        pixel_height=480,
        active_width_mm=0,
        active_height_mm=0,
        tag_type=0,
        rotation=90,
        reset_pin=0xFF,
        busy_pin=0xFF,
        dc_pin=0xFF,
        cs_pin=0xFF,
        data_pin=0xFF,
        partial_update_support=0,
        color_scheme=ColorScheme.BWGBRY,
        transmission_modes=0x08,  # DIRECT_WRITE only
        clk_pin=0xFF,
        reserved_pins=b"\xff" * 7,
        full_update_mC=0,
        reserved=b"\x00" * 13,
    )
    base.update(overrides)
    return DisplayConfig(**base)


def _config(*, ic_type: int = ICType.ESP32_S3, **overrides: Any) -> GlobalConfig:
    """A 2.x-style config (no length-prefixed packets) with one 800x480 display."""
    base: dict[str, Any] = dict(
        system=SystemConfig(
            ic_type=ic_type,
            communication_modes=0x01,
            device_flags=0,
            pwr_pin=0xFF,
            reserved=b"\x00" * 16,
            pwr_pin_2=0,
            pwr_pin_3=0,
        ),
        manufacturer=ManufacturerData(
            manufacturer_id=1,
            board_type=1,
            board_revision=1,
            reserved=b"\x00" * 14,
            simple_config_driver_index=0,
            simple_config_display_index=0,
            simple_config_power_index=0,
            simple_config_configured_at=0,
        ),
        power=PowerOption(
            power_mode=1,
            battery_capacity_mah=(3000).to_bytes(3, "little"),
            sleep_timeout_ms=40000,
            tx_power=8,
            sleep_flags=0,
            battery_sense_pin=1,
            battery_sense_enable_pin=0x28,
            battery_sense_flags=0,
            capacity_estimator=5,
            voltage_scaling_factor=0xA1,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            charge_enable_pin=0,
            charge_state_pin=0,
            charger_flags=0,
            min_wake_time_seconds=0,
            screen_timeout_seconds=0,
            reserved=b"\x00" * 4,
        ),
        displays=[_display()],
    )
    base.update(overrides)
    return GlobalConfig(**base)


def test_fresh_3_0_device_without_config() -> None:
    p = build_profile(FW3, None, IDENT_V3_NO_DISPLAY)

    assert isinstance(p, DeviceProfile)
    assert (p.generation, p.target, p.configured, p.needs_migration) == (
        Generation.V3,
        FirmwareTarget.ESP32,
        False,
        False,
    )
    assert (p.width, p.height, p.color_scheme, p.rotation) == (None, None, None, None)
    assert p.max_config == 4000
    assert (p.ident, p.ident_missing) == (IDENT_V3_NO_DISPLAY, False)


def test_3_0_device_whose_config_lacks_drive_data() -> None:
    p = build_profile(FW3, _config(), IDENT_NEEDS_MIGRATION)

    assert (p.configured, p.needs_migration, p.width, p.height) == (True, True, 800, 480)
    assert (p.color_scheme, p.rotation) == (ColorScheme.BWGBRY, 90)


def test_3_0_device_without_identity_tail_needs_migration_when_config_has_no_drive_data() -> None:
    assert build_profile(FW3, _config(), None, ident_missing=True).needs_migration is True


def test_built_in_controller_driver_counts_as_drive_data() -> None:
    controller = LengthPrefixedPacket(number=5, packet_type=0xB3, body=b"\x00\x01")

    assert build_profile(FW3, _config(length_prefixed_packets=[controller]), None).needs_migration is False


def test_identity_flag_decides_migration_when_the_identity_has_a_tail() -> None:
    # The config lacks drive packets, but the device says it does not need migrating.
    assert build_profile(FW3, _config(), IDENT_V3_NO_DISPLAY).needs_migration is False


def test_config_driven_device_reports_its_modes_and_code_map() -> None:
    p = build_profile(FW3, ALPHA_CONFIG, None, ident_missing=True)

    assert p.drive_modes == (
        ProfileMode(mode_index=0, name="full", color_scheme=None, passes=1),  # 0xFF: inherits 0x20
        ProfileMode(mode_index=1, name="colour16", color_scheme=10, passes=3),  # BWRY16 lives in the mode
    )
    assert p.drive_mode_names == ("full", "colour16")
    assert p.code_map == (0, 1, 3, 2)
    assert (p.needs_migration, p.ident_missing) == (False, True)  # has drive packets; Review Focus 1
    assert p.target is FirmwareTarget.EFR32BG22  # mapped from the config's ic_type
    assert (p.width, p.height) == (184, 360)


def test_drive_modes_follow_mode_index_not_packet_order() -> None:
    lp = ALPHA_CONFIG.length_prefixed_packets
    modes = [pkt for pkt in lp if pkt.packet_type == 0xAE]
    others = [pkt for pkt in lp if pkt.packet_type != 0xAE]
    swapped = dataclasses.replace(ALPHA_CONFIG, length_prefixed_packets=others + modes[::-1])

    assert build_profile(FW3, swapped, None).drive_mode_names == ("full", "colour16")


def test_2x_device_is_described_from_its_config_alone() -> None:
    p = build_profile(FW2, _config(), None)

    assert (p.generation, p.ident, p.ident_missing, p.needs_migration, p.max_config) == (
        Generation.V2,
        None,
        False,
        False,
        4096,
    )
    assert p.target is FirmwareTarget.ESP32
    assert p.drive_modes == ()
    assert p.code_map is None


def test_2x_needs_no_migration_even_with_a_config_without_drive_data() -> None:
    assert build_profile(FW2, _config(), None).needs_migration is False


@pytest.mark.parametrize(
    ("ic_type", "target"),
    [
        (ICType.NRF52840, FirmwareTarget.NRF52840),
        (ICType.ESP32_S3, FirmwareTarget.ESP32),
        (ICType.ESP32_C3, FirmwareTarget.ESP32),
        (ICType.ESP32_C6, FirmwareTarget.ESP32),
        (ICType.NRF52811, FirmwareTarget.NRF52811),
        (ICType.EFR32BG22, FirmwareTarget.EFR32BG22),
        (0x7F, None),
    ],
)
def test_target_is_mapped_from_ic_type_without_an_identity(ic_type: int, target: FirmwareTarget | None) -> None:
    assert build_profile(FW2, _config(ic_type=ic_type), None).target == target


def test_identity_target_wins_over_the_config() -> None:
    p = build_profile(FW3, _config(ic_type=ICType.NRF52840), IDENT_NEEDS_MIGRATION)

    assert p.target is FirmwareTarget.ESP32


def test_no_config_and_no_identity_has_no_target() -> None:
    assert build_profile(FW2, None, None).target is None


def test_width_falls_back_to_the_identity_display_record() -> None:
    p = build_profile(FW3, None, IDENT_NEEDS_MIGRATION)

    assert (p.width, p.height, p.color_scheme, p.rotation) == (800, 480, ColorScheme.BWGBRY, None)
    assert p.configured is False


def test_3_0_firmware_is_v3_even_without_an_identity() -> None:  # Review Focus 1
    p = build_profile(FW3, None, None, ident_missing=True)

    assert (p.generation, p.ident, p.ident_missing) == (Generation.V3, None, True)


def test_config_without_displays_is_not_configured() -> None:  # Review Focus 2
    p = build_profile(FW2, _config(displays=[]), None)

    assert (p.configured, p.width, p.height, p.supports_partial) == (False, None, None, False)


def test_3_0_config_without_displays_does_not_need_migration() -> None:  # Review Focus 2
    assert build_profile(FW3, _config(displays=[]), None).needs_migration is False


def test_unknown_colour_scheme_is_kept_as_its_raw_value() -> None:  # Review Focus 3
    # 0x20 holds the base scheme; mode-level schemes such as BWRY16 never go there, but a newer
    # firmware may define a 0x20 value this library does not know.
    p = build_profile(FW3, _config(displays=[_display(color_scheme=0x7E)]), None)

    assert p.color_scheme == 0x7E
    assert not isinstance(p.color_scheme, ColorScheme)


def test_unknown_colour_scheme_in_the_identity_is_kept_as_its_raw_value() -> None:  # Review Focus 3
    ident = parse_read_ident(bytes.fromhex("004604010123002003e0017e0ba00f800003"))

    assert build_profile(FW3, None, ident).color_scheme == 0x7E


def test_transfer_support_follows_the_display_config() -> None:
    full = _config(displays=[_display(partial_update_support=1, transmission_modes=0x10 | 0x02)])
    none = _config(displays=[_display(partial_update_support=0, transmission_modes=0x08)])
    streaming = _config(displays=[_display(transmission_modes=0x01)])

    p = build_profile(FW2, full, None)
    assert (p.supports_partial, p.supports_pipe, p.supports_compression) == (True, True, True)
    p = build_profile(FW2, none, None)
    assert (p.supports_partial, p.supports_pipe, p.supports_compression) == (False, False, False)
    assert build_profile(FW2, streaming, None).supports_compression is True


def test_profile_is_immutable() -> None:
    p = build_profile(FW2, _config(), None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        p.width = 1  # type: ignore[misc]


# ── firmware 3.0 drive modes and partial support ─────────────────────────────
# Firmware_Unified docs/CONFIG_DRIVEN_DISPLAY.md "Mode conventions": a mode is identified by its
# mode_index, the first packet per (instance, mode_index) wins, names match case-insensitively,
# and the mode named "partial" is what enables partial updates; the 0x20 flag alone does not.


def _mode(index: int, name: str, *, instance: int = 0, scheme: int = 0xFF, passes: int = 1) -> LengthPrefixedPacket:
    head = struct.pack(
        "<BB12sBBbbB8s8sH", instance, index, name.encode(), scheme, passes, 0, 0, 0, b"\xff" * 8, bytes(8), 0
    )
    return LengthPrefixedPacket(number=index, packet_type=0xAE, body=head)


def _v3(*modes: LengthPrefixedPacket, scheme: int = ColorScheme.MONO, partial_flag: int = 0, **kw: Any) -> GlobalConfig:
    display = _display(color_scheme=scheme, partial_update_support=partial_flag)
    return _config(displays=[display], length_prefixed_packets=[_mode(0, "full"), *modes], **kw)


def test_v3_partial_comes_from_a_partial_mode_not_the_display_flag() -> None:
    assert build_profile(FW3, _v3(_mode(1, "partial")), None).supports_partial is True
    assert build_profile(FW3, _v3(partial_flag=1), None).supports_partial is False


def test_v3_partial_mode_name_is_matched_case_insensitively() -> None:
    assert build_profile(FW3, _v3(_mode(2, "PARTIAL")), None).supports_partial is True


def test_v3_partial_mode_of_another_display_does_not_count() -> None:
    assert build_profile(FW3, _v3(_mode(1, "partial", instance=1)), None).supports_partial is False


def test_v3_partial_needs_a_scheme_with_partial_geometry() -> None:
    # od_color_direct_geometry sets partial_supported for MONO only.
    assert build_profile(FW3, _v3(_mode(1, "partial"), scheme=ColorScheme.BWR), None).supports_partial is False


def test_v3_partial_is_off_while_the_device_needs_migration() -> None:
    assert build_profile(FW3, _v3(_mode(1, "partial")), IDENT_NEEDS_MIGRATION).supports_partial is False


def test_v3_partial_is_off_on_efr32bg22() -> None:
    # targets/efr32bg22-slc: partial_enabled is always false.
    config = _v3(_mode(1, "partial"), ic_type=ICType.EFR32BG22)
    assert build_profile(FW3, config, None).supports_partial is False


def test_v2_partial_still_follows_the_display_flag() -> None:
    assert build_profile(FW2, _v3(partial_flag=1), None).supports_partial is True
    assert build_profile(FW2, _v3(_mode(1, "partial")), None).supports_partial is False


def test_first_mode_packet_per_index_wins_and_other_displays_are_left_out() -> None:
    config = _v3(_mode(1, "fast"), _mode(1, "partial"), _mode(2, "other", instance=1))
    p = build_profile(FW3, config, None)

    assert p.drive_mode_names == ("full", "fast")
    assert p.supports_partial is False  # the duplicate "partial" at index 1 is ignored
