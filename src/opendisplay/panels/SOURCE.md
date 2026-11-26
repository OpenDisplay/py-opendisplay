# Bundled panel drive database

Both files are the drive presets of a Firmware_Unified release, stored gzip-compressed. Every
release from 3.1.0 on carries them and lists them in its `manifest.json` (`presets[]`, with the
READ_IDENT targets each file serves, its size and its SHA-256).

`source.json` records the release, its commit and the SHA-256 of each uncompressed file.
`tests/unit/test_panel_database.py` checks the packaged files against it, so a copy that does not
match the record fails the suite.

| Package file | Release file | Serves |
|---|---|---|
| `drive_presets.json.gz` | `drive_presets.json` | ESP32, nRF52840, BG22, nRF54 (`fw_target` 1, 2, 3, 5) |
| `drive_presets_nrf52811.json.gz` | `drive_presets_nrf52811.json` | nRF52811 (`fw_target` 4) |

`setup`, `upgrade` and `migrate` read the drive data from a release when they can
(`PanelDatabase.for_release`). This copy is the fallback when the release cannot be read, and
what `PanelDatabase.load()` returns.

## Updating

```bash
uv run python tools/sync_drive_presets.py               # the latest release
uv run python tools/sync_drive_presets.py --release 3.1.1
uv run pytest tests/unit/test_panel_database.py
```

The script checks each file against the release manifest, writes the gzip files and
`source.json`, and writes nothing when the data already matches. The `Sync drive presets`
workflow runs it weekly and opens a pull request when the release has newer data.
