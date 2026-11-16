# Piece 3: Panel Database, Migration and Drive Modes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `EXECUTOR_RULES.md` in this folder first.

**Goal:** Give a firmware 3.0 device a config it can draw with, from a 2.x config or from nothing, and then use what that config declares: the panel's colour code map and its named drive modes.

**Architecture:** A vendored copy of the firmware's generated panel database (`drive-presets.json`) is loaded by a small pure module. A pure planner decides which packets to add and remove (a port of `planMigration` in the website's `od-v3.js`), and a pure function applies the plan to a `GlobalConfig`. Device access goes through `Device.config` from piece 2b. Drive modes are a second, separate part (3b).

**Tech Stack:** Python 3.13, `uv`, pytest, ruff, mypy strict, pylint, prek.

**Spec:** `../specs/2026-11-15-v3-architecture-design.md`. Reference code: `Web` repo `origin/v3/website`, `httpdocs/js/od-v3.js` (functions named below) and `httpdocs/firmware/toolbox/drive-presets.json`. Firmware: `docs/CONFIG_DRIVEN_DISPLAY.md`, `panels/README.md`.

**Depends on:** pieces 2a and 2b merged.

## Decisions to confirm with the user

**Status 2026-11-16:** 1–3 settled (the user decided 3; the user delegated 1 and 2 to the plan author, whose answers are below). 4 is open until 3b starts.

