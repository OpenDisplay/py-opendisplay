"""A Firmware_Unified release's ``manifest.json`` (schema ``opendisplay-release/1``).

The manifest lists, per board, which files each flow uses (``usb-install``, ``usb-update``,
``ble-ota``, ``ble-ota-from-2x``) and with which protocol, plus every file's size and SHA-256
(Firmware_Unified ``docs/RELEASE_ARTEFACTS.md``). From 3.1.0 on it also lists the panel drive
databases (``presets``), one per group of READ_IDENT ``fw_target`` values. No I/O: the caller
fetches the bytes.

``READ_IDENT`` does not report the board, and several ESP32 boards share a chip (flash, PSRAM and
console differ), so :meth:`ReleaseManifest.boards_for_chip` returns every candidate; a caller picks
on its own only when there is exactly one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ..models.ident import FirmwareTarget

SCHEMA: Final = "opendisplay-release/1"

# MCUboot test-swap (nRF52840, nRF54) and ESP32's two app slots revert an image that never
# confirms; BG22 (AppLoader, one slot), nRF52811 and Telink cannot (Firmware_Unified UPGRADING.md).
_ROLLBACK_TARGETS: Final = frozenset({FirmwareTarget.ESP32, FirmwareTarget.NRF52840, FirmwareTarget.NRF54})


@dataclass(frozen=True)
class Artefact:
    """One release file: its name, board, kind (``type``), flash offset, size and SHA-256."""

    file: str
    board: str | None
    kind: str
    offset: int | None
    size: int
    sha256: str


@dataclass(frozen=True)
class BoardRelease:  # pylint: disable=too-many-instance-attributes
    """What one board's release offers: its Bluetooth update, its USB install, and the first move
    from 2.x over Bluetooth where the board has one (``ble-ota-from-2x``: BG22, nRF54)."""

    board: str
    target: str
    chip: str
    ble_ota_files: tuple[str, ...]
    ble_ota_protocol: str | None  # "mcumgr-smp", "silabs-apploader"
    ble_ota_erases_config: bool | None
    usb_install_files: tuple[str, ...]
    usb_install_erases_config: bool | None
    legacy_names: tuple[str, ...] = ()
    usb_install_protocol: str | None = None  # "adafruit-serial-dfu", "swd"; None: the website flasher
    from_2x_files: tuple[str, ...] = ()
    from_2x_protocol: str | None = None
    from_2x_erases_config: bool | None = None


@dataclass(frozen=True)
class PresetsFile:
    """One drive database of the release: the READ_IDENT ``fw_target`` values it serves, its
    schema minor, size and SHA-256."""

    file: str
    ident_targets: frozenset[int]
    schema_minor: int
    size: int
    sha256: str


def _flow(flows: Mapping[str, Any], name: str) -> tuple[tuple[str, ...], str | None, bool | None]:
    flow = flows.get(name) or {}
    return tuple(flow.get("files") or ()), flow.get("protocol"), flow.get("erases_config")


@dataclass(frozen=True)
class ReleaseManifest:
    """A parsed ``manifest.json``."""

    version: str
    commit: str
    signing: Mapping[str, str]
    boards: tuple[BoardRelease, ...]
    artefacts: tuple[Artefact, ...]
    presets: tuple[PresetsFile, ...] = ()

    @classmethod
    def parse(cls, data: bytes) -> ReleaseManifest:
        """Parse a manifest.

        Raises:
            ValueError: Not JSON, or another schema than ``opendisplay-release/1``.
        """
        doc = json.loads(data)
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"Unsupported release manifest schema {doc.get('schema')!r} (expected {SCHEMA})")
        boards = []
        for entry in doc["boards"]:
            ota_files, ota_protocol, ota_erases = _flow(entry["flows"], "ble-ota")
            usb_files, usb_protocol, usb_erases = _flow(entry["flows"], "usb-install")
            from_2x_files, from_2x_protocol, from_2x_erases = _flow(entry["flows"], "ble-ota-from-2x")
            boards.append(
                BoardRelease(
                    board=entry["board"],
                    target=entry["target"],
                    chip=entry["chip"],
                    ble_ota_files=ota_files,
                    ble_ota_protocol=ota_protocol,
                    ble_ota_erases_config=ota_erases,
                    usb_install_files=usb_files,
                    usb_install_erases_config=usb_erases,
                    legacy_names=tuple(entry.get("legacy_names") or ()),
                    usb_install_protocol=usb_protocol,
                    from_2x_files=from_2x_files,
                    from_2x_protocol=from_2x_protocol,
                    from_2x_erases_config=from_2x_erases,
                )
            )
        artefacts = tuple(
            Artefact(
                file=a["file"],
                board=a.get("board"),
                kind=a["type"],
                offset=a.get("offset"),
                size=a["size"],
                sha256=a["sha256"],
            )
            for a in doc["artefacts"]
        )
        presets = tuple(
            PresetsFile(
                file=p["file"],
                ident_targets=frozenset(int(t) for t in p.get("ident_targets") or ()),
                schema_minor=int(p.get("schema_minor", 0)),
                size=p["size"],
                sha256=p["sha256"],
            )
            for p in doc.get("presets") or ()
        )
        # The presets are release files too, so whatever fetches and checks artefacts takes them.
        artefacts += tuple(
            Artefact(file=p.file, board=None, kind="drive-presets", offset=None, size=p.size, sha256=p.sha256)
            for p in presets
            if all(a.file != p.file for a in artefacts)
        )
        return cls(
            version=doc["version"],
            commit=doc.get("commit", ""),
            signing=dict(doc.get("signing") or {}),
            boards=tuple(boards),
            artefacts=artefacts,
            presets=presets,
        )

    def board(self, name: str) -> BoardRelease | None:
        """The board called ``name``, or ``None``."""
        return next((b for b in self.boards if b.board == name), None)

    def boards_for_chip(self, chip: str) -> list[BoardRelease]:
        """Every board built for ``chip`` (``"esp32s3"``, ``"efr32bg22"``, ...)."""
        return [b for b in self.boards if b.chip == chip]

    def artefact(self, file: str) -> Artefact | None:
        """The release file called ``file``, or ``None``."""
        return next((a for a in self.artefacts if a.file == file), None)


def can_roll_back(target: FirmwareTarget | int | None) -> bool:
    """Whether this target returns to its old image when a new one never confirms."""
    return target in _ROLLBACK_TARGETS
