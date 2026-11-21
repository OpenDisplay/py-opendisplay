"""Release files from GitHub: the manifest by tag (or the latest), files checked and cached."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from opendisplay.firmware import download

MANIFEST = (Path(__file__).parents[2] / "fixtures" / "firmware" / "manifest-3.0.2.json").read_bytes()
BASE = "https://github.com/OpenDisplay/Firmware_Unified/releases/download/3.0.2/"
GBL = "opendisplay-bg22-ota.gbl"


class _Web:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        if url not in self.files:
            raise download.DownloadError(f"404 {url}")
        return self.files[url]


def _manifest_for(image: bytes) -> bytes:
    doc = json.loads(MANIFEST)
    for artefact in doc["artefacts"]:
        if artefact["file"] == GBL:
            artefact["sha256"], artefact["size"] = hashlib.sha256(image).hexdigest(), len(image)
    return json.dumps(doc).encode()


async def test_the_latest_release_is_resolved_then_its_manifest_read(tmp_path: Path) -> None:
    web = _Web({download.LATEST_URL: json.dumps({"tag_name": "3.0.2"}).encode(), BASE + "manifest.json": MANIFEST})

    manifest = await download.ReleaseDownloader(cache=tmp_path, get=web).manifest()

    assert manifest.version == "3.0.2"
    assert web.asked == [download.LATEST_URL, BASE + "manifest.json"]


async def test_a_file_is_checked_against_the_manifest_and_cached(tmp_path: Path) -> None:
    image = b"\xeb\x17\xa6\x03" + bytes(60)
    web = _Web({BASE + "manifest.json": _manifest_for(image), BASE + GBL: image})
    releases = download.ReleaseDownloader("3.0.2", cache=tmp_path, get=web)

    assert await releases.fetch(GBL) == image
    again = download.ReleaseDownloader("3.0.2", cache=tmp_path, get=web)
    assert await again.fetch(GBL) == image

    assert web.asked.count(BASE + GBL) == 1  # the second came from the cache
    assert (tmp_path / "3.0.2" / GBL).read_bytes() == image


async def test_a_file_that_does_not_match_is_refused_and_not_cached(tmp_path: Path) -> None:
    web = _Web({BASE + "manifest.json": _manifest_for(b"the real one"), BASE + GBL: b"something else"})

    with pytest.raises(download.DownloadError, match="SHA-256"):
        await download.ReleaseDownloader("3.0.2", cache=tmp_path, get=web).fetch(GBL)
    assert not (tmp_path / "3.0.2" / GBL).exists()


async def test_a_file_the_release_does_not_have_is_refused(tmp_path: Path) -> None:
    web = _Web({BASE + "manifest.json": MANIFEST})

    with pytest.raises(download.DownloadError, match="no file"):
        await download.ReleaseDownloader("3.0.2", cache=tmp_path, get=web).fetch("nope.bin")


def test_the_cache_follows_the_platform_or_the_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENDISPLAY_CACHE", str(tmp_path))
    assert download.cache_dir() == tmp_path / "firmware"
    monkeypatch.delenv("OPENDISPLAY_CACHE")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(download.sys, "platform", "linux")
    assert download.cache_dir() == tmp_path / "xdg" / "opendisplay" / "firmware"