1. **Restoring a 2.x backup that carries secrets.** *(Corrected: an earlier text said 3.0 adopts a key from a config write and locks the device. It does not; the write is refused. See the spec's "Status and findings".)* An unclaimed 3.0 device refuses to store any secret (`[FF][41|42][10][00]`). **Decided:** `migrate --from-backup` refuses a backup that carries any secret unless `--strip-secrets` is given. Before connecting to write, it lists which secrets it found, never their values, and says why: "an unclaimed firmware 3.0 device does not store secrets; claim it first, then enter WiFi / FindMy settings again." The secrets are `SECURITY_V2.md` § 10.4's closed list: 0x27 key or `encryption_enabled`, 0x26 password, 0x2D key material. `--strip-secrets` zeroes the 0x27 key and `encryption_enabled`, zeroes every 0x26 password, and drops the 0x2D packet (the library keeps it raw), and says so. A migrate that reads the device's own config strips nothing, because stripping would delete the user's WiFi; the firmware accepts LEGACY_SECRETS WiFi/FindMy secrets back byte-identical. A refusal then arrives as a typed error (Task 0b). TODO for piece 4: claim first, then write the secrets in the session.
2. **Panels marked `equivalent` (70 of 77) get one extra confirmation; `glass` (7) none.** **Decided as planned.** The prompt names the panel and its id and says in plain words what "equivalent" means: the drive data replays byte-identically to what 2.x sent, but nobody has watched this panel draw with it. `--yes-untested` skips the prompt; `--dry-run` always prints the verification level.
3. **The database ships gzip-compressed** (`drive_presets.json.gz`, 32 KB instead of 809 KB; decided by the user). Load it with `gzip` from package data. `panels/SOURCE.md` records the `Web` commit and the sha256 of the uncompressed file, so an update can be verified.
4. **Part 3b sends multi-pass frames only on targets where the firmware documents `0x0085`.** The header currently lists Silabs only ("Firmware/NRF54/NRF52811: pending"), although the status deck shows hardware runs on BG22 and nRF52840. Re-read the header's `@targets` line when 3b starts.

## Global Constraints

- Everything in `EXECUTOR_RULES.md`.
- **Never write to a device in an automated test run.** Every hardware write in this plan is its own step and needs the user's go-ahead.
- Raw bytes stay the source: migration adds and removes whole packets; it never edits inside a drive packet.
- Legacy packets (`0x00`–`0x7F`) of a migrated config are written back byte for byte, except the one field `panel_ic_type` in `0x20` when the planner remaps it (nRF52811 only).
- Length-prefixed packets go after every legacy packet (the serializer already does this).

## Review Focus

1. **A panel id with no database entry.** The plan must come back `blocked` with a reason; nothing is written.
2. **A database entry that does not fit the device's limits** (`max_config`, `max_seq_steps`, a required escape the device lacks). Blocked, with the limit named. Use the identity's limits, not the per-class table, when an identity exists.
3. **A config that already has drive data.** No migration, no write, and the reason says so; running `migrate` twice is harmless.
4. **A board with a built-in controller (IT8951, parallel).** It needs one `0xB3` packet and no `0xAE`–`0xB1`. The E1003 is such a board. ESP32 only: on any other target the plan is blocked.
5. **Interrupted write.** `migrate` reads the config back after writing and compares; a mismatch is reported with both sizes and the command exits non-zero. The backup file is never deleted.

---

## Part 3a: database, planner, `backup-config` and `migrate`

### Task 0a: Lenient read of a config without a display packet

**Branch:** `feat/lenient-config-read`. Required by the 2b review: on 3.0 a stored config without 0x20 is a real state (the firmware stores a write as sent), and a repair must keep the other packets.

- `parse_config_response(raw, *, require_display=True)`: one keyword on the existing function, not a second parser, so the two cannot drift. With `False`, a config without 0x20 parses to `displays=[]` and keeps every other packet. Only the display packet becomes optional. The strict error message stays byte-identical, because a 2b test pins it.
- The facade gets one small internal reader that returns the assembled config bytes. `interrogate()` and `ConfigView` both use it. Do this as its own refactor commit first, and move something out of `facade.py` if the line limit needs it. `OpenDisplayDevice` stays strict.
- `Device` and `ConfigView` use the lenient mode: `connect()` succeeds with `configured=False`, and `config.cached` holds the other packets. Update the 2b test `test_stored_config_without_displays_fails_the_connect_and_closes_the_link` accordingly, and assert the other packets are there.
- `legacy.write_config()` keeps refusing a config without a display: a repair must add one.

### Task 0b: Typed errors for the 3.0 config-write refusals

Next to `ConfigWriteBusyError`, raised by `raise_for_config_write_refusal`: `ConfigSecretNeedsClaimError` (`[FF][41|42][10][00]`) and `ConfigKeyMismatchError` (`[FF][41|42][11][00]`). The values live in `shared/core/od_sec2_wire.h` (`OD_ERR_CONFIG_SECRET_NEEDS_CLAIM`, `OD_ERR_CONFIG_KEY_MISMATCH`), not yet in the protocol header's section 4g: mark them provisional in a comment. Tests from those frames, on the first chunk and on a later chunk.

### File Structure

| File | Responsibility |
|---|---|
| `src/opendisplay/panels/__init__.py` | exports |
| `src/opendisplay/panels/drive_presets.json.gz` | vendored, gzip-compressed, from `Web` `origin/v3/website` `httpdocs/firmware/toolbox/drive-presets.json`; record its source commit and the uncompressed sha256 in `panels/SOURCE.md` |
| `src/opendisplay/panels/database.py` | load and query the database |
| `src/opendisplay/panels/migration.py` | `plan_migration`, `apply_migration`, `MigrationPlan` |
| `src/opendisplay/cli_migrate.py` | the `backup-config` and `migrate` commands (new module: `cli.py` is near its line limit) |
| `tests/unit/test_panel_database.py`, `test_migration.py`, `test_cli_migrate.py` | tests |

### Task 1: The panel database

**Branch:** `feat/panel-database`

**Interfaces:**
- Produces:

```python
class TargetClass(Enum):            # which limits apply; mirrors od-v3.js targetClassFromIdent / normalizeClass
    DEFAULT = "default"             # ESP32, nRF52840, nRF54
    BG22 = "bg22"
    NRF52811 = "nrf52811"

@dataclass(frozen=True)
class PanelEntry:
    panel_ic_type: int
    name: str
    width: int
    height: int
    color_scheme: int
    verified: str                               # "glass" or "equivalent"
    packets: tuple[LengthPrefixedPacket, ...]   # the drive packets for this target class
    drive_bytes: int
    requires_escapes: tuple[str, ...]
    blocked_reason: str | None                  # not None when the entry cannot be used on this class
    @property
    def usable(self) -> bool

@dataclass(frozen=True)
class NativeController:                          # boards with a built-in controller driver (0xB3)
    panel_ic_type: int
    name: str
    board: str
    controller_class: str
    packets: tuple[LengthPrefixedPacket, ...]

class PanelDatabase:
    schema_minor: int
    caps: Mapping[str, int]                      # the file's "caps"
    @classmethod
    def load(cls, path: Path | None = None) -> PanelDatabase      # None: the vendored file
    def entry_for(self, panel_ic_type: int, target_class: TargetClass) -> PanelEntry | None
    def native_controller_for(self, panel_ic_type: int) -> NativeController | None
    def needs_drive_data(self, panel_ic_type: int) -> bool         # port of panelNeedsDriveData

def target_class_for(target: FirmwareTarget | int | None) -> TargetClass
```

Port these `od-v3.js` functions and keep their names in the docstrings so the two stay comparable: `targetClassFromIdent`, `normalizeClass`, `driveView`, `entryFromGenerated`, `driveEntryFor`, `nativeControllerFor`, `panelNeedsDriveData`. Where the JSON gives per-class data (`targets`, `fits_target`), `entry_for` returns the view for the requested class, as `driveView` does.

- [ ] **Step 1:** Branch. Copy the JSON file in gzip-compressed (`drive_presets.json.gz`, decision 3); write `panels/SOURCE.md` with the `Web` commit, the sha256 of the uncompressed file and the file's `sources` block; add the file to the package data in `pyproject.toml` and confirm with `uv build` that the wheel contains it.

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_panel_database.py`):

```python
def test_database_loads_and_declares_its_schema():
    db = PanelDatabase.load()
    assert db.schema_minor >= 3 and db.caps["max_steps"] == 128

def test_known_panel_has_drive_packets_that_start_with_the_panel_packet():
    entry = PanelDatabase.load().entry_for(0x0001, TargetClass.DEFAULT)
    assert entry.name == "EP42_400x300" and entry.usable
    assert entry.packets[0].packet_type == 0xB1
    assert {p.packet_type for p in entry.packets} <= {0xAE, 0xAF, 0xB0, 0xB1}

def test_every_usable_entry_decodes_with_the_library_models():
    # for each panel and each TargetClass: build a GlobalConfig carrying entry.packets, and assert
    # display_panels and display_modes decode, mode 0 exists, and every LUT has crc_valid
def test_unknown_panel_has_no_entry():                                   # Review Focus 1
    assert PanelDatabase.load().entry_for(0x7FFF, TargetClass.DEFAULT) is None

def test_native_controller_board():                                      # Review Focus 4
    native = PanelDatabase.load().native_controller_for(3000)
    assert native.controller_class == "IT8951" and [p.packet_type for p in native.packets] == [0xB3]

def test_entry_that_does_not_fit_a_class_says_why():                     # Review Focus 2
    # pick, by scanning the file in the test, a panel whose fits_target["bg22"] is False:
    # entry_for(id, BG22).usable is False and blocked_reason is a non-empty sentence

def test_glass_verified_panels_are_marked():
    # count entries with verified == "glass" for DEFAULT: equals the count in the JSON file
```

- [ ] **Step 3:** Run to see them fail. **Step 4:** Implement. **Step 5:** `uv run pytest -q && uv run prek run --all-files`, commit `feat: vendor the panel drive database and query it`.

### Task 2: The migration planner

**Branch:** `feat/migration-planner`

**Interfaces:**
- Consumes: `PanelDatabase`, `DeviceProfile` (piece 2b), `GlobalConfig`.
- Produces:

```python
@dataclass(frozen=True)
class MigrationPlan:
    needed: bool
    blocked: bool
    reason: str                                   # one sentence, always set
    panel_ic_type: int | None
    target_class: TargetClass
    entry: PanelEntry | NativeController | None
    add: tuple[LengthPrefixedPacket, ...]
    remove: tuple[LengthPrefixedPacket, ...]      # existing drive packets to drop
    needs_confirmation: bool                      # entry is "equivalent", not seen on glass
    remap_panel_ic_type: int | None               # nRF52811 only: the 3.0 id that replaces the 2.x id in 0x20

def plan_migration(config: GlobalConfig | None, profile: DeviceProfile, db: PanelDatabase, *,
                   panel_ic_type: int | None = None) -> MigrationPlan
def apply_migration(config: GlobalConfig, plan: MigrationPlan) -> GlobalConfig
def check_fits(config: GlobalConfig, profile: DeviceProfile) -> list[str]      # problems; empty when fine
```

`plan_migration` is a port of `planMigration` and `planNativeController` in `od-v3.js`; follow their branches in order and keep their reason strings. Differences from the JS: the "burned lab packet" branches (`isBurnedPid`, ids 0x2E–0x31) are not ported, because the library has no such packets outside `unparsed_tail`; if `config.unparsed_tail` is non-empty, the plan is `blocked` with the reason "the config has packets this library cannot read; migrate it with the website toolbox". `check_fits` ports `checkDrivePacketsFit` and uses `profile.max_config` and the identity's `max_seq_steps` and escapes when present, else the database's `caps` for the class.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_migration.py`). Build profiles with `build_profile` and real inputs:

