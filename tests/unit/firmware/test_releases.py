"""The release manifest (Firmware_Unified `manifest.json`, schema opendisplay-release/1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opendisplay.firmware.releases import ReleaseManifest, can_roll_back
from opendisplay.models.ident import FirmwareTarget

MANIFEST = (Path(__file__).parents[2] / "fixtures" / "firmware" / "manifest-3.0.2.json").read_bytes()


@pytest.fixture
def manifest() -> ReleaseManifest:
    return ReleaseManifest.parse(MANIFEST)


def test_the_release_identity(manifest: ReleaseManifest) -> None:
    assert manifest.version == "3.0.2"
    assert manifest.commit == "a519151badc65b498d8266371532a058cf47aa7b"
    assert manifest.signing == {"bg22": "unsigned", "esp32": "unsigned", "mcuboot": "debug-key"}


def test_an_esp32_board_installs_its_merged_image_over_usb_and_erases_the_config(manifest: ReleaseManifest) -> None:
    board = manifest.board("s3-n32r8-extuart")

    assert board is not None
    assert board.target == "esp32-idf" and board.chip == "esp32s3"
    assert board.usb_install_files == ("opendisplay-s3-n32r8-extuart-merged.bin",)
    assert board.usb_install_erases_config is True
    assert board.ble_ota_protocol == "mcumgr-smp"
    assert board.ble_ota_files == ("opendisplay-s3-n32r8-extuart-app.bin",)


def test_the_bg22_updates_through_its_apploader_and_keeps_the_config(manifest: ReleaseManifest) -> None:
    board = manifest.board("bg22")

    assert board is not None
    assert board.ble_ota_protocol == "silabs-apploader"
    assert board.ble_ota_files == ("opendisplay-bg22-ota.gbl",)
    assert board.ble_ota_erases_config is False


def test_the_first_move_from_2x_over_bluetooth_where_a_board_has_one(manifest: ReleaseManifest) -> None:
    bg22, nrf54, esp32 = manifest.board("bg22"), manifest.board("xiao_nrf54l15"), manifest.board("s3-n32r8")

    assert bg22 is not None and nrf54 is not None and esp32 is not None
    assert (bg22.from_2x_files, bg22.from_2x_protocol) == (("opendisplay-bg22-ota.gbl",), "silabs-apploader")
    assert bg22.from_2x_erases_config is False
    assert (nrf54.from_2x_files, nrf54.from_2x_protocol) == (("opendisplay-xiao_nrf54l15-ota.bin",), "mcumgr-smp")
    assert (esp32.from_2x_files, esp32.from_2x_protocol) == ((), None)  # USB only
    assert esp32.usb_install_protocol is None and nrf54.usb_install_protocol == "swd"


def test_an_unknown_board_is_none(manifest: ReleaseManifest) -> None:
    assert manifest.board("nope") is None


def test_boards_by_chip(manifest: ReleaseManifest) -> None:
    assert [b.board for b in manifest.boards_for_chip("efr32bg22")] == ["bg22"]
    assert [b.board for b in manifest.boards_for_chip("nrf52840")] == ["xiao_nrf52840"]
    assert len(manifest.boards_for_chip("esp32s3")) == 6  # not derivable from the chip: the user names it


def test_each_file_has_its_size_and_hash(manifest: ReleaseManifest) -> None:
    artefact = manifest.artefact("opendisplay-bg22-ota.gbl")

    assert artefact is not None
    assert artefact.size == 228016
    assert artefact.sha256 == "60277b1d2ce9cb657429ee81521bd133be6d1b26a7df788577d79e8e991ffbae"


def test_another_schema_is_refused() -> None:
    with pytest.raises(ValueError, match="schema"):
        ReleaseManifest.parse(b'{"schema": "opendisplay-release/2", "boards": [], "artefacts": []}')


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (FirmwareTarget.ESP32, True),
        (FirmwareTarget.NRF52840, True),
        (FirmwareTarget.NRF54, True),
        (FirmwareTarget.EFR32BG22, False),
        (FirmwareTarget.NRF52811, False),
        (FirmwareTarget.TELINK, False),
        (None, False),
        (0x7F, False),
    ],
)
def test_which_targets_can_roll_back(target: FirmwareTarget | int | None, expected: bool) -> None:
    assert can_roll_back(target) is expected


MANIFEST_311 = (Path(__file__).parents[2] / "fixtures" / "firmware" / "manifest-3.1.1.json").read_bytes()


def test_a_release_lists_its_drive_presets_and_the_targets_each_serves() -> None:
    manifest = ReleaseManifest.parse(MANIFEST_311)

    assert [(p.file, p.ident_targets) for p in manifest.presets] == [
        ("drive_presets.json", frozenset({1, 2, 3, 5})),
        ("drive_presets_nrf52811.json", frozenset({4})),
    ]
    main = manifest.presets[0]
    assert main.schema_minor == 4 and main.size == 959091
    assert main.sha256 == "f201ec416c25903e5d589183dac8613b3780e308392bad17657b365c0a372922"


def test_drive_presets_are_release_files_like_any_other() -> None:
    """So the downloader (and Home Assistant's fetcher) checks them against the manifest too."""
    manifest = ReleaseManifest.parse(MANIFEST_311)

    artefact = manifest.artefact("drive_presets_nrf52811.json")
    assert artefact is not None and artefact.kind == "drive-presets" and artefact.board is None
    assert artefact.sha256 == "dba76087e6345cdb915a02f0060df8a8fead04f9b18a2d9a661eda0f862f1b94"


def test_a_release_before_3_1_has_no_drive_presets(manifest: ReleaseManifest) -> None:
    assert manifest.presets == ()
