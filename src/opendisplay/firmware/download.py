"""Release files from GitHub (``OpenDisplay/Firmware_Unified`` releases), checked and cached.

The release's ``manifest.json`` names every file with its SHA-256; a file that does not match is
refused and not cached. Files are kept under the user's cache directory, per release tag, so a
second run sends nothing. Standard library only; Home Assistant passes its own fetcher instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Final

from ..exceptions import OpenDisplayError
from .releases import ReleaseManifest

REPO: Final = "OpenDisplay/Firmware_Unified"
LATEST_URL: Final = f"https://api.github.com/repos/{REPO}/releases/latest"
_DOWNLOAD_URL: Final = f"https://github.com/{REPO}/releases/download/"
_USER_AGENT: Final = "py-opendisplay"
_TIMEOUT_S: Final = 60.0

Get = Callable[[str], bytes]


class DownloadError(OpenDisplayError):
    """A release file could not be fetched, or did not match the manifest."""


def cache_dir() -> Path:
    """``$OPENDISPLAY_CACHE/firmware``, else the platform's user cache directory."""
    override = os.environ.get("OPENDISPLAY_CACHE")
    if override:
        return Path(override) / "firmware"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "opendisplay" / "firmware"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310 (https only)
            data: bytes = response.read()
            return data
    except OSError as err:
        raise DownloadError(f"Could not fetch {url}: {err}") from err


class ReleaseDownloader:
    """One release: its manifest and its files.

    Args:
        tag: The release tag (``"3.0.2"``); ``None`` takes the latest release.
        cache: Where files are kept; by default :func:`cache_dir`.
        get: Fetches a URL's bytes (blocking; run in a thread); for tests.
    """

    def __init__(self, tag: str | None = None, *, cache: Path | None = None, get: Get | None = None) -> None:
        self.tag = tag
        self._cache = cache if cache is not None else cache_dir()
        self._get = get or _get
        self._manifest: ReleaseManifest | None = None

    async def _fetch(self, url: str) -> bytes:
        return await asyncio.to_thread(self._get, url)

    async def manifest(self) -> ReleaseManifest:
        """The release's manifest (and, for the latest release, its tag)."""
        if self._manifest is None:
            if self.tag is None:
                self.tag = str(json.loads(await self._fetch(LATEST_URL))["tag_name"])
            self._manifest = ReleaseManifest.parse(await self._fetch(f"{_DOWNLOAD_URL}{self.tag}/manifest.json"))
        return self._manifest

    async def fetch(self, name: str) -> bytes:
        """The release file ``name``, from the cache when it is there.

        Raises:
            DownloadError: The release has no such file, it could not be fetched, or its SHA-256
                differs from the manifest's.
        """
        manifest = await self.manifest()
        artefact = manifest.artefact(name)
        if artefact is None:
            raise DownloadError(f"Release {self.tag} has no file {name!r}")
        path = self._cache / str(self.tag) / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == artefact.sha256:
            return path.read_bytes()
        data = await self._fetch(f"{_DOWNLOAD_URL}{self.tag}/{name}")
        if hashlib.sha256(data).hexdigest() != artefact.sha256:
            raise DownloadError(f"{name} from release {self.tag} does not match the manifest's SHA-256")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data