```python
def test_2x_config_on_3_0_gets_the_panels_drive_packets():
    # config: one display, panel_ic_type 0x0001, no length-prefixed packets; profile V3 ESP32 needs_migration
    plan = plan_migration(config, profile, db)
    assert (plan.needed, plan.blocked) == (True, False) and plan.add == db.entry_for(1, DEFAULT).packets
    migrated = apply_migration(config, plan)
    assert migrated.display_modes[0].mode_index == 0
    assert serialize_config(migrated)[: <length of the legacy part>] == serialize_config(config)[: <same>]   # legacy bytes untouched

def test_not_a_3_0_device_needs_nothing():                 # profile V2 -> needed False, reason "not a 3.x device"
def test_already_migrated_config_needs_nothing():          # Review Focus 3; use tests/fixtures/configs/bg22_alpha_drive_packets.bin
def test_unknown_panel_blocks():                           # Review Focus 1; reason names the panel id
def test_entry_too_large_for_the_device_blocks():          # Review Focus 2; profile with max_config 600
def test_equivalent_panel_asks_for_confirmation():         # needs_confirmation True; a "glass" panel False
def test_it8951_board_gets_one_controller_packet():        # Review Focus 4; panel 3000 on ESP32 -> add == the 0xB3 packet, no 0xAE-0xB1
def test_it8951_board_on_another_target_blocks():
def test_panel_override_replaces_existing_drive_packets(): # panel_ic_type=... given: plan.remove holds the old drive packets
def test_config_with_an_unparsed_tail_blocks():
def test_no_config_and_no_panel_id_blocks():               # reason "no display configured"
def test_config_without_a_display_blocks_even_with_a_panel_id():
    # A display packet cannot be invented from a panel id: pins, size and bus wiring are
    # board-specific. Blocked; the reason says so and points to a backup or the website toolbox.
```

