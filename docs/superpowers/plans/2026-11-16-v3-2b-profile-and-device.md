# Piece 2b: Device Profile and the New `Device` Entry Point — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `EXECUTOR_RULES.md` in this folder first.

**Goal:** One immutable description of a connected device that is correct for firmware 2.x and 3.0, and the first part of the new public surface built on it.

**Architecture:** A pure function turns firmware version, config and identity into a `DeviceProfile`. A new class `Device`, returned by `opendisplay.connect()`, wraps today's `OpenDisplayDevice` and exposes `profile` and a `config` view. Nothing is removed; `OpenDisplayDevice` keeps working unchanged.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff, mypy strict, pylint, prek.

**Spec:** `../specs/2026-11-15-v3-architecture-design.md` (sections "Seam 2: the device profile", "Public surface", "When the new surface appears", "Status and findings").

**Depends on:** piece 2a fully merged (`read_ident`, `NoStoredConfigError`, advertisement state, typed refusals, length-prefixed packets).

## Decisions to confirm with the user before Task 2

1. **The new surface is a new class, `opendisplay.Device`, not new attributes on `OpenDisplayDevice`.** Reason: the spec wants `dev.config.read()`, but `OpenDisplayDevice.config` already exists as a property returning `GlobalConfig`, and that cannot change without breaking Home Assistant. `Device` holds an `OpenDisplayDevice` and delegates to it for now; piece 7 inverts that.
2. **`opendisplay.connect()` does not fail on a device without config.** It returns a `Device` whose profile says `configured=False`.
3. **The identity is read only when the firmware can answer**: major version 3 and up, or `ic_type` EFR32BG22. Other 2.x firmware is silent and would cost every connect 2 seconds.
4. **`connect()` costs one extra round trip against today** (the firmware version read). Home Assistant already reads the version separately, so in practice it moves, it does not add.

## Global Constraints

- Everything in `EXECUTOR_RULES.md`.
- Additive only: no change to `OpenDisplayDevice`'s public names, signatures or behaviour, except the one new keyword argument in Task 2.
- `facade.py` may not grow by more than the lines Task 2 names. New code goes into new modules.
- A profile is built from data already read. Building one performs no I/O.

## Review Focus

1. **A 3.0 device on a slow link.** `read_ident` raising `IdentNotSupportedError` with `timed_out=True` on firmware major 3 must not produce a 2.x profile; the generation comes from the firmware version, and the profile records that the identity is missing.
2. **A device with a config but zero displays** (2.x allows it). The profile must be built, with `configured=False`, not raise `IndexError`.
3. **An unknown colour scheme value** in the config or the identity (3.0 defines scheme 10, BWRY16, which `ColorScheme` does not have). The profile keeps the raw value and does not raise.
4. **`async with opendisplay.connect(...)` when the connection or the profile read fails halfway.** The link must be closed, as `OpenDisplayDevice.__aenter__` does today for its own failures.
5. **A caller that passes both `ble_device` and `host`.** Must be rejected with the same error `OpenDisplayDevice` raises for conflicting addressing.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/opendisplay/models/profile.py` | `Generation`, `DeviceProfile`, `build_profile()`. Pure |
| `src/opendisplay/device/api.py` | `Device`, `ConfigView`, `connect()` |
| `src/opendisplay/device/facade.py` | one new keyword argument, `auto_interrogate` |
| `src/opendisplay/__init__.py`, `models/__init__.py`, `device/__init__.py` | exports |
| `tests/unit/test_profile.py`, `tests/unit/test_device_api.py` | tests |

---

### Task 1: `DeviceProfile` and `build_profile`

**Branch:** `feat/device-profile`

**Files:**
- Create: `src/opendisplay/models/profile.py`, `tests/unit/test_profile.py`
- Modify: `src/opendisplay/models/__init__.py`, `src/opendisplay/__init__.py`, `tests/unit/data/public_api.json`

**Interfaces:**
- Consumes: `DeviceIdent`, `FirmwareTarget` (`models/ident.py`), `GlobalConfig` and its `display_modes` / `display_panels` views, `FirmwareVersion`, `ColorScheme`, `ICType`.
- Produces:

```python
class Generation(Enum):
    V2 = "2.x"          # firmware major below 3
    V3 = "3.x"          # firmware major 3 and up: config-driven, SEC2

