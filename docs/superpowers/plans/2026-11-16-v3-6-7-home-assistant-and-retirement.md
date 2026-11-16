# Pieces 6 and 7: Home Assistant, then Retiring the Old API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `EXECUTOR_RULES.md` in this folder first.

**Goal:** The Home Assistant integrations work with firmware 3.0 devices through the library's new surface, and afterwards the old method names on `OpenDisplayDevice` are deprecated and removed.

**Architecture:** Home Assistant keeps doing only Home Assistant things: config flow forms, entities, storage. Every protocol decision stays in the library. Each library request type from the setup flow maps to one config-flow step.

**Spec:** `../specs/2026-11-15-v3-architecture-design.md` (sections "Order of work", "When the new surface appears", "Status and findings").

**Depends on:** pieces 2b to 5 released in a `py-opendisplay` version.

**This plan is an outline, on purpose.** It was written before pieces 2b to 5 exist, and against a reading of the integrations made on 2026-11-15. Before executing it, re-read both integrations and turn each task into steps with exact files and tests; the first task below is that work.

## Where the integrations live

- **Custom integration:** `~/Developer/Home_Assistant/custom_integrations/opendisplay/custom_components/opendisplay/` (about 4,840 lines: `config_flow.py`, `coordinator.py`, `services.py`, `update.py`, `delivery.py`, `sleep.py`, `transport.py`, `ble_lock.py`, entity platforms). Requires `py-opendisplay>=7.18.1`.
- **Core integration:** `~/Developer/Home_Assistant/core-opendisplay-8/homeassistant/components/opendisplay/` (about 1,170 lines), pinned to an exact library version, moves at Home Assistant core's review pace.
- The status deck names a `v3/ha` branch by another author; it was not present in the local checkout. Ask the user whether to read it before starting.

## Standing rules for this work

- From the user's global instructions: **never `pip install` or `uv pip install` a local library into a Home Assistant environment.** To run an integration against an unreleased library, point the requirement in the integration's `manifest.json` at the local checkout (`"py-opendisplay @ file:///absolute/path/to/py-opendisplay"`), let Home Assistant install it, and restore the pinned requirement before committing.
- Pull request titles carry no Conventional Commits prefix.
- A 2.x device must keep working exactly as before in both integrations.

## Decisions to confirm with the user

1. **Custom integration first, core second.** Core gets the smallest possible change (a version bump and whatever a 3.0 device needs to not misbehave) once the library release is out.
2. **Where device keys are stored:** the config entry's data, as the 2.x key is today, through the library's `KeyStore` protocol.
3. **What Home Assistant does with a 3.0 device it cannot use yet** (unconfigured, needs migration, awaiting claim): a repair issue that starts the setup flow, rather than a failed setup.
4. **Whether Home Assistant offers the 2.x to 3.0 upgrade at all**, given that several first installs are wired and that BG22 has no rollback. A conservative first version only links to the website for the install and handles everything after it.

## Piece 6 tasks (custom integration)

### Task 1: Re-read and write the detailed plan

List every library call the integration makes (the piece 1 review used: constructor arguments, `upload_image`, `upload_prepared_image`, `prepare_image`, `PartialState`, NFC, LED, buzzer, `landing_url`, `clear_gatt_cache`, `trigger_dfu_bootloader`, config JSON, `perform_silabs_ota`, `firmware_ota_asset`, advertisement parsing and the two trackers). For each, name its replacement on the new surface. Output: this file's tasks rewritten with files, signatures and tests.

### Task 2: Do no harm with 3.0 devices (can ship before anything else)

Three facts from hardware that change Home Assistant today:
- An unconfigured device advertises all-`0xFF` dynamic bytes. With the library from piece 2a the trackers ignore them; verify no entity reports a phantom press or a sensor value.
- The advertisement's session-required bit is unreliable on 2.x. Do not use it to decide whether a 2.x device needs a key.
- A 3.0 device without config raises `NoStoredConfigError` on connect; with a 2.x-style config it raises `NeedsMigrationError` on upload. Both must become a clear message or repair issue, not a generic failure. `PanelBusyError` and `ConfigWriteBusyError` are transient: retry with backoff.

### Task 3: Discovery and setup

Route `AdvertisementData.awaiting_claim` to a claim step. Map the library's setup requests to forms: `NeedClaimCode` → a 6-digit field; `NeedPanelChoice` → a selector; `NeedConfirmation` → a confirm step with the reason; `NeedGesture` → instructions (two or five power cycles); `NeedWiredInstall` → a link and a "continue when done" step. Persist `SetupRun.to_json()` in the flow so a restart resumes.

### Task 4: Sessions and keys

Implement the `KeyStore` protocol on the config entry. Replace the direct `encryption_key=` use with `opendisplay.connect(..., key_store=...)`. Rotation of a migrated 2.x key happens by default; offer the opt-out as an option.

### Task 5: Uploads, drive modes, updates

Move uploads to `dev.display.show()`. Offer a drive-mode option only when the profile lists more than one mode. Replace the integration's own update logic in `update.py` with `dev.firmware.update()` and show, on the update entity, whether the device can undo a bad image by itself.

### Task 6: Core integration

The smallest change that makes Task 2 true there, plus the version bump.

## Piece 7: retire the old surface (library)

Only after both integrations and the CLI call nothing old.

1. **Check that nothing inside the library uses an old name.** A test that greps `src/` for calls to the deprecated methods, excluding their own definitions.
2. **Deprecation warnings.** Each old public method on `OpenDisplayDevice` becomes a one-line call into the new surface and emits `DeprecationWarning` naming its replacement. If an old method needs logic of its own to keep working, the new surface is missing something: fix it there. A minor release.
3. **Migration table** in the README: old name, new name, one line each. The table in the architecture spec ("When the new surface appears") is the starting point.
4. **Removal** in a breaking release (9.0), after at least one Home Assistant core release has shipped on the new surface. Regenerate the public API snapshot; this is the one time its diff shows removals, and the pull request lists them.
5. Remove `opendisplay.ota` and `opendisplay.models.firmware` re-exports in the same release.