- [ ] **Step 2:** Run to see them fail. **Step 3:** Implement. **Step 4:** Verify, commit `feat: plan and apply the migration of a config to firmware 3.0 drive data`.

### Task 3: `backup-config` and `migrate`

**Branch:** `feat/cli-migrate`

**Interfaces:**

```
opendisplay backup-config FILE [--device/--host/--key ...]
    Like export-config, but refuses to overwrite an existing file and prints a warning that
    the file contains the key when it does.
opendisplay migrate [--dry-run] [--from-backup FILE] [--panel ID] [--yes-untested] [--strip-secrets]
    Without --from-backup: read the device's config, plan, show the plan, write, read back, compare.
    With --from-backup: use the file as the starting config (for a device whose config was erased).
```

Behaviour of `migrate`:
1. Connect with `opendisplay.connect()`; print generation, target, configured, needs-migration from the profile.
2. Plan. Print `plan.reason`. If not needed: exit 0. If blocked: exit 1.
3. With `--from-backup`: refuse a backup carrying any secret unless `--strip-secrets` (Decision 1), before writing. Say that the device's existing packets are replaced by the backup's, and list the packet types the device has that the backup lacks.
4. If `plan.needs_confirmation` and not `--yes-untested`: ask "This panel's drive data has not been seen on a real panel. Continue?".
5. With `--dry-run`: print the packets to add and remove, with sizes, and stop.
6. Write through `dev.config.write()`, then `dev.config.read()` and compare the serialized bytes; report.

