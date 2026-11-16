# py-opendisplay for firmware 3.0: architecture and piece 1

Date: 2026-11-15. Status: draft for approval.

Lines marked **(call)** are choices made while writing this, not things agreed in conversation.

## Intent

Firmware 3.0 (`Firmware_Unified`, merged to its `main` as PR #95) replaces three firmware lines.
It changes how a device is secured (SEC2), how a panel is driven (drive data in the config
instead of a panel id), and how a device is updated. 2.x devices stay in the field and keep the
old behaviour, so the library has to speak both generations, and the 2.x to 3.0 upgrade is one
flow that crosses from one to the other.

py-opendisplay is the base for the Home Assistant integration and the CLI. The aim is that 3.0
support is built in properly rather than as version branches spread through `device.py`.

## Goals

- One device object that works against 2.x and 3.0 firmware, with the differences held in two
  places: the secure channel and the device profile.
- SEC2: sessions, claim, key rotation, unclaim.
- Config-driven panels: read and write drive packets, apply `code_map` on the host, select drive
  modes, migrate a 2.x config using the panel database.
- A setup and upgrade flow that lives in the library and is called by both HA and the CLI.
- The 2.x behaviour on the wire does not change.

## Non-goals

- A breaking 9.0 release now. New API is added beside the old; old names become thin delegates
  and are removed only after HA has moved. **(call)**
- Adopting the WIP branches (`origin/feat/od-epd-driver`, the `v3/py-v9` named in the status
  deck). They are a reference for intent and naming only.
- USB flashing in the first version of the upgrade flow (see piece 5).
- SEC2 on the nRF52811 port (the firmware has no security module there).

## Anti-goals

- A three-way `if` (none / v1 / SEC2) in every read and write path.
- Compatibility shims whose only purpose is to keep private test seams alive. Tests move with
  the code.
- An upgrade flow that can leave a device dark or unreachable without telling the caller what
  to do next.
- Raising pylint limits to make the new code fit.

## Constraints

- Sources of truth: `Firmware_Unified` `upstream/main` (`docs/PROTOCOL_CHANGES_3.0.md`,
  `SECURITY_V2.md`, `CONFIG_DRIVEN_DISPLAY.md`, `UPGRADING.md`, `shared/protocol/*.h`) and the
  website branch `origin/v3/website` (`od-v3.js`, `od-sec2*.js`, `od-keystore.js`,
  `od-wizard.js`, `tests/fixtures/sec2_vectors.json`,
  `httpdocs/firmware/toolbox/drive-presets.json`).
- Several protocol rows are still provisional and nothing is in `opendisplay-protocol` yet.
  Nothing 3.0-specific is released from the library before the values are final.
- Python 3.13+, `uv`, mypy strict, pylint limits as they are, release-please owns versions.
- Local `main` is 107 commits behind `origin/main` and has 3 unpushed commits. All work starts
  from `origin/main` once those 3 commits are rebased onto it.
- There are two HA consumers, read on 2026-11-15 under `~/Developer/Home_Assistant`:
  - **Core integration** (`core-opendisplay-8`, pinned `py-opendisplay==8.1.0`, about 1,170
    lines). Uses the constructor (`mac_address`, `ble_device`, `encryption_key`, `config`),
    `upload_image`, `read_firmware_version`, `config`, `is_flex`, and advertisement parsing.
  - **Custom integration** (`custom_integrations/opendisplay`, `py-opendisplay>=7.18.1`, about
    4,840 lines). Adds `upload_prepared_image`, `prepare_image`, `PartialState`, NFC, LED,
    buzzer, `landing_url`, `clear_gatt_cache`, `trigger_dfu_bootloader`, the TCP arguments
    (`host`, `port`, `tls`), config JSON import/export, and its own firmware update entity
    built on `opendisplay.ota.perform_silabs_ota` and `opendisplay.models.firmware`.
  - Neither touches a private attribute of the library.
  - Both import from submodules, not only from `opendisplay`: `opendisplay.device`,
    `opendisplay.exceptions`, `opendisplay.ota`, `opendisplay.models.advertisement`,
    `.config`, `.config_json`, `.enums`, `.firmware`. Those paths are public in practice.
  - The core integration pins an exact version and moves at core's review pace, so the old
    names have to outlive at least one core release after the new surface exists.

## Approaches considered

1. **Bolt on.** Add SEC2 and drive-data branches inside `device.py`. Fastest start. `device.py`
   is 3,021 lines with the v1 session written inline; every path would gain version branches.
2. **Seams first (chosen).** Behaviour-preserving refactor that pulls the secure channel and the
   upload paths out of `device.py`; then 3.0 in layers, pure logic first. The new public surface
   is designed now so the seams fit it.
3. **Rewrite with a new public API in one release.** Same end state as 2, but HA, the CLI and
   most tests change at once, against firmware that is still moving, and the upgrade flow (the
   code that can strand a device) would ship on untested foundations.

Chosen: 2, with the public surface of 3 designed up front and added additively.

## Target architecture

```
opendisplay/
  transport/     unchanged (BLE, TCP)
  protocol/      frames and parsers; gains READ_IDENT tail, new refusals, 0x80+ packets, SEC2 frames
  models/        dataclasses; gains panel drive models, DeviceProfile
  device/        facade.py   OpenDisplayDevice: connect, interrogate, commands, config
                 channel.py  SecureChannel: plain, legacy (v1), later SEC2
                 upload.py   Uploader: direct, partial, pipe, later 0x0085 drive write
                 imaging.py  prepare_image, preview_image, rotation
  sec2/          crypto, session, claim, rotation, key store interface      (piece 4)
  panels/        panel database (vendored drive-presets.json), migration    (piece 3)
  firmware/      today's ota.py, plus SMP updater and release lookup        (piece 5)
  setup/         planner (pure), runner, typed requests                     (piece 5)
```

The chain for traffic is `Uploader` / facade -> `SecureChannel` -> `Transport`. Nothing above the
channel knows which security scheme is active.

### Seam 1: the secure channel

One `SecureChannel` object per connection, holding an optional session. No session is the
plaintext case; the 2.x scheme and SEC2 are two session types that plug into the same slot.
(Refined while planning piece 1: a single channel with a pluggable session, rather than three
channel classes, because a session is established after the connection is already in use.)

| | Plain | Legacy (2.x) | SEC2 (3.0) |
|---|---|---|---|
| Handshake | none | `0x0050` challenge-response | `0x0056`, fresh ECDH, client proves first |
| Frames | as is | per-frame nonce counter; short replies arrive in plaintext | sealed both ways, a key and counter per direction, never split, 244 B limit |
| Lifetime | — | re-auth at 90 % of the config's timeout | one session per connection, 1 h |

The interface has to carry: write a command, write a stream frame (no re-auth, no draining of
queued ACKs), read and unwrap a reply, raise the typed refusals, report per-frame overhead so
chunk sizes are computed in one place, and reset on disconnect. The SEC2 column is listed so the
interface is shaped for it; only Plain and Legacy are built in piece 1.

### Seam 2: the device profile

`DeviceProfile` is an immutable description built once per connection: firmware target and
version, limits, panel geometry and colour scheme, drive modes, palette, what transfer paths are
allowed. For 2.x it is built from packet `0x20` and the built-in tables (today's
`_capabilities_from_config`). For 3.0 it is built from the READ_IDENT tail and the drive
packets. Upload code asks the profile, never the firmware version.

### Public surface (designed now, added from piece 2 on)

```python
async with opendisplay.connect(address, credential=DeviceKey(k)) as dev:
    dev.profile
    dev.security.state            # UNCLAIMED | CLAIM_MODE | CLAIMED | LOCKED | LEGACY
    await dev.display.show(image, mode="fast", partial=state)
    await dev.config.read(); await dev.config.write(cfg)
    await dev.security.rotate_key()
    await dev.firmware.enable_ota()

async with opendisplay.claim(address) as claim:
    key = await claim.confirm("493021")
```

- Credentials are typed: `LegacyKey` for 2.x, `DeviceKey` for 3.0. `encryption_key=` keeps
  working and maps to `LegacyKey`. **(call)**
- Each refusal has its own exception: needs migration, panel busy, config write busy, session
  required, refused in claim mode.
- `OpenDisplayDevice` and its current methods stay and delegate.

### Setup and upgrade flow (decided: in the library, HA and CLI both call it)

Mirrors the website's split in `od-wizard.js`: a pure planner plus a runner.

- **Planner.** Input: advertisement, READ_IDENT, SEC_STATUS, firmware version. Output: the
  device state and the ordered steps: back up, update, confirm image, configure display, verify
  panel, claim, secrets, done.
- **Runner.** Executes steps and pauses with a typed request when a person is needed
  (`NeedClaimCode`, `NeedPanelChoice`, `NeedConfirmation`, `NeedGesture`, `NeedWiredInstall`).
  HA turns a request into a config-flow form, the CLI into a prompt.
- **Resumable.** The plan serialises to JSON, config backup included, so it survives the device
  reboot and an HA restart. The backup holds the key; the caller chooses where it is stored.
- **Claiming is optional.** An unclaimed 3.0 device in normal mode is open and works in
  plaintext (SECURITY_V2 decision 2), so the plain channel is a first-class path on 3.0 and no
  caller may force a claim. The flow offers "use without a key", which on a fresh device means
  leaving claim mode (SEC_CLAIM EXIT), because claim mode refuses panel writes. The flow says
  what is given up: an unclaimed device refuses to store a WiFi password or FindMy keys.
- **Wired installs** (ESP32 over USB, nRF52840 serial DFU): the step can be completed two ways.
  First version: hand out the file and the website link, then resume when the device reappears.
  Later, for the CLI only: flash through an optional `esptool` extra. **(call)**

### Firmware updater (piece 5)

Two different jobs that the library keeps apart:

- **The first move to 3.0.** Once per device, different per target, sometimes wired, and it may
  erase the config. This is the setup and upgrade flow above.
- **Updates from 3.x to a later 3.x.** Over Bluetooth on every target. This is the updater.

The updater is one call, `dev.firmware.update(image)`, that picks the method from the profile's
firmware target:

| Target on 3.x | Method | In the library today |
|---|---|---|
| ESP32 | mcumgr SMP, `-app.bin` | no (2.x had no Bluetooth update for ESP32) |
| nRF52840, nRF54 | mcumgr SMP, `-ota.bin` | no (2.x nRF52840 used Nordic legacy DFU via `nrf-ota`) |
| EFR32BG22 | AppLoader, `.gbl` | yes, `silabs-ble-ota` |
| nRF52811 | Nordic secure DFU | to check: `nrf-ota` is the legacy DFU client |
| Telink | the firmware's own updater, inside a SEC2 session | no; after the other targets |

Every update runs the same steps, whatever the method:

1. Find the release asset for this target and board (3.0 uses new names in a new repository).
2. Enable the update, by security state (SECURITY_V2 § 6.1):
   - unclaimed, normal mode: every update path is open, nothing to unlock. BG22 still needs a
     plaintext ENTER_DFU, because that is what reboots it into the AppLoader;
   - claimed: a sealed ENTER_DFU first;
   - claim mode: updates are refused, so claim mode is left first.
3. Transfer the image.
4. Wait for the reboot and reconnect.
5. Send one command (READ_IDENT). On ESP32 and nRF this is what confirms the new image; without
   it the device goes back to the previous image at the next reset.
6. Read the version and report one of: updated, rolled back to the old version, or (BG22)
   waiting in the AppLoader for another `.gbl`.

What the new bootloaders give, per target (evidence as stated in `UPGRADING.md`):

| Target | A bad or interrupted update | Evidence |
|---|---|---|
| nRF52840, nRF54 | old image stays in the other slot; the new one runs on trial and is reverted if it cannot serve a connection | verified on nRF52840 (EN04); nRF54 untested |
| ESP32 | same idea, with the ESP32 rollback slot | rollback verified on one S3 board; the trust rule is host-tested only |
| EFR32BG22 | no second slot; a failing image is meant to fall back into the AppLoader | host-tested only; one unit was stranded and needed a debugger |
| nRF52811, Telink | no second slot | — |

So the updater must never assume a rollback exists: it reads `fw_target` and tells the caller
before the transfer whether this device can undo a bad image by itself. Images are unsigned on
every target; the protection is the gated ENTER_DFU, not a signature.

## Order of work

Each piece gets its own short design when it starts.

1. Seams in `device.py`, no behaviour change (detailed below).
2. 3.0 read side: READ_IDENT tail and `fw_target` values, advertisement bits 2/3 and the
   FF x 11 rule, new refusals, length-prefixed packets (draft PR #198, re-based on the
   `Firmware_Unified` structs), `DeviceProfile`.
3. Panel database, `backup-config`, `migrate`; tested on the E1003 after a manual USB flash.
4. SEC2: crypto against the website vectors, session channel, claim, rotation, unclaim, key
   store interface.
5. Setup and upgrade plan with runner; SMP updater; image confirmation after OTA.
6. Home Assistant, written against the new surface only. The custom integration goes first
   (claim flow, migration repair, the setup and upgrade plan replacing its own update logic);
   the core integration follows with a version bump once the library release is out.
7. Retire the old surface: deprecation warnings on the old names, then their removal in 9.0.

### When the new surface appears

The new surface is not a late migration step. Each piece adds the part it needs, on top of the
seams from piece 1, and the old method for the same job becomes a delegate to it in that same
piece:

| Piece | New surface added | Old names that become delegates |
|---|---|---|
| 1 | none (internal seams only) | — |
| 2 | `opendisplay.connect()`, `dev.profile`, `dev.config`, typed refusals | `interrogate`, `write_config`, `capabilities`, `width`, `height`, `color_scheme` |
| 3 | `dev.display.show()` with drive modes, `dev.config.plan_migration()` | `upload_image`, `upload_prepared_image` |
| 4 | credentials, `dev.security`, `opendisplay.claim()` | `encryption_key=`, `authenticate` |
| 5 | `dev.firmware`, `opendisplay.plan_setup()` | `trigger_dfu_bootloader`, the `ota` module functions |
| 5 | `dev.io` for LED, buzzer, NFC, sensors, power (no 3.0 change, moved for consistency) | `activate_led`, `activate_buzzer`, `write_nfc*`, `read_sensors`, `reboot`, `deep_sleep`, `power_off` |

So by the end of piece 5 both surfaces exist and the old one holds no logic. Piece 7 is then
small and mechanical:

1. Old names emit `DeprecationWarning` (a minor release), once HA and the CLI no longer call them.
2. A check that nothing inside the library or its CLI uses an old name.
3. Removal as the breaking 9.0, with a short migration table in the README.

A delegate must stay a one-line call into the new surface. If an old method needs logic of its
own to keep working, that is a sign the new surface is missing something, and it is fixed there.

## Piece 1: seams in `device.py`

**What changes.** `src/opendisplay/device.py` becomes the package `src/opendisplay/device/`
with `facade.py`, `channel.py`, `upload.py`, `imaging.py`. Code is moved, not rewritten.

- `channel.py`: the session state and logic now in `_clear_session`, `_encrypt_frame`, `_write`,
  `_write_pipe_frame`, `_reauthenticate_if_needed`, `_read`, `_read_auth_step2` and
  `authenticate`, as `SecureChannel` with a `LegacySession`.
- `upload.py`: everything from `_dispatch_upload` to `_await_pipe_end_ack`, including pipe
  negotiation state, as `Uploader`. It holds a channel and a capabilities object, not the device.
- `imaging.py`: `prepare_image`, `preview_image`, rotation helpers, the firmware limitation
  warnings.
- `facade.py`: `OpenDisplayDevice` with construction, connect, interrogate, the simple commands
  and config read/write. Its public methods keep their names and signatures and delegate.

**What must not change.**

- Everything importable from `opendisplay` and from `opendisplay.device` today, and the
  submodule import paths the HA integrations use (listed under Constraints).
- Public method names, signatures and exceptions of `OpenDisplayDevice`.
- Bytes on the wire and their order, for plain and encrypted sessions.

**How that is checked.**

- A new test that snapshots the public API (`opendisplay.__all__`, the public members and
  signatures of `OpenDisplayDevice`, and every submodule import the two HA integrations make),
  written before the move.
- The existing suite, with patch targets updated to the new module paths. No delegating private
  attributes are added to keep old patch strings working.
- `uv run prek run --all-files` clean.
- Hardware smoke test on the reTerminal E1003 (ESP32-S3, `OD405BD8`, firmware 2.27.1 as read on 2026-11-15) before and after:
  full upload, partial upload, pipe upload, config read, and one encrypted session if a key is
  set on it or on another 2.x device from the inventory.

**Shipping.** `refactor:` commits, one PR with a plain title, no release-note entry expected.

**Reference.** The unmerged `refactor/code-quality` branch (June, 174 commits behind) did this
split once: `facade.py` 1,101 lines, `upload.py` 423, `session.py` 183, `imaging.py` 196. Use it
to see where the cuts fell; do not rebase it.

## Left to the builder

- Exact method names on `SecureChannel` and `Uploader`.
- Whether pipe negotiation state lives on `Uploader` or in a small per-connection object.
- How the facade reaches into `Uploader` for the `@_serialized` lock.
- Splitting piece 1 into more than one PR if the diff is hard to review.

## Open items

- `opendisplay.connect()` must accept what HA passes today: a `ble_device` from HA's Bluetooth
  stack, and `host` / `port` / `tls` for LAN devices.
- When `ota.py` moves under `firmware/` in piece 5, `opendisplay.ota` and
  `opendisplay.models.firmware` stay importable as re-exports until piece 7.
- The status deck names a `v3/ha` branch; it is not among the local branches of the custom
  integration checkout.
- SMP client: an existing Python package or our own (decide in piece 5).
- Whether wire structs get generated from `opendisplay-protocol` once 3.0 is written into it.
- PR #198's open question is answered by the firmware docs: the host applies `code_map`, the
  firmware never remaps pixels. The built-in BWRY table and `code_map` must be reconciled in
  piece 2.

## Status and findings (updated 2026-11-16)

This section is the record a later session needs. It replaces nothing above; where it
disagrees with the text above, this section is newer.

### Where the work stands

| Piece | State |
|---|---|
| 1 Seams | Merged (#203). `device/` = `facade.py`, `channel.py`, `upload.py`, `imaging.py`, later also `nfc.py` |
| 2a Read side | Done: #208 (identity), #209 (advertisement state), #211 (typed refusals), #198 (panel drive packets) merged |
| 2b Profile and first new API | Task 1 merged (#212). Correction to the profile (`fix/profile-mode-records`) and Task 2 (`feat/device-entry-point`) done locally. Ledger: `.superpowers/sdd/2026-11-16-v3-2b-profile-and-device/progress.md` |
| 3 Panels, migration, drive modes | Planned: `../plans/2026-11-16-v3-3-panels-and-migration.md` (3a in detail, 3b as an outline that needs a design note first) |
| 4 SEC2 | Planned: `../plans/2026-11-16-v3-4-sec2.md` |
| 5 Updater and setup flow | Planned: `../plans/2026-11-16-v3-5-update-and-setup.md` |
| 6 Home Assistant, 7 retiring the old API | Outline: `../plans/2026-11-16-v3-6-7-home-assistant-and-retirement.md` |

Every plan lists "Decisions to confirm with the user" at its top. Those are choices the plan
author made alone; they are not approved until the user says so. Whoever executes a plan reads
`../plans/EXECUTOR_RULES.md` first.

Piece 2 was split: 2a is pure reading, 2b is `DeviceProfile` plus `opendisplay.connect()`,
`dev.profile`, `dev.config`. Plan and design for 2a: `2026-11-15-v3-read-side-design.md`,
`../plans/2026-11-15-v3-read-side.md`.

### Facts learned from hardware (each one changes a later piece)

- **Identity is not free on 2.x.** 2.x ESP32/nRF firmware is silent on READ_IDENT (a timeout);
  encrypted 2.2.1 EFR32BG22 answers the 3-byte session-required frame; only 3.0 and
  config-driven BG22 builds answer. So 2b must decide the generation from the firmware version
  first and ask for the identity only on major 3 and up. (The user ruled on 2026-11-16 that
  `connect()` has no BG22 exception, because an unencrypted 2.x BG22 is silent and would cost 2 s
  per connect; `opendisplay info` keeps its BG22 exception.) `IdentNotSupportedError.timed_out`
  tells silence from refusal; silence alone must not classify a device as 2.x.
- **A device with no config is its own state.** It answers READ_CONFIG with the no-config frame
  (`NoStoredConfigError`), reports zero displays in its identity, and refuses an upload START
  with generic NACKs (`ff70`, `ff760100`, `ff800300`), not with "needs migration" (0x08). 0x08
  is for a config that lacks drive data. The setup flow must treat "no config" and "needs
  migration" as two states.
- **No config means no display at all, including for the claim code.** A fresh device cannot
  draw its 6-digit code. Order for a fresh device: configure display, verify panel, then claim.
- **Advertisement bit 3 is unreliable on 2.x.** Encrypted 2.2.1 BG22 tags advertise it clear.
  Only on 3.0 does `session_required` mean something. Home Assistant must not use it to decide
  whether a 2.x device needs a key.
- **Fresh claim mode lasts 10 minutes**, then the device advertises unconfigured without it.
- **A 3.0.2 release image reports version 3.0.0 and an all-zero hash.** Do not rely on the
  patch byte on 3.0 yet.
- **ESP32 `max_config` is 4,000 bytes** (the deck said 4,096). Read limits from the identity.
- **Writing a backup cannot lock a 3.0 device; it is refused.** *(Corrected 2026-11-16: an
  earlier version of this bullet said 3.0 adopts a key from a config write. It does not.)*
  `SECURITY_V2.md` § 10.3–10.5 and `od_cfg_policy.c` (Firmware_Unified f6d3a83): an **unclaimed**
  device refuses a write carrying a 0x27 key or `encryption_enabled` with `[FF][41|42][10][00]`
  (`OD_ERR_CONFIG_SECRET_NEEDS_CLAIM`), and a write carrying a 0x26 WiFi password or 0x2D key
  material likewise. The one exception is a LEGACY_SECRETS device writing back WiFi/FindMy
  secrets byte-identical to what it stores; a 0x27 key is refused even there. A **claimed** device
  accepts a 0x27 key that is zero or equal to K_dev and refuses any other with `[FF][41|42][11][00]`
  (`OD_ERR_CONFIG_KEY_MISMATCH`); a claimed write needs a SEC2 session anyway. The key is set only
  by claim and rotation. A 2.x key is adopted in one place only: the first 3.0 boot of a device
  whose encrypted 2.x config survived the upgrade in place (§ 10.5 b; BG22 and nRF52811), which
  comes up claimed with K_dev = the old key and is usable only once SEC2 (piece 4) exists. To
  restore a backup onto another or re-claimed device, zero the 0x27 key and `encryption_enabled`
  first. The 0x10/0x11 values are in `shared/core/od_sec2_wire.h`, not yet in the protocol
  header's section 4g: provisional.
- **The Spectra boards redraw for about 30 seconds after a reset before they advertise.**
  Scripts that reset a board must wait or retry discovery.
- **PR #198's packet layouts are the same in `Firmware_Unified`.** Calibration L*, a*, b* are
  all signed. The host applies `code_map`.

- **Piece 3a migration proven on glass (2026-11-16).** `opendisplay migrate --from-backup
  scratch/backup-OD4064C9-fw2.26.7.json --strip-secrets` on the E1002 (Firmware_Unified 3.0.2,
  unclaimed, no config) stored a 1036-byte config byte-identical to the plan, and a 6-colour test
  image drew with correct colours. Panel 0x0023 (EP73_SPECTRA_800x480), "equivalent" in the
  database, is now seen drawing on glass with mode 0 (`full`); worth reporting upstream so the
  database can mark it. The device answers nothing for about 30 s after a config write while it
  redraws; `Device.config.write()` retries its read-back for about 90 s.

- **Piece 3b proven on glass (2026-11-16).** Named drive modes through `CMD_DRIVE_WRITE_START`
  (0x0085):
  - E1002 (ESP32, 3.0.2), mode `full`, compressed START: answered in 1.1 s (mode 0, 1 pass,
    25 °C), END acknowledged at once, panel refresh 32.9 s.
  - OD58D951 (BG22, 3.0.2), mode `colour16`, uncompressed START: answered in 0.9 s (mode 1,
    3 passes, 30 °C). The pass boundaries showed as data-chunk acknowledgements that waited
    20.3 s and 2.1 s; END at once, final refresh 2.0 s; 16 distinct swatches on glass.
  - A red/yellow image in the default mode was still correct after the host started applying
    `code_map`.
  - The measured waits sit well inside the website's timeout rule (data 90 s, START with
    inline data 10 s + max(90 s, (passes-1) x 45 s), END max(90 s, passes x 45 s)), which stays.
- **Open points from the 3b review (no code change yet):**
  - After a substituted mode or a pass-count mismatch the library sends nothing more, which leaves
    a started transfer on the device. A new START replaces it, and the firmware has a transfer
    timeout (`od_xfer.c` `od_xfer_report_timeout`); whether that timeout covers 0x0085 is a
    question for the firmware author.
  - Data chunks use the uploader's fixed 90 s acknowledgement timeout. With compression one chunk
    can cross several pass boundaries: fine for 3 passes (20.3 s + 2.1 s measured), not for a
    mode with many. Revisit when such a mode exists.
  - For piece 4: the START room is chosen on "session is not None" (`ENCRYPTED_CHUNK_SIZE` vs
    `MAX_START_PAYLOAD`), as in `upload.py`. A SEC2 session has another overhead, so all three
    places must ask the channel.
  - `mode_palette` takes the lightest 0xB0 entry as the paper white. That is an assumption about
    the calibration, true for all 7 shipped palettes.
- **The 0x0085 `@targets` line in `opendisplay_protocol.h` is stale.** It says "Silabs
  (Firmware/NRF54/NRF52811: pending)", but the ESP32 and nordic builds set
  `OD_CAP_DRIVE_WRITE=1` (`shared/core/od_cmd_drive.c`, also at release a519151), and the E1002
  accepted the command. Worth reporting upstream.

### Facts learned from the firmware sources in piece 2b

- **Two levels of colour scheme on 3.0** (`docs/CONFIG_DRIVEN_DISPLAY.md` "Mode conventions").
  The 0x20 `color_scheme` stays required: it is the pixel format of mode 0, the default full
  refresh every upload without a named mode goes through, and what READ_IDENT's display record
  reports. A 0xAE mode may declare its own scheme (0xFF = inherit); mode-level schemes such as
  BWRY16 (10) live only there and never in 0x20. `DeviceProfile.color_scheme` is the base
  scheme; `drive_modes` carries each mode's scheme and pass count.
- **Modes are identified by `mode_index`; the first 0xAE per (instance, mode_index) wins**;
  names match case-insensitively; `fast` and `partial` are reserved names.
- **On 3.0, partial support is not the 0x20 flag.** Firmware enables partial when a usable
  "partial" mode exists and no migration is pending, and the 0x20 scheme has partial geometry
  (`od_color_direct_geometry`: MONO only). The EFR32BG22 target never enables it; the ESP32
  FastEPD native driver (0xB3) always does. The 0x20 `partial_update_support` is read only by
  the boot screen. The profile models all of this except the mode's temperature window and the
  temperature window; the device's START NACK stays authoritative.
- **Native controllers (0xB3, ESP32 FastEPD builds) enable partial on their own.** The first 0xB3
  for the display's instance with a 12-byte body, version 0 and class 1 (IT8951) or 2 (parallel,
  panel 1..3) selects a built-in driver; partial is then on for any scheme but GRAY8 (9), whatever
  the modes say. C3/C6 builds have no FastEPD. The profile models this (Firmware_Unified f6d3a83).
- **`interrogate()` couples reading the config to deriving the legacy capabilities.** A 0x20
  scheme the legacy enum lacks makes it raise after the config is stored. `Device` tolerates
  exactly that (`ImageEncodingError`, re-raised when no config was stored); `Device.legacy` then
  has no capabilities. Piece 7 should separate config reading from capability derivation when
  `OpenDisplayDevice` becomes the delegate. `opendisplay info` fails on such a device today.
- **A stored config without a display packet cannot be parsed** (`ConfigParseError`), so
  `connect()` fails on it, like `OpenDisplayDevice`; 2b keeps that and a test pins it. It is a
  real 3.0 state: the firmware stores a config write as sent (PROTOCOL_CHANGES_3.0 row 10), and
  "no config, no 0x20, or no display with drive data" all mean "no drivable display".
  **Required for 3a:** the new entry point needs a lenient read that keeps the other packets, so a
  repair write does not silently drop WiFi, security or sensor packets. Do not treat it as "no
  config": a later write would then delete the device's other packets.
- **Required for piece 4: a 2.x key fails on 3.0 before a profile exists.**
  `connect(encryption_key=...)` runs the 2.x handshake inside `OpenDisplayDevice.__aenter__`,
  before the version read. 3.0 (`od_sec2.c`) answers `[00][50][03]` while unclaimed
  (`AuthenticationRequiredError`) and `[00][50][05]` = `AUTH_STATUS_V2_REQUIRED` once claimed
  (`AuthenticationFailedError`, "status 0x05"; `responses.py` does not know 0x05). So the caller
  cannot learn the device runs 3.0; this is what Home Assistant hits with a stored 2.x key after
  an upgrade. The needs-migration flag does not cover it: identity bit 1 and NACK 0x08 are about
  display drive data only, so such a device reports `needs_migration=False`. Status 0x05 is the
  security-side signal ("use SEC2"). The fix is ordering: the version, READ_IDENT and SEC_STATUS
  are plaintext, so piece 4 must read them first, then pick the scheme, and map 0x05 to a typed
  "needs SEC2" error. A device upgraded in place with an encrypted 2.x config comes up claimed
  with K_dev = its old key (§ 10.5 b), so a stored key should keep working over SEC2 (verify on
  hardware in piece 4). Two tests in `test_device_api.py` pin
  today's failure.
- **Future (no piece yet): a config missing system, manufacturer or power.** `GlobalConfig` types
  those as required and much of the library reads them unguarded, so making them optional is a
  typing change across the code base, not a parser flag. The status deck's "Core profile" for
  third-party firmware says a device must only provide 0x20: the opposite gap of the one 3a
  handles (0x20 present, the rest missing) will need handling one day. 3a makes only the display
  packet optional.
- **For piece 6:** `connect()` does not yet pass `max_attempts`, `use_services_cache`,
  `discovery_timeout`, `blocks_per_ack`, `max_queue_size` or `use_measured_palettes` through.
  Home Assistant passes none of them today.
- **For 3b: measured colours.** 0xB0 calibration packets are host-only, "the renderable palette
  of one multi-pass mode", flagged "measured on this unit". Mode 0 has no 0xB0 in the captured
  config, so the library's per-panel measured palettes (`display_palettes.py`, keyed by
  `panel_ic_type`, which stays in 0x20) remain the source for ordinary uploads. Calibration codes
  are panel codes already: never apply `code_map` to them.

