"""The drive database taken from a firmware release (manifest ``presets``), bundled copy as fallback."""

from __future__ import annotations

import gzip
import hashlib
import json
from importlib.resources import files
from typing import Any

import pytest

from opendisplay.firmware.releases import ReleaseManifest
from opendisplay.panels import PanelDatabase, TargetClass

_PANELS = files("opendisplay.panels")
_MAIN = gzip.decompress(_PANELS.joinpath("drive_presets.json.gz").read_bytes())
_NRF52811 = gzip.decompress(_PANELS.joinpath("drive_presets_nrf52811.json.gz").read_bytes())


def _release_main() -> bytes:
    """The bundled main file with one change only a release copy has: panel 0x0042 renamed."""
    doc = json.loads(_MAIN)
    doc["panels"]["0x0042"]["name"] = "FROM_THE_RELEASE"
    return json.dumps(doc).encode()


def _manifest(files_: dict[str, tuple[bytes, list[int]]], *, sha_of: dict[str, bytes] | None = None) -> ReleaseManifest:
    sha_of = sha_of or {}
    doc: dict[str, Any] = {
        "schema": "opendisplay-release/1",
        "version": "3.1.1",
        "boards": [],
        "artefacts": [],
        "presets": [
            {
                "file": name,
                "type": "drive-presets",
                "ident_targets": targets,
                "schema": "opendisplay.drive_presets",
                "schema_version": 1,
                "schema_minor": 4,
                "size": len(data),
                "sha256": hashlib.sha256(sha_of.get(name, data)).hexdigest(),
            }
            for name, (data, targets) in files_.items()
        ],
    }
    return ReleaseManifest.parse(json.dumps(doc).encode())


def _fetcher(data: dict[str, bytes], seen: list[str] | None = None) -> Any:
    async def fetch(name: str) -> bytes:
        if seen is not None:
            seen.append(name)
        return data[name]

    return fetch


async def test_the_release_database_is_used_when_the_release_has_one() -> None:
    main, small = _release_main(), _NRF52811
    manifest = _manifest({"drive_presets.json": (main, [1, 2, 3, 5]), "drive_presets_nrf52811.json": (small, [4])})
    seen: list[str] = []

    db = await PanelDatabase.for_release(
        manifest, _fetcher({"drive_presets.json": main, "drive_presets_nrf52811.json": small}, seen)
    )

    assert sorted(seen) == ["drive_presets.json", "drive_presets_nrf52811.json"]
    assert db.source == "release 3.1.1"
    entry = db.entry_for(0x0042, TargetClass.DEFAULT)
    assert entry is not None and entry.name == "FROM_THE_RELEASE"
    bundled = PanelDatabase.load()
    # the nRF52811 file came from the release as well, and still serves that class
    some = next(i for i in bundled.panel_ids() if bundled.entry_for(i, TargetClass.NRF52811) is not None)
    assert db.entry_for(some, TargetClass.NRF52811) is not None


async def test_a_release_without_presets_uses_the_bundled_copy() -> None:
    manifest = _manifest({})

    db = await PanelDatabase.for_release(manifest, _fetcher({}))

    assert db.source == "bundled"
    entry = db.entry_for(0x0042, TargetClass.DEFAULT)
    assert entry is not None and entry.name != "FROM_THE_RELEASE"


async def test_a_file_that_does_not_match_the_manifest_is_never_used(caplog: pytest.LogCaptureFixture) -> None:
    main = _release_main()
    manifest = _manifest({"drive_presets.json": (main, [1, 2, 3, 5])}, sha_of={"drive_presets.json": b"other"})

    db = await PanelDatabase.for_release(manifest, _fetcher({"drive_presets.json": main}))

    assert db.source == "bundled"
    entry = db.entry_for(0x0042, TargetClass.DEFAULT)
    assert entry is not None and entry.name != "FROM_THE_RELEASE"
    assert "SHA-256" in caplog.text


async def test_a_failed_download_uses_the_bundled_copy(caplog: pytest.LogCaptureFixture) -> None:
    main = _release_main()
    manifest = _manifest({"drive_presets.json": (main, [1, 2, 3, 5])})

    async def fetch(name: str) -> bytes:
        raise OSError("offline")

    db = await PanelDatabase.for_release(manifest, fetch)

    assert db.source == "bundled"
    assert "offline" in caplog.text


async def test_the_bundled_copy_says_where_it_came_from() -> None:
    assert PanelDatabase.load().source == "bundled"