@dataclass(frozen=True)
class DeviceProfile:
    generation: Generation
    firmware: FirmwareVersion
    target: FirmwareTarget | int | None      # from the identity; else mapped from config ic_type; else None
    ident: DeviceIdent | None                # None: not asked (2.x) or not answered
    ident_missing: bool                      # True when a V3 device did not answer READ_IDENT
    configured: bool                         # a config with at least one display is stored
    needs_migration: bool                    # V3 only: identity flag, or a display without drive packets
    width: int | None                        # config display 0, else identity record 0, else None
    height: int | None
    color_scheme: ColorScheme | int | None   # enum when known, raw int when not
    rotation: int | None
    max_config: int                          # identity max_config when present, else 4096
    supports_partial: bool
    supports_pipe: bool
    supports_compression: bool
    drive_modes: tuple[str, ...]             # names from the config's 0xAE packets, in mode_index order; () on V2
    code_map: tuple[int, int, int, int] | None   # from the first 0xB1 packet; None when absent

def build_profile(
    firmware: FirmwareVersion,
    config: GlobalConfig | None,
    ident: DeviceIdent | None,
    *,
    ident_missing: bool = False,
) -> DeviceProfile
```

Rules for `build_profile`:
- `generation`: `V3` when `firmware["major"] >= 3`, else `V2`. Nothing else decides it.
- `target`: `ident.fw_target` when there is an identity. Otherwise map `config.system.ic_type`: `ICType.NRF52840 -> FirmwareTarget.NRF52840`, `ESP32_S3 / ESP32_C3 / ESP32_C6 -> ESP32`, `NRF52811 -> NRF52811`, `EFR32BG22 -> EFR32BG22`; anything else `None`.
- `configured`: `config is not None and len(config.displays) > 0`.
- `needs_migration`: `False` on V2. On V3: `ident.needs_migration` when the identity has a tail; otherwise `configured and not config.display_modes and not config.display_controllers`.
- `supports_partial`, `supports_pipe`, `supports_compression`: from display 0 of the config exactly as today's code derives them (`DisplayConfig.partial_update_support`, `supports_pipe_write`, `supports_zip or supports_streaming_decompression`); all `False` without a configured display.
- `color_scheme`: `ColorScheme.from_value(raw)`, and the raw int when that raises `ValueError`.

- [ ] **Step 1: Branch** `feat/device-profile` from `main`.

- [ ] **Step 2: Write the failing tests** in `tests/unit/test_profile.py`. Inputs are real:

```python
FW2 = {"major": 2, "minor": 27, "patch": 1, "sha": "a" * 40}
FW3 = {"major": 3, "minor": 0, "patch": 0, "sha": "0" * 40}
IDENT_V3_NO_DISPLAY = parse_read_ident(bytes.fromhex("004604010029a00f800003"))
IDENT_NEEDS_MIGRATION = parse_read_ident(bytes.fromhex("0046040101" "23002003e00105" "0ba00f800003"))
ALPHA_CONFIG = parse_config_response(<tests/fixtures/configs/bg22_alpha_drive_packets.bin>)

def test_fresh_3_0_device_without_config():
    p = build_profile(FW3, None, IDENT_V3_NO_DISPLAY)
    assert (p.generation, p.target, p.configured, p.needs_migration) == (Generation.V3, FirmwareTarget.ESP32, False, False)
    assert (p.width, p.height, p.color_scheme) == (None, None, None)
    assert p.max_config == 4000

def test_3_0_device_whose_config_lacks_drive_data():
    p = build_profile(FW3, <a 2.x-style config with one 800x480 BWGBRY display, no length-prefixed packets>, IDENT_NEEDS_MIGRATION)
    assert (p.configured, p.needs_migration, p.width, p.height) == (True, True, 800, 480)

def test_config_driven_device_reports_its_modes_and_code_map():
    p = build_profile(<fw major 3>, ALPHA_CONFIG, None, ident_missing=True)
    assert p.drive_modes == ("full", "colour16")
    assert p.code_map == (0, 1, 3, 2)
    assert (p.needs_migration, p.ident_missing) == (False, True)      # has drive packets; Review Focus 1