Also add `Device.config.plan_migration(db=None)` to `device/api.py`, returning `plan_migration(self.cached, device.profile, db or PanelDatabase.load())`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_cli_migrate.py`) with a fake `Device`, as `tests/unit/test_cli_write_config.py` fakes `OpenDisplayDevice`: dry run writes nothing; a blocked plan exits 1 and writes nothing; a backup carrying secrets is refused without `--strip-secrets` (the message names the secrets, never their values) and written with the key, password and 0x2D stripped with it; the device's own config is written back with its secrets untouched; a 0x10 refusal surfaces as `ConfigSecretNeedsClaimError`; an `equivalent` panel is not written when the confirmation is declined; a read-back mismatch exits non-zero (Review Focus 5); `backup-config` refuses an existing file.

- [ ] **Step 2–4:** fail, implement, verify, commit `feat(cli): add backup-config and migrate`.

- [ ] **Step 5: Hardware — needs the user's go-ahead for each write.** Target: the E1002 `OD4064C9` (ESP32-S3, firmware 3.0.2, no config; Spectra 800x480, panel_ic_type 35; backup at `scratch/backup-OD4064C9-fw2.26.7.json`, which contains a key).

```bash
uv run opendisplay migrate --device OD4064C9 --from-backup scratch/backup-OD4064C9-fw2.26.7.json --strip-secrets --dry-run
```

Expected: a plan that adds the drive packets for panel 35 and reports whether that panel is `glass` or `equivalent`. Stop and show the plan to the user. With their go-ahead, run it without `--dry-run`. Then: `opendisplay info` shows a display and "Needs migration: no"; upload a test image with `opendisplay upload`; the user confirms what the panel shows. Record the result, including a wrong or blank image, in the ledger. If the upload START is refused, record the frame and stop.

## Part 3b: using the drive data

**Design:** `../specs/2026-11-16-v3-3b-drive-modes-design.md` (approved 2026-11-16). Read it first;
it holds the facts, the wire format, the timeouts and the five decisions. Decision 4 of this plan
(0x0085 targets) is settled by it: availability follows the config's modes, and the Silabs target
documents `0x0085`. Ledger: `.superpowers/sdd/2026-11-16-v3-3-panels-and-migration/progress.md`.

### Task 4: `code_map` on the host

**Branch:** `feat/bwry-code-map`

**Files:** `models/capabilities.py`, `device/imaging.py` (`prepare_image`), `device/facade.py`
(`_extract_capabilities_from_config` path in `imaging._capabilities_from_config`), tests in
`tests/unit/test_bwry_code_map.py`.

**Interfaces:**

```python
@dataclass
class DeviceCapabilities:
    ...
    bwry_codes: tuple[int, int, int, int] | None = None   # from display 0's 0xB1 code_map; None = built-in table
```

- `_capabilities_from_config` sets `bwry_codes` from the first 0xB1 whose `instance_number` is
  display 0's, on any firmware version; `None` without one.
- BWRY encoding uses `capabilities.bwry_codes` when set, else `get_bwry_codes(panel_ic_type)`.
- A map that is not a permutation of 0..3 raises `ImageEncodingError` (port of od-v3.js
  `wireMapFromCodeMap`). Never applied to 0xB0 codes.

**Tests (each must fail without the change):** `(0, 1, 3, 2)` exchanges the red and yellow codes
of a 2x1 red/yellow image against the identity; an invalid map (`(0, 1, 1, 3)`) raises; a config
without 0xB1 gives `bwry_codes is None` and the built-in table (0x001D still swapped);
`tests/unit/test_wire_bytes_golden.py` stays green.

**Hardware (after merge of 5b, with the user):** the red/yellow image on `OD58D951` in the
default mode still draws correctly (decision 4).

### Task 5a: encoding a multi-pass frame

**Branch:** `feat/multipass-encoding`

**Files:** `src/opendisplay/encoding/multipass.py` (no I/O), `tests/unit/test_multipass.py`.

**Interfaces:**

```python
def mode_palette(calibration: DisplayCalibration) -> Palette
    # epaper-dithering Palette from the 0xB0 entries' CIELAB, in entry order; white = highest L*
