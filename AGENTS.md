# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`py-opendisplay` — a typed, async Python library (and `opendisplay` CLI) for OpenDisplay BLE e-paper displays: image upload with dithering, TLV config read/write, encryption, partial updates, and OTA firmware updates.

## Commands

Always use `uv`, never `pip`. Requires Python ≥ 3.11.

```bash
uv sync --all-extras                 # install deps (pytest lives in the `test` extra, not the dev group)
uv run prek install                  # install the pre-commit hook (once per clone, if not already installed)
uv run pytest                        # run all tests (asyncio_mode=auto, so no @pytest.mark.asyncio needed)
uv run pytest tests/unit/test_partial.py                 # one file
uv run pytest tests/unit/test_partial.py::test_name      # one test
uv run prek run --all-files          # full lint suite — exactly what CI runs
```

Check that the prek pre-commit hook is installed (`.git/hooks/pre-commit`); if it isn't, run `uv run prek install` so every commit is linted locally.

The lint suite (`prek`, configured in `.pre-commit-config.yaml`) runs ruff check `--fix`, ruff format, `mypy src/` (strict mode), and `pylint src/`. Individual tools can be run the same way, e.g. `uv run mypy src/`.

Gotchas:

- In a git worktree, `uv run pytest` can silently fall back to a system pytest with the *main checkout* installed editable — sync the worktree's venv first and verify you're testing the right code.
- Don't `git add -A` or `git add .` — the repo root has many loose untracked device-config JSON files. Stage explicit file names.
- Version and CHANGELOG are managed by release-please; never bump `version` in `pyproject.toml` by hand.
- Pylint limits in `pyproject.toml` are deliberately tuned to this codebase (large config dataclasses, TLV dispatch functions) — fix code to fit them rather than raising them further.

## Architecture

`src/` layout, package `opendisplay`, fully typed (`py.typed`, mypy strict). Layered roughly bottom-up:

- **`transport/connection.py`** — `BLEConnection`: bleak + bleak-retry-connector, notification queue, GATT service-cache handling with bounded stale-cache retries.
- **`protocol/`** — pure byte-level layer, no I/O. `commands.py` builds command frames (`CommandCode` enum: 0x0040 config read, 0x0070 direct write, 0x0076 partial, 0x0050 auth, …), `responses.py` parses ACK/NACK/response frames, `config_parser.py`/`config_serializer.py` round-trip the TLV device config.
- **`models/`** — dataclasses only: `GlobalConfig` and its per-TLV-packet sections (`config.py`), enums, `config_json.py` (JSON import/export compatible with the Config Builder web tool), `advertisement.py` (parses both legacy 11-byte and v1 14-byte BLE advertisement formats; `AdvertisementTracker` derives button events from durable press counts, not the transient pressed bit).
- **`encoding/`** — image → bitplane bytes (`bitplanes.py`, numpy-vectorized) and compression (`compression.py`). Actual dithering/palette mapping is done by the external `epaper-dithering` package; `display_palettes.py` holds measured per-panel palettes used instead of theoretical ColorScheme colors when available.
- **`device.py`** — `OpenDisplayDevice`, the facade that orchestrates everything: connect → (authenticate if encrypted) → interrogate config → upload. This is the file to read to understand any end-to-end flow.
- **`crypto.py`** — AES-128 challenge-response authentication; after auth, all command traffic is transparently encrypted in `device.py`'s read/write path.
- **`partial.py`** — `PartialState` for differential (flicker-free) 0x76 updates; caller-owned, persistable via `to_bytes()`.
- **`ota.py`** — dispatch to optional `nrf-ota` (Nordic Legacy DFU, DFU device appears at MAC+1) or `silabs-ble-ota` (AppLoader, same MAC) extras. No BLE OTA for ESP32. macOS cannot do OTA (CoreBluetooth GATT cache).
- **`cli.py`** — argparse CLI (`opendisplay` entry point; scan/info/upload/reboot/export-config/write-config), `rich` optional.

### Upload flow (the core path)

`upload_image()` → `prepare_image()` (rotate → fit → dither) → encode to bitplanes → `_dispatch_upload()` picks one of three wire paths:

1. **Partial** (0x76) — only when the caller passes a `PartialState`, the panel supports it, and a previous frame exists; falls back to full automatically. `PartialUpdateSupport.FULL_FRAME` (=2) panels need the stream expanded to the whole screen.
2. **Pipe/streaming** — negotiated chunked transfer with retransmission budget.
3. **Direct write** (0x70/0x71) — the legacy path.

Compression capability is firmware-dependent: the `transmission_modes` bit 0x01 means ZIP on pre-2.0 firmware but streaming decompression on 2.0+; firmware ≤ 1.81 NACKs a compressed START without the ZIP bit.

### Protocol ground truth

When protocol constants or firmware behavior are in question, verify against the firmware sources at `/Users/gabriel/Developer/OpenDisplay/Firmwares` (fetch and read `upstream/main` — local checkouts go stale) rather than trusting comments here.

## Tests

All real tests are in `tests/unit/` (`tests/integration/` is empty); they mock the BLE layer, so no hardware is needed. Hypothesis is available for property-based tests. `scripts/` holds ad-hoc hardware/debug scripts run against real devices — they are not part of the test suite or the package.