def test_2x_device_is_described_from_its_config_alone():
    p = build_profile(FW2, <the E1003-like config from tests/unit/test_cli_info_report.py::_config>, None)
    assert (p.generation, p.ident, p.ident_missing, p.needs_migration, p.max_config) == (Generation.V2, None, False, False, 4096)
    assert p.target is FirmwareTarget.ESP32 and p.drive_modes == () and p.code_map is None

def test_3_0_firmware_is_v3_even_without_an_identity():                 # Review Focus 1
    assert build_profile(FW3, None, None, ident_missing=True).generation is Generation.V3

def test_config_without_displays_is_not_configured():                   # Review Focus 2
    p = build_profile(FW2, dataclasses.replace(<config>, displays=[]), None)
    assert (p.configured, p.width, p.supports_partial) == (False, None, False)

def test_unknown_colour_scheme_is_kept_as_its_raw_value():              # Review Focus 3
    p = build_profile(FW3, dataclasses.replace(<config>, displays=[dataclasses.replace(<display>, color_scheme=10)]), None)
    assert p.color_scheme == 10

def test_transfer_support_follows_the_display_config():
    # one config with partial + pipe + zip bits set, one with none: assert the three booleans for each
```

- [ ] **Step 3: Run to see them fail.** `uv run pytest tests/unit/test_profile.py -q` → ImportError.

- [ ] **Step 4: Implement** `models/profile.py`. Export `DeviceProfile`, `Generation`, `build_profile` from `opendisplay.models` and `opendisplay`.

- [ ] **Step 5: Snapshot, verify, commit.**

```bash
UPDATE_API_SNAPSHOT=1 uv run pytest tests/unit/test_public_api_snapshot.py -q
uv run pytest -q && uv run prek run --all-files
git add src/opendisplay tests/unit && uv run git commit -m "feat: describe a device with one profile built from firmware, config and identity"
```

---

### Task 2: `Device`, `ConfigView` and `opendisplay.connect()`

**Branch:** `feat/device-entry-point` (after Task 1 is merged, and after the user confirmed the four decisions above)

**Files:**
- Create: `src/opendisplay/device/api.py`, `tests/unit/test_device_api.py`
- Modify: `src/opendisplay/device/facade.py`, `src/opendisplay/device/__init__.py`, `src/opendisplay/__init__.py`, `tests/unit/data/public_api.json`, `README.md`

**Interfaces:**
- Consumes: `build_profile`, `OpenDisplayDevice`, `NoStoredConfigError`, `IdentNotSupportedError`.
- Produces:

```python
# facade.py: one new keyword-only constructor argument, default keeps today's behaviour
OpenDisplayDevice(..., auto_interrogate: bool = True)
# When False, __aenter__ connects and authenticates but does not read the config.

# api.py
class ConfigView:
    @property
    def cached(self) -> GlobalConfig | None          # last config read or written; None when the device has none
    async def read(self) -> GlobalConfig              # raises NoStoredConfigError when the device has none
    async def write(self, config: GlobalConfig) -> None    # then refreshes the profile

class Device:
    profile: DeviceProfile                            # property; set during connect, refreshed by refresh()
    config: ConfigView
    legacy: OpenDisplayDevice                         # the wrapped object, for everything not yet on the new surface
    async def refresh(self) -> DeviceProfile          # re-read firmware version, config and identity
    async def __aenter__(self) -> Device
    async def __aexit__(self, *exc) -> None

