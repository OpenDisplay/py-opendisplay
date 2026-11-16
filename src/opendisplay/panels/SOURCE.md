# Vendored panel drive database

Both files are copied from the OpenDisplay `Web` repository, branch `v3/website`, commit
`b7df514`, and stored gzip-compressed (`gzip -9 -n`). They are the website's copies of
Firmware_Unified's generated drive presets (`panels/generated/`).

| Package file | Source path | sha256 of the uncompressed file |
|---|---|---|
| `drive_presets.json.gz` | `httpdocs/firmware/toolbox/drive-presets.json` | `c8b15046be60ee011954269d7933b80e7daeb9add0b6288b22fb47b74fcff08c` |
| `drive_presets_nrf52811.json.gz` | `httpdocs/firmware/toolbox/drive-presets-nrf52811.json` | `9fe6a57349379c7f9ff4814d106446cb9dec8cc277b03e49d5e0a7c297b7d376` |

`drive-presets.json` `sources` block: `{"bb_epaper": "4edfa7f", "panels_yaml_sha256":
"2e53cea99e2edbbfb6a6d731fa6cae62d6826fd2ade1fe472d0ad623818c3b61"}`. The nRF52811 file has an
empty `sources` block.

## Updating

```bash
cd ~/Developer/OpenDisplay/Web && git fetch origin
git show origin/v3/website:httpdocs/firmware/toolbox/drive-presets.json > /tmp/dp.json
git show origin/v3/website:httpdocs/firmware/toolbox/drive-presets-nrf52811.json > /tmp/dp811.json
shasum -a 256 /tmp/dp.json /tmp/dp811.json      # record both here with the new commit
gzip -9 -n -c /tmp/dp.json > src/opendisplay/panels/drive_presets.json.gz
gzip -9 -n -c /tmp/dp811.json > src/opendisplay/panels/drive_presets_nrf52811.json.gz
uv run pytest tests/unit/test_panel_database.py
```

`tests/unit/test_panel_database.py` checks the sha256 values above against the packaged files,
so a copy that does not match this record fails the suite.