### Constraints the code now has

- `device/facade.py` (about 1,490 lines) and `cli.py` are close to pylint's 1,500-line module
  limit. A piece that adds to either must first move something out, as its own refactor commit.
  2b's namespaced views are the natural place for the device methods to go.
- `tests/unit/test_public_api_snapshot.py` pins the public API. Regenerate it only for names a
  change adds (`UPDATE_API_SNAPSHOT=1`), and check the diff shows additions only.
- Tests must be able to fail. Every piece so far had tests that passed without the feature;
  a mutation run (switch the feature off, see which tests fail) found them.
- The typed refusals are not retried by the library. `PanelBusyError` on a partial upload now
  raises where 3.0 firmware used to get a silent fallback; Home Assistant needs a retry.

### Open questions for the firmware side

- Should an upload START on a device with no config answer with a code that says so?
- The version and hash reported by release images.
- `fw_target` 0x06 (Telink) and security flag bit 6 are still provisional; the bit names
  `CLAIM_MODE` / `SESSION_REQUIRED` are not in `opendisplay_structs.h` yet.

### What each remaining piece must still decide (not planned yet)

- **2b:** when the identity is read (never on 2.x non-BG22); what `DeviceProfile` holds for a
  no-config device; how `opendisplay.connect()` takes a `ble_device` and LAN arguments; which
  facade methods become delegates first.
- **3:** vendoring and updating `drive-presets.json`; the 0xB3 controller packet for IT8951
  boards (the E1003); the "untested panel" confirmation; stripping or keeping the key on restore.
- **4:** SEC_STATUS decoding (a 19-byte capture is in `scratch/captures-OD4064C9-fw3.0.2.md`),
  the key store interface, where `ENCRYPTED_CHUNK_SIZE` in `upload.py` must become the
  session's own value.
- **5:** an SMP client (existing package or own), whether macOS can do SMP updates, and the
  rule to connect once after an update to confirm the image.