def connect(
    address: str | None = None, *,
    name: str | None = None,
    ble_device: BLEDevice | None = None,
    transport: Transport | None = None,
    host: str | None = None, port: int | None = None, tls: bool = False, psk: bytes | None = None,
    encryption_key: bytes | None = None,
    timeout: float = 10.0,
) -> Device                                           # use as: async with opendisplay.connect(...) as dev
```

`connect()` only builds the `Device`; `Device.__aenter__` does the work:
1. enter the wrapped `OpenDisplayDevice` (`auto_interrogate=False`);
2. read the firmware version;
3. read the config; on `NoStoredConfigError` continue with `None`;
4. read the identity only if `firmware major >= 3` or the config's `ic_type` is EFR32BG22; on `IdentNotSupportedError` continue with `None` and remember `ident_missing = (major >= 3)`;
5. `build_profile(...)`.
If any step other than the two tolerated errors raises, exit the wrapped device and re-raise.

`address` maps to `mac_address`, `name` to `device_name`. Conflicting addressing is left to `OpenDisplayDevice.__init__`, which already validates it.

- [ ] **Step 1: Branch.**

- [ ] **Step 2: Write the failing tests** in `tests/unit/test_device_api.py`, with `fake_transport` and scripted replies. Build the config reply with `serialize_config` the way `tests/unit/test_protocol_error_frames.py` builds interrogate replies.

```python
async def test_connect_to_a_2x_device_reads_version_and_config_but_not_identity(fake_transport):
    # replies: fw 2.27 version frame, config frames. Assert: profile.generation is V2,
    # profile.width matches, and no frame b"\x00\x46" was written.

async def test_connect_to_a_3_0_device_reads_its_identity(fake_transport):
    # replies: fw 3.0 version, no-config frame ff400000, identity 004604010029a00f800003.
    # Assert: profile.configured is False, profile.max_config == 4000, dev.config.cached is None,
    # written opcodes in order: 0x43, 0x40, 0x46.

async def test_3_0_device_that_does_not_answer_the_identity_is_still_v3(fake_transport):   # Review Focus 1
    # fw 3.0, a config, then a timeout for 0x46 -> generation V3, ident_missing True.

async def test_failure_while_building_the_profile_closes_the_link(fake_transport):       # Review Focus 4
    # fw version reply is garbage -> the exception propagates and fake.is_connected is False.

async def test_config_read_on_a_device_without_config_raises(fake_transport):
    # connect succeeds (no-config), then `await dev.config.read()` raises NoStoredConfigError.

async def test_config_write_refreshes_the_profile(fake_transport):
    # start without config, write one (script the write ACKs and the re-read), assert profile.configured flips to True.

def test_conflicting_addressing_is_rejected():                                           # Review Focus 5
    with pytest.raises(ValueError):
        opendisplay.connect("AA:BB:CC:DD:EE:FF", host="10.0.0.5")

async def test_auto_interrogate_false_sends_no_config_read(fake_transport):
    # OpenDisplayDevice(transport=fake, auto_interrogate=False): entering writes nothing.

async def test_default_still_interrogates(fake_transport):
    # OpenDisplayDevice(transport=fake) with a scripted config: the first frame written is 0x0040, as today.
```

- [ ] **Step 3: Run to see them fail.**

- [ ] **Step 4: Implement.** In `facade.py` the change is the constructor argument, its docstring line, and one condition around the existing auto-interrogate block. If `facade.py` would exceed 1,500 lines, first move `landing_url` and `_device_id_bytes` into `opendisplay/landing.py` helpers in a separate refactor commit.

- [ ] **Step 5: README.** Add a short "New entry point" section showing `async with opendisplay.connect(...) as dev:` with `dev.profile` and `dev.config.read()`, and stating that `OpenDisplayDevice` remains supported.

- [ ] **Step 6: Snapshot, verify.** `uv run pytest -q && uv run prek run --all-files`.

- [ ] **Step 7: Hardware (read-only).**

```bash
uv run python -c "import asyncio, opendisplay
async def main():
    async with opendisplay.connect(name='OD4064C9', timeout=30) as dev: print(dev.profile)
asyncio.run(main())"
```

Expected on the E1002 (3.0.2, no config): `generation=V3`, `target=ESP32`, `configured=False`, `max_config=4000`. Then the same against the nRF52840 kit `OD216205` (2.27.0): `generation=V2`, `ident=None`, and the run is no slower than `opendisplay info`. Remember the Spectra boards need about 30 seconds after a reset before they advertise.

- [ ] **Step 8: Commit.**

```bash
git add src/opendisplay tests/unit README.md
uv run git commit -m "feat: add opendisplay.connect() returning a Device with a profile and a config view"
```

## Not in this piece

`dev.display`, `dev.security`, `dev.firmware`, `dev.io` (pieces 3, 4, 5); making `OpenDisplayDevice` delegate to `Device` (piece 7).