def encode_multipass(indices: np.ndarray, calibration: DisplayCalibration, passes: int) -> bytes
    # per pass p: index -> entries[index].code_for_pass(p), 2 bpp, first pixel in the high bits,
    # rows padded to a byte; the pass frames concatenated
def prepare_multipass(image, mode: DisplayMode, calibration: DisplayCalibration | None, *, width, height,
                      rotation, fit, dither_mode, ...) -> bytes
    # rotate -> fit -> dither against mode_palette -> encode_multipass
```

- A mode with `passes > 1` and no 0xB0 for its `mode_index` raises `ImageEncodingError`.
- A single-pass named mode is encoded as its scheme's normal frame (the existing path).

**Tests:** exact bytes for a 5x2 index image (row padding exercised) with a hand-made 3-pass,
4-entry calibration; a pass whose codes are all 0 gives an all-zero frame; the frame count and
length equal `passes x rows x ceil(width / 4)`; a missing calibration raises; `mode_palette` keeps
the entry order and picks white by L*.

### Task 5b: the `0x0085` transfer, `Device.display`, the CLI

**Branch:** `feat/drive-write`

**Files:** `protocol/commands.py` (builder), `protocol/responses.py` (START answer and refusal
parser), `exceptions.py`, `device/drive_write.py` (the transfer; `upload.py` only if it fits the
pylint limits), `device/api.py` (`DisplayView`), `cli.py` (`--mode`), `cli_info.py` (mode list),
tests in `tests/unit/test_drive_write.py`, `test_device_api.py`, `test_cli_info_report.py`.

**Interfaces:**

```python
CommandCode.DRIVE_WRITE_START = 0x0085
def build_drive_write_start(mode: int, refresh: int, *, uncompressed_size: int | None = None,
                            first_chunk: bytes = b"") -> bytes
    # [00][85][mode][refresh][flags=0][00 00] (+ uncompressed_size:4 LE + first compressed bytes)
def parse_drive_write_start(data: bytes) -> tuple[int, int, int]   # (mode, passes, temp_c) or raises

class DriveModeSubstitutedError(ProtocolError): requested, actual, temp_c
class DriveModeTemperatureError(ProtocolError): mode, temp_c  (0x06)
class DriveSequenceUnsupportedError(ProtocolError)            (0x07)
# 0x08 -> PanelBusyError, 0x02 -> NeedsMigrationError, other codes -> InvalidResponseError naming the code

class DisplayView:
    async def show(self, image, *, mode: str | None = None, refresh=..., fit=..., ...) -> None
Device.display: DisplayView
```

- `show()` without `mode=` delegates to today's `legacy.upload_image` unchanged.
- `"fast"` and `"partial"` are rejected before any I/O with a message naming the right
  argument; an unknown name raises `ValueError` listing the device's modes (case-insensitive).
- Timeouts: data chunks 90 s; START with inline data `10 s + max(90 s, (passes - 1) x 45 s)`;
  END `max(90 s, passes x 45 s)`. `PIPELINED` is never set. Data goes over `0x71`.
- A START answer naming another mode sends nothing further and raises
  `DriveModeSubstitutedError`.
- CLI: `opendisplay upload --mode NAME`, printing one line that the colours are nominal when the
  mode's calibration is not measured. `opendisplay info`: a drive-mode section (decision 5).

**Tests (scripted connection):** exact START bytes compressed and uncompressed; the pass count is
taken from the answer; substitution sends nothing after START and raises; each refusal code maps
to its error; reserved and unknown names fail before any frame is written; `show()` without
`mode=` writes exactly what `upload_image` writes; the info section and its JSON, and its absence
without 0xAE. Regenerate the public API snapshot (additions only) and list the additions in the PR.

**Hardware (with the user, each upload approved):**
1. E1002 `OD4064C9`: a single-pass upload through `0x0085` (mode 0 and `fast`).
2. `OD58D951`: a 16-swatch `colour16` image, and the red/yellow image in the default mode
   (decision 4).
3. Measure the START answer time and each pass boundary on `OD58D951` and record them: the
   timeout rule is the website's, not yet ours on glass. Captures go to `scratch/`, the result to
   the spec's "Status and findings".

After 3b is seen on glass: the end-of-piece review, as for 3a.

## Not in this piece

Key handover on upgrade (piece 4), the guided upgrade flow (piece 5).
