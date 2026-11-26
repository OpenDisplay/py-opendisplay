#!/usr/bin/env python3
"""Refresh the bundled panel drive database from a Firmware_Unified release.

Every release from 3.1.0 on lists its drive presets in ``manifest.json`` (``presets[]``, each with
its size and SHA-256). This script downloads them, checks them against the manifest, stores them
gzip-compressed in ``src/opendisplay/panels/`` and records where they came from in
``src/opendisplay/panels/source.json`` (which ``tests/unit/test_panel_database.py`` checks).

    uv run python tools/sync_drive_presets.py              # the latest release
    uv run python tools/sync_drive_presets.py --release 3.1.1
    uv run python tools/sync_drive_presets.py --check      # exit 1 when the bundled copy is behind
    uv run python tools/sync_drive_presets.py --summary changes.md   # also list what changed

Standard library only. ``GITHUB_TOKEN`` in the environment is sent to the GitHub API, when set,
to avoid its rate limit in CI.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO = "OpenDisplay/Firmware_Unified"
PANELS = Path(__file__).resolve().parents[1] / "src" / "opendisplay" / "panels"
RECORD = PANELS / "source.json"
# Release file -> bundled file. A release file not named here is a new database: stop and ask.
BUNDLED = {
    "drive_presets.json": "drive_presets.json.gz",
    "drive_presets_nrf52811.json": "drive_presets_nrf52811.json.gz",
}


def _get(url: str, *, api: bool = False) -> bytes:
    headers = {"User-Agent": "py-opendisplay-sync"}
    token = os.environ.get("GITHUB_TOKEN")
    if api and token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (https only)
        data: bytes = response.read()
        return data


def latest_tag() -> str:
    """The latest stable release (GitHub's "latest" skips drafts and pre-releases such as presets-v*)."""
    doc = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest", api=True))
    return str(doc["tag_name"])


def fetch_release(tag: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The release's manifest and its drive preset files, each checked against the manifest."""
    base = f"https://github.com/{REPO}/releases/download/{tag}/"
    manifest = json.loads(_get(base + "manifest.json"))
    presets = manifest.get("presets") or []
    if not presets:
        raise SystemExit(f"Release {tag} lists no drive presets (releases before 3.1.0 have none)")
    files: dict[str, bytes] = {}
    for entry in presets:
        name = entry["file"]
        if name not in BUNDLED:
            raise SystemExit(f"Release {tag} has a drive database py-opendisplay does not bundle yet: {name}")
        data = _get(base + name)
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise SystemExit(f"{name} from release {tag} does not match the manifest's size and SHA-256")
        files[name] = data
    missing = set(BUNDLED) - set(files)
    if missing:
        raise SystemExit(f"Release {tag} lacks {', '.join(sorted(missing))}")
    return manifest, files


def record_for(tag: str, manifest: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "repository": REPO,
        "release": tag,
        "commit": manifest.get("commit", ""),
        "files": {
            BUNDLED[name]: {"source": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        },
    }


def _panels(data: bytes) -> dict[str, dict[str, Any]]:
    return dict(json.loads(data).get("panels") or {})


def _glass(panel: dict[str, Any]) -> bool:
    return any((t or {}).get("verified") == "glass" for t in (panel.get("targets") or {}).values()) or (
        panel.get("verified") == "glass"
    )


def summarize(old: dict[str, bytes], new: dict[str, bytes]) -> str:
    """Markdown: panels added and removed, glass status gained and lost, drive data changed."""
    lines: list[str] = []
    for name in sorted(new):
        before = _panels(old[name]) if name in old else {}
        after = _panels(new[name])
        names = {key: panel.get("name", "") for key, panel in {**before, **after}.items()}
        sections = {
            "Added": sorted(set(after) - set(before)),
            "Removed": sorted(set(before) - set(after)),
            "Now verified on glass": sorted(
                k for k in after if k in before and _glass(after[k]) and not _glass(before[k])
            ),
            "No longer verified on glass": sorted(
                k for k in after if k in before and _glass(before[k]) and not _glass(after[k])
            ),
            "Drive data changed": sorted(
                k for k in after if k in before and after[k].get("packets") != before[k].get("packets")
            ),
        }
        body = [
            f"- {title}: " + ", ".join(f"`{k}` {names[k]}".rstrip() for k in keys)
            for title, keys in sections.items()
            if keys
        ]
        lines += [f"**{name}** ({len(before)} → {len(after)} panels)", *(body or ["- no panel changes"]), ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--release", metavar="TAG", help="The release to take (default: the latest)")
    parser.add_argument("--check", action="store_true", help="Write nothing; exit 1 when the bundled copy differs")
    parser.add_argument("--summary", metavar="FILE", help="Write what changed, as Markdown, to FILE")
    args = parser.parse_args()

    tag = args.release or latest_tag()
    manifest, files = fetch_release(tag)
    record = record_for(tag, manifest, files)
    bundled = {n: gzip.decompress((PANELS / g).read_bytes()) for n, g in BUNDLED.items() if (PANELS / g).exists()}
    current = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else None
    # Judged by the files themselves, so a stale copy behind a correct record is refreshed too.
    same_data = bundled == files and current == record
    if args.check:
        print(f"Bundled drive presets {'match' if same_data else 'differ from'} release {tag}")
        return 0 if same_data else 1
    if same_data:
        print(f"Bundled drive presets already match release {tag}; nothing written")
        return 0
    if args.summary:
        Path(args.summary).write_text(summarize(bundled, files), encoding="utf-8")
    for name, data in files.items():
        # mtime=0 and no file name in the header: the same input always gives the same bytes.
        (PANELS / BUNDLED[name]).write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Bundled drive presets updated to release {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
