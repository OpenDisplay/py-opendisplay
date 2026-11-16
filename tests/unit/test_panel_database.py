"""The vendored panel drive database: loading it and querying it as the website's od-v3.js does."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from importlib.resources import files

import pytest

from opendisplay.models.config import GlobalConfig
from opendisplay.models.ident import FirmwareTarget
from opendisplay.panels import NativeController, PanelDatabase, PanelEntry, TargetClass, target_class_for
from opendisplay.panels.database import check_drive_packets_fit, needs_drive_data

DB = PanelDatabase.load()
_PANELS = files("opendisplay.panels")
_RAW = json.loads(gzip.decompress(_PANELS.joinpath("drive_presets.json.gz").read_bytes()))


def _minimal_config(packets: tuple) -> GlobalConfig:
    from test_profile import _config  # same synthetic config the profile tests use

    return _config(length_prefixed_packets=list(packets))


def test_packaged_files_match_the_recorded_sha256() -> None:
    record = _PANELS.joinpath("SOURCE.md").read_text(encoding="utf-8")
    for name in ("drive_presets.json.gz", "drive_presets_nrf52811.json.gz"):
        row = next(line for line in record.splitlines() if f"`{name}`" in line)
        expected = re.findall(r"`([0-9a-f]{64})`", row)[0]

        assert hashlib.sha256(gzip.decompress(_PANELS.joinpath(name).read_bytes())).hexdigest() == expected


def test_database_loads_and_declares_its_schema() -> None:
    assert DB.schema_minor >= 3
    assert DB.caps["max_steps"] == 128


def test_known_panel_has_drive_packets_that_start_with_the_panel_packet() -> None:
    entry = DB.entry_for(0x0001, TargetClass.DEFAULT)

    assert isinstance(entry, PanelEntry)
    assert (entry.name, entry.width, entry.height, entry.color_scheme) == ("EP42_400x300", 400, 300, 0)
    assert entry.usable and entry.blocked_reason is None
    assert entry.packets[0].packet_type == 0xB1
    assert {p.packet_type for p in entry.packets} <= {0xAE, 0xAF, 0xB0, 0xB1}
    assert entry.drive_bytes == 1087
    assert entry.target_class is TargetClass.DEFAULT


def test_the_bg22_view_has_its_own_packets() -> None:
    default = DB.entry_for(0x0001, TargetClass.BG22)
    assert default is not None and default.usable
    assert default.packets != DB.entry_for(0x0001, TargetClass.DEFAULT).packets  # type: ignore[union-attr]


@pytest.mark.parametrize("target_class", list(TargetClass))
def test_every_usable_entry_decodes_with_the_library_models(target_class: TargetClass) -> None:
    usable = 0
    for panel_ic_type in DB.panel_ids():
        entry = DB.entry_for(panel_ic_type, target_class)
        assert entry is not None
        if not entry.usable:
            assert entry.blocked_reason and entry.packets == ()
            continue
        usable += 1
        config = _minimal_config(entry.packets)
        assert len(config.display_panels) == 1, hex(panel_ic_type)
        assert any(mode.mode_index == 0 for mode in config.display_modes), hex(panel_ic_type)
        assert all(lut.crc_valid for lut in config.display_luts), hex(panel_ic_type)
        assert check_drive_packets_fit(entry.packets, target_class) == []
    assert usable > 0


def test_unknown_panel_has_no_entry() -> None:  # Review Focus 1
    assert DB.entry_for(0x7FFF, TargetClass.DEFAULT) is None


def test_native_controller_board() -> None:  # Review Focus 4
    native = DB.native_controller_for(3000)

    assert isinstance(native, NativeController)
    assert (native.name, native.board, native.controller_class) == (
        "ED103TC2_1872x1404",
        "Seeed reTerminal E1003",
        "IT8951",
    )
    assert [p.packet_type for p in native.packets] == [0xB3]
    assert DB.native_controller_for(0x0001) is None


def test_entry_that_does_not_fit_a_class_says_why() -> None:  # Review Focus 2
    # 0x0001 has no nRF52811 view, and its default packets use WINDOW, which nRF52811 lacks.
    entry = DB.entry_for(0x0001, TargetClass.NRF52811)

    assert entry is not None and not entry.usable
    assert entry.packets == ()
    assert entry.blocked_reason is not None
    assert entry.blocked_reason.startswith("Not available on this target: ")
    assert "WINDOW/FILL" in entry.blocked_reason


def test_glass_verified_panels_are_marked() -> None:
    in_file = sum(1 for p in _RAW["panels"].values() if p["targets"]["default"]["verified"] == "glass")
    entries = [DB.entry_for(i, TargetClass.DEFAULT) for i in DB.panel_ids()]
    glass = [e for e in entries if e is not None and e.usable and e.verified == "glass"]

    assert len(glass) == in_file == 7


def test_nrf52811_panels_exist_only_on_their_class() -> None:
    own = DB.entry_for(0xE002, TargetClass.NRF52811)
    other = DB.entry_for(0xE002, TargetClass.DEFAULT)

    assert own is not None and own.usable and own.verified == "glass"
    assert own.legacy_panel_ic_type == 1001
    assert other is not None and not other.usable
    assert other.blocked_reason == "Only on nRF52811 firmware 3.x."


def _doc(panels: dict) -> dict:
    return {"schema": "opendisplay.drive_presets", "schema_version": 1, "schema_minor": 3, "caps": {}, "panels": panels}


_PANEL_PACKET = {"id": 0xB1, "number": 0, "body_hex": "00" * 13}


def test_alias_borrows_its_targets_packets() -> None:
    db = PanelDatabase(
        _doc(
            {
                "0x0010": {"name": "REAL", "status": "glass", "verified": "glass", "packets": [_PANEL_PACKET]},
                "0x0011": {"name": "ALIAS", "alias_of": "0x0010"},
            }
        )
    )
    alias = db.entry_for(0x0011, TargetClass.DEFAULT)

    assert alias is not None and alias.usable
    assert (alias.panel_ic_type, alias.name) == (0x0011, "ALIAS")
    assert [p.packet_type for p in alias.packets] == [0xB1]


def test_legacy_packet_ids_in_a_preset_are_ignored() -> None:
    legacy = {"id": 0x20, "number": 0, "body_hex": "00" * 46}
    db = PanelDatabase(_doc({"0x0010": {"name": "P", "status": "glass", "packets": [legacy, _PANEL_PACKET]}}))

    entry = db.entry_for(0x0010, TargetClass.DEFAULT)
    assert entry is not None and [p.packet_type for p in entry.packets] == [0xB1]


def test_a_file_with_another_schema_is_refused() -> None:
    with pytest.raises(ValueError, match="schema"):
        PanelDatabase({**_doc({}), "schema": "something.else"})


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (FirmwareTarget.ESP32, TargetClass.DEFAULT),
        (FirmwareTarget.NRF54, TargetClass.DEFAULT),
        (FirmwareTarget.NRF52840, TargetClass.DEFAULT),
        (FirmwareTarget.EFR32BG22, TargetClass.BG22),
        (FirmwareTarget.NRF52811, TargetClass.NRF52811),
        (FirmwareTarget.TELINK, TargetClass.BG22),  # unknown to the database: the restrictive class
        (0x7F, TargetClass.BG22),
        (None, TargetClass.BG22),
    ],
)
def test_target_class_for(target: FirmwareTarget | int | None, expected: TargetClass) -> None:
    assert target_class_for(target) is expected


@pytest.mark.parametrize(
    ("panel_ic_type", "needs"),
    [
        (0x0001, True),
        (999, True),
        (0, False),
        (1000, False),
        (3000, False),
        (0xE000, True),
        (0xE1FF, True),
        (0xE200, False),
    ],
)
def test_needs_drive_data(panel_ic_type: int, needs: bool) -> None:
    assert needs_drive_data(panel_ic_type) is needs
    assert DB.needs_drive_data(panel_ic_type) is needs


def test_fit_check_names_the_limit() -> None:
    entry = DB.entry_for(0x0001, TargetClass.DEFAULT)
    assert entry is not None

    problems = check_drive_packets_fit(entry.packets, TargetClass.DEFAULT, max_steps=3)

    assert problems and all("limit 3" in p for p in problems if "steps" in p)


def test_nrf52811_panel_for_a_2x_firmware_nrf_id() -> None:
    assert DB.nrf52811_panel_for(1001) == 0xE002
    assert DB.nrf52811_panel_for(1016) == 0xE011
    assert DB.nrf52811_panel_for(1002) is None  # no preset
    assert DB.nrf52811_panel_for(0x0001) is None  # not a Firmware_NRF 2.x id


def test_nrf52811_panel_for_prefers_entries_derived_from_the_2x_driver() -> None:
    def panel(model_id: int | None) -> dict:
        legacy = {"panel_ic_type": 1001, **({"model_id": model_id} if model_id is not None else {})}
        return {"name": "P", "status": "glass", "legacy": legacy, "packets": [_PANEL_PACKET]}

    db = PanelDatabase(_doc({}), {TargetClass.NRF52811: _doc({"0xE001": panel(None), "0xE005": panel(4)})})

    assert db.nrf52811_panel_for(1001) == 0xE005
