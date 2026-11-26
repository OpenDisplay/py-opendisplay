# py-opendisplay

[![PyPI](https://img.shields.io/pypi/v/py-opendisplay?style=flat-square)](https://pypi.org/project/py-opendisplay/)
[![Python](https://img.shields.io/pypi/pyversions/py-opendisplay?style=flat-square)](https://pypi.org/project/py-opendisplay/)
[![License](https://img.shields.io/github/license/OpenDisplay-org/py-opendisplay?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/OpenDisplay-org/py-opendisplay/test.yml?style=flat-square&label=tests)](https://github.com/OpenDisplay-org/py-opendisplay/actions/workflows/test.yml)
[![Lint](https://img.shields.io/github/actions/workflow/status/OpenDisplay-org/py-opendisplay/lint.yml?style=flat-square&label=lint)](https://github.com/OpenDisplay-org/py-opendisplay/actions/workflows/lint.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&style=flat-square)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-strict-blue?style=flat-square)](https://mypy.readthedocs.io/)

Python library for communicating with OpenDisplay BLE e-paper displays.

## Installation

```bash
pip install py-opendisplay
```

## Quick Start

### Option 1: Using MAC Address

```python
from opendisplay import OpenDisplayDevice
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")
    await device.upload_image(image)
```

### Option 2: Using Device Name (Auto-Discovery)

```python
from opendisplay import OpenDisplayDevice, discover_devices
from PIL import Image

# List available devices
devices = await discover_devices()
print(devices)  # {"OpenDisplay-A123": "AA:BB:CC:DD:EE:FF", ...}

# Connect using name
async with OpenDisplayDevice(device_name="OpenDisplay-A123") as device:
  image = Image.open("photo.jpg")
  await device.upload_image(image)
```

### New entry point: `opendisplay.connect()`

`connect()` returns a `Device` that describes the device it connected to, for firmware 2.x and 3.0
alike. It also connects to a device that has no config yet.

```python
import opendisplay

async with opendisplay.connect("AA:BB:CC:DD:EE:FF") as dev:
    print(dev.profile.generation, dev.profile.width, dev.profile.height, dev.profile.configured)
    config = await dev.config.read()   # raises NoStoredConfigError when the device has none
```

`await dev.config.clear()` erases the stored config (CONFIG_CLEAR 0x0045) and refreshes the
profile. A claimed firmware 3.0 device keeps its reset-pin settings (the 0x27 packet).

`dev.profile` is built from the firmware version, the stored config and, on firmware 3.0 and up, the
device identity. Everything not yet on the new surface is reached through `dev.legacy`, the wrapped
`OpenDisplayDevice`. `OpenDisplayDevice` remains supported.

#### Drive modes (firmware 3.0)

A firmware 3.0 config can declare several drive modes for a panel, for example `colour16`, which
drives a BWRY panel in three passes to show 16 colours. `dev.profile.drive_mode_names` lists them;
`dev.display.show()` uploads in one:

```python
async with opendisplay.connect("AA:BB:CC:DD:EE:FF") as dev:
    print(dev.profile.drive_mode_names)        # the modes the config declares, e.g. colour16
    await dev.display.show(image, mode="colour16")
```

Without `mode`, `show()` is `upload_image()` with the same options. If the device runs a different
mode than the one asked for, `DriveModeSubstitutedError` is raised and nothing is uploaded.

## CLI

Run without installing (requires [uv](https://docs.astral.sh/uv/)):

```bash
uvx --from "py-opendisplay[cli]" opendisplay --help
```

Or after `pip install py-opendisplay[cli]`:

```bash
# Discover nearby devices
opendisplay scan

# Read device info (size, color, firmware, board)
opendisplay info --device AA:BB:CC:DD:EE:FF

# Upload an image
opendisplay upload --device AA:BB:CC:DD:EE:FF photo.jpg
opendisplay upload --device AA:BB:CC:DD:EE:FF photo.jpg --fit cover --refresh-mode fast

# Reboot the device
opendisplay reboot --device AA:BB:CC:DD:EE:FF

# Export / write device configuration
opendisplay export-config --device AA:BB:CC:DD:EE:FF config.json
opendisplay write-config --device AA:BB:CC:DD:EE:FF config.json

# Upload in a drive mode the config declares (firmware 3.0; `info` lists them)
opendisplay upload --device AA:BB:CC:DD:EE:FF photo.jpg --mode colour16

# Move a device to firmware 3.0 (see "Moving to firmware 3.0" below)
opendisplay backup-config --device OD4064C9 backup.json
opendisplay migrate --device OD4064C9 --dry-run
opendisplay migrate --device OD4064C9

# Set up a 3.0 display, or move a 2.x one to 3.0 (see "Setting up a display" below)
opendisplay setup --device OD4064C9 --claim
opendisplay upgrade --device OD5A2F4C --no-key --state-file en05.json

# Send a firmware 3.x image (see "Firmware 3.x updates" below)
opendisplay update --device OD4064C9                # the release's image for the board it reports
opendisplay update --device OD4064C9 --file opendisplay-s3-n32r8-extuart-app.bin

# Debugging: send one command frame and print the reply as hex (nothing checks it)
opendisplay raw --device OD4064C9 --cmd 0043
opendisplay raw --device OD4064C9 --cmd 000f --no-reply

# Firmware 3.0 device keys (see "Firmware 3.0 security" below)
opendisplay claim --device OD4064C9
opendisplay rotate-key --device OD4064C9
opendisplay unclaim --device OD4064C9
opendisplay key export --device OD4064C9
opendisplay key import --device OD4064C9 0102030405060708090a0b0c0d0e0f10
```

Firmware 2.x devices with encryption require `--key HEX` (32 hex characters). Firmware 3.0 keys
live in a key store, `~/.config/opendisplay/keys.json` by default (`--key-store PATH` to change
it). Pass `-v` / `--verbose` for debug logging.

Every command that talks to a display reads the key store, so a **claimed** firmware 3.0 display
works with all of them once its key is there (from `claim`, or `key import`).

`export-config` keeps config packets this library does not recognise in an `unparsed_tail` field, so writing the file back restores them. If a file has lost that field (for example after editing it in another tool) and the device has such packets, `write-config` refuses to write. Pass `--keep-unknown-packets` to carry the device's packets over unchanged, or `--discard-unknown-packets` to delete them.

## Image Fitting

Images are automatically fitted to the display dimensions. Control how aspect ratio mismatches are handled with the `fit` parameter:

```python
from opendisplay import OpenDisplayDevice, FitMode
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")

    # Default: scale to fit, pad with white (no distortion, no cropping)
    await device.upload_image(image, fit=FitMode.CONTAIN)

    # Scale to cover display, crop overflow (no distortion, fills display)
    await device.upload_image(image, fit=FitMode.COVER)

    # Distort to fill exact dimensions
    await device.upload_image(image, fit=FitMode.STRETCH)

    # No scaling, center-crop at native resolution (pad if smaller)
    await device.upload_image(image, fit=FitMode.CROP)
```

| Mode                | Aspect Ratio | Fills Display          | Content Loss            |
|---------------------|--------------|------------------------|-------------------------|
| `CONTAIN` (default) | Preserved    | No (white padding)     | None                    |
| `COVER`             | Preserved    | Yes                    | Edges cropped           |
| `STRETCH`           | Distorted    | Yes                    | None (but distorted)    |
| `CROP`              | Preserved    | Depends on source size | Edges cropped if larger |

## Image Rotation

Rotate source images before fitting/encoding using the `rotate` parameter:

```python
from opendisplay import OpenDisplayDevice, Rotation
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")
    await device.upload_image(image, rotate=Rotation.ROTATE_90)
```

Rotation is applied before `fit`, so crop/pad behavior matches the rotated orientation.
Rotation angles use clockwise semantics (`ROTATE_90` = 90 degrees clockwise).

## Dithering Algorithms

E-paper displays have limited color palettes, requiring dithering to convert full-color images. py-opendisplay supports 9 dithering algorithms with different quality/speed tradeoffs:

### Available Algorithms

- **`none`** - Direct palette mapping without dithering (fastest, lowest quality)
- **`ordered`** - Bayer/ordered dithering using pattern matrix (fast, visible patterns)
- **`burkes`** - Burkes error diffusion (default, good balance)
- **`floyd-steinberg`** - Floyd-Steinberg error diffusion (most popular, widely used)
- **`sierra-lite`** - Sierra Lite (fast, simple 3-neighbor algorithm)
- **`sierra`** - Sierra-2-4A (balanced quality and performance)
- **`atkinson`** - Atkinson (designed for early Macs, artistic look)
- **`stucki`** - Stucki (high quality, wide error distribution)
- **`jarvis-judice-ninke`** - Jarvis-Judice-Ninke (highest quality, smooth gradients)

### Usage Example

```python
from opendisplay import OpenDisplayDevice, RefreshMode, DitherMode
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")

    # Use Floyd-Steinberg dithering
    await device.upload_image(
        image,
        dither_mode=DitherMode.FLOYD_STEINBERG,
        refresh_mode=RefreshMode.FULL
    )
```
### Comparing Dithering Modes

To preview how different dithering algorithms will look on your e-paper display, use the **[img2lcd.com](https://img2lcd.com/)** online tool.
Upload your image and compare the visual results before choosing an algorithm.

**Quality vs Speed Tradeoff:**

| Category                 | Algorithms                            |
|--------------------------|---------------------------------------|
| Fastest / Lowest Cost    | `none`, `ordered`, `sierra-lite`      |
| Best Cost-to-Quality     | `floyd-steinberg`, `burkes`, `sierra` |
| Heavy / Rarely Worth It  | `stucki`, `jarvis-judice-ninke`       |
| Stylized / High Contrast | `atkinson`                            |

## Color Palettes

py-opendisplay automatically selects the best color palette for your display based on its hardware specifications.

### Measured and Idealised Palettes

**Measured palettes** (default) use colours measured on real panels:
- Spectra 6 7.3" (ep73_spectra_800x480), also used for the 13.3" split panel (T133A01)
- 3.97" BWRY (ep397yr_800x480)

**Idealised palettes** use the pure colours of the display's colour scheme (black, white, red, ...).
Every other display uses these.

To add a measured palette for your panel, see [docs/palettes.md](docs/palettes.md).

In both, the pure inks are named, so a colour that asks for an ink (such as `#000000` text) comes
out as that ink, solid.

### Disabling Measured Palettes

To use the idealised palette even where a measured one exists (useful for testing or comparison):

```python
from opendisplay import OpenDisplayDevice

async with OpenDisplayDevice(
    mac_address="AA:BB:CC:DD:EE:FF",
    use_measured_palettes=False
) as device:
    await device.upload_image(image)
```

### Tone and Gamut

Two opt-in strengths from `0.0` (off, the default) to `1.0`:

- `tone` lifts the black point to bring out shadow detail, at the cost of the panel's deepest black.
- `gamut` pulls colours the panel can't show towards ones it can before dithering.

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Default: both off
    await device.upload_image(image)

    # Full black-point lift and gamut compression for a photo
    await device.upload_image(image, tone=1.0, gamut=1.0)
```

## Refresh Modes

Control how the display updates when uploading images:

```python
from opendisplay import RefreshMode

await device.upload_image(
    image,
    refresh_mode=RefreshMode.FULL  # Options: FULL, FAST
)
```

### Available Modes

| Mode               | Description                                                                                              |
|--------------------|----------------------------------------------------------------------------------------------------------|
| `RefreshMode.FULL` | Full display refresh \(default\). Cleanest image quality; eliminates ghosting; slower \(~5–15 seconds\). |
| `RefreshMode.FAST` | Fast refresh. Quicker updates; may show slight ghosting. Only supported on some B/W displays.            |

Note: Fast refresh support varies by display hardware. Color and grayscale displays only support full refresh.

## Partial Updates

B/W (MONO) panels with `partial_update_support` in their device config can update
just the changed region of the screen using the differential 0x76 protocol —
no full-refresh flash. Partial updates are **opt-in**: create a `PartialState`
and pass it to every upload. The first upload is always full; subsequent uploads
diff against the previous frame and go partial when possible.

```python
from opendisplay import OpenDisplayDevice, PartialState

state = PartialState()

async with OpenDisplayDevice(device_name="OD1234AB", encryption_key=key) as device:
    await device.upload_image(dashboard_frame(1), state=state)  # full upload
    await device.upload_image(dashboard_frame(2), state=state)  # partial (flicker-free)
    await device.upload_image(dashboard_frame(3), state=state)  # partial
```

The library handles all preconditions and falls back to a full upload
automatically (unsupported panel, etag mismatch after another client wrote the
screen, firmware NACK, no previous frame). `state` is mutated in place; persist
it across processes with `state.to_bytes()` / `PartialState.from_bytes()`.

Practical notes:

- Blacks rendered by the partial waveform are slightly lighter than a full
  refresh. Do a periodic full refresh (e.g. every 10-50 updates, or on a
  schedule) by passing `state=None` once — this also removes any ghosting.
- Requires reference firmware ≥ 1.8 and `partial_update_support: 1` in the
  device config; the panel itself must support differential refresh.
- Some panels (e.g. EP426 / Seeed EN05) require the partial stream to cover the
  **full panel**: firmware white-fills the controller RAM at partial start, so
  content outside the region would be erased
  ([Firmware#80](https://github.com/OpenDisplay/Firmware/issues/80)). Device
  configs signal this with `partial_update_support: 2`
  (`PartialUpdateSupport.FULL_FRAME`), and the library expands the region to the
  whole screen automatically — still flicker-free. Older device configs may
  still report `1` on affected panels; update the config to `2` to get correct
  behavior.

## Advanced Features

### Device Interrogation

Query the complete device configuration including hardware specs, sensors, and capabilities:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Automatic interrogation on first connect
    config = device.config
    
    print(f"IC Type: {config.system.ic_type_enum.name}")
    print(f"Displays: {len(config.displays)}")
    print(f"Sensors: {len(config.sensors)}")
    print(f"WiFi config present: {config.wifi_config is not None}")
```

Skip interrogation if the device info is already cached:
```python

# Provide cached config to skip interrogation
device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=cached_config)

# Or provide minimal capabilities
capabilities = DeviceCapabilities(296, 128, ColorScheme.BWR, 0)
device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=capabilities)
```

### Firmware Version

Read the device firmware version including git commit SHA:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    fw = await device.read_firmware_version()
    print(f"Firmware: {fw['major']}.{fw['minor']}")
    print(f"Git SHA: {fw['sha']}")

    # Example output:
    # Firmware: 0.65
    # Git SHA: e63ae32447a83f3b64f3146999060ca1e906bf15
```

### Writing Configuration

Modify device settings and write them back to the device:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Read current config
    config = device.config

    # Modify settings
    config.displays[0].rotation = 1

    # Write config back to device
    await device.write_config(config)

    # Reboot to apply changes
    await device.reboot()
```

**Note:** Many configuration changes (rotation, pin assignments, IC type) require a device reboot to take effect.
`write_config()` requires `system`, `manufacturer`, `power`, and at least one display.
When present, optional `wifi_config` (packet `0x26`) is preserved on write.

#### JSON Import/Export

Export and import configurations using JSON files compatible with the [Open Display Config Builder](https://opendisplay.org/firmware/config/) web tool:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Export current config to JSON
    device.export_config_json("my_device_config.json")

# Import config from JSON file
config = OpenDisplayDevice.import_config_json("my_device_config.json")

# Write imported config to another device
async with OpenDisplayDevice(mac_address="BB:CC:DD:EE:FF:00") as device:
    await device.write_config(config)
    await device.reboot()
```

`import_config_json()` raises `ValueError` if required packets (`system`, `manufacturer`, `power`) or all display packets are missing.
JSON packet id `38` (`wifi_config` / TLV `0x26`) is supported for import/export.

### Moving to firmware 3.0

Firmware 3.0 drives the panel from data in the device's config. A device updated from 2.x needs that
data before it can draw; `dev.profile.needs_migration` says so. The CLI does the move:

```bash
opendisplay backup-config --device OD4064C9 backup.json   # never overwrites a file
opendisplay migrate --device OD4064C9 --dry-run           # show what would change
opendisplay migrate --device OD4064C9                     # write it, then read it back
```

- The drive data comes from the latest Firmware_Unified release (from 3.1.0 on each release
  carries its panel database, checked against the release manifest). `--release TAG` picks
  another release, and `--offline` uses the copy bundled with py-opendisplay, which is also the
  fallback when the release cannot be read. `setup` and `upgrade` take it from the release they
  install. The command prints which one it used.
- `--panel ID` picks the panel (`panel_ic_type`) when the config does not say which one it is.
- `--yes-untested` writes drive data not yet seen working on real glass without asking.
- `--from-backup FILE` restores a backup instead of starting from the device's config. A backup
  that carries secrets (the 2.x key, a WiFi password, FindMy keys) is written inside the session
  of a claimed device; an unclaimed device does not store secrets, so it is refused there unless
  `--strip-secrets` writes the backup without them. Claim the display first to keep them.

In Python, `dev.config.plan_migration()` returns the plan and `dev.config.write(config)` writes a
config and reads it back. `plan_migration(db)` takes a database:
`await PanelDatabase.for_release(manifest, fetch)` builds it from a release (falling back to the
bundled copy), `PanelDatabase.load()` is the bundled copy.

### Firmware 3.0 security (claiming a display)

Firmware 3.0 replaces the 2.x key with a key the display generates itself. A display without a key
is open, as before. Claiming gives it a key that only you hold:

1. Put the display in claim mode: switch it off and on, wait about 5 seconds, switch it off and on
   again. It shows SETUP MODE. Leave it off for at least 2 seconds each time.
2. Run `opendisplay claim --device OD4064C9`. The display draws a 6-digit code (a colour panel can
   take a minute); type it in. **If a code was already showing before you started, stop: someone
   else is claiming the display.**
3. The new key is saved in the key store. Every later connect opens an encrypted session with it.

A wrong code is never sent to the display, and nothing is kept. If the key is lost, factory-reset
the display with five quick power cycles. A config can turn that pattern off (0x27 flags bit 6,
`SecurityConfig.no_reset_pattern`; `info` shows it as "Reset pattern"). Without a reset pin as well,
a lost key then needs a wired reflash, so `dev.config.write()` and `write-config` warn before writing
such a config to a claimed display.

In Python, pass a key store; `connect()` reads the display's security state before any handshake
and picks the scheme itself (2.x, firmware 3.0 unclaimed, or firmware 3.0 claimed). A 3.0 build
without the security module (nRF52811) connects open, and a 2.x BG22 with encryption on gets its
2.x handshake before the firmware version read, which it otherwise refuses:

```python
import opendisplay
from opendisplay.sec2.keystore import JsonFileKeyStore

store = JsonFileKeyStore()   # ~/.config/opendisplay/keys.json, mode 0600

async with opendisplay.connect(name="OD4064C9", key_store=store) as dev:
    if dev.security.status.claim_mode:
        flow = await dev.security.claim()
        await flow.begin()
        code = await flow.exchange_nonces()     # the display now draws the code
        await flow.confirm(input("Code: "))     # checked here before anything is sent
    await dev.display.show(image)               # sealed in the session
```

- **Keys.** `encryption_key=` takes a `DeviceKey` (firmware 3.0), a `LegacyKey` (2.x) or plain
  bytes, tried with whichever scheme the display speaks. A key is stored as confirmed only after it
  opens a session, and a key the display refuses is never deleted. At most three keys are tried.
- **A key migrated from 2.x** is replaced by a display-generated one right after the first session.
  Pass `rotate_legacy_key=False` to keep it.
- **`dev.security`**: `status`, `claim()`, `rotate_key()`, `unclaim()`, `use_without_key()` (leave
  claim mode and use the display open).
- **Several clients** (Home Assistant, the web tool, another computer) share one key per display.
  Move it with `opendisplay key export` / `opendisplay key import` (hex, or an
  `opendisplay.org/l/` link that carries the key). Anyone with the key can change the display,
  update its firmware, and read its WiFi password and FindMy keys. After `rotate-key`, every other
  client needs the new key.
- **Errors** say what to do: `NotInClaimModeError` (power-cycle twice), `Sec2KeyRequiredError` (no
  key opened a session; import it, or factory-reset), `Sec2CodeMismatchError`, `DeviceLockedError`,
  `RateLimitedError`, `Sec2BusyError`, and `Sec2RequiredError` when a 2.x-only API meets a claimed
  display. `Sec2KeyRequiredError`, `DeviceLockedError` and `Sec2RequiredError` import from
  `opendisplay`; the others from `opendisplay.sec2.errors`.
- **LAN**: claiming, sessions and key changes run over Bluetooth only. TLS with a firmware 3.0 key
  is not supported yet.

### Firmware 3.x updates

`dev.firmware.update(image)` updates a device that already runs firmware 3.x, over Bluetooth. The
method follows the device: mcumgr SMP on ESP32, nRF52840 and nRF54 (built in, no extra needed), the
Silicon Labs AppLoader on a BG22 (the `silabs-ota` extra). nRF52811 and Telink are not supported
yet. The first move from 2.x to 3.0 is a different job; see "Moving to firmware 3.0".

```python
from opendisplay.firmware import ReleaseManifest, UpdateOutcome

manifest = ReleaseManifest.parse(manifest_json)  # the release's manifest.json, as bytes

async with opendisplay.connect(name="OD4064C9") as dev:
    name = manifest.ble_ota_file(dev.profile.ident.board)  # the board READ_IDENT reports
    image = ...  # the release file `name`, downloaded and checked against the manifest
    if not dev.firmware.can_roll_back:
        ...  # a BG22 cannot undo a bad image by itself: ask the user first
    result = await dev.firmware.update(image, on_progress=lambda done, total: print(done, total))
    print(result.outcome, result.version_before, result.version_after, result.detail)
```

- **Outcomes**: `UPDATED`, `ROLLED_BACK` (the new image did not start and the device is back on its
  old one), `WAITING_IN_BOOTLOADER` (a BG22 in its AppLoader, advertising as `OTA`, waiting for a
  `.gbl`), and `NOT_UPDATED` (for example a transfer cut off part-way, which leaves the old image
  running). `detail` says what to do next.
- **Refused before anything is sent** (`UpdateRefusedError`): firmware 2.x, claim mode (claim the
  display or call `dev.security.use_without_key()` first), and an image for another target or chip.
- **Security**: an unclaimed display takes an update as it is. A claimed one is unlocked with a
  sealed ENTER_DFU on its session first, and again after the reboot, to read the image slots
  (the reboot locks SMP again); `dev.firmware.enable_ota()` does only that step.
- **Confirmation**: after the reboot, `update()` reconnects and sends one command, which makes the
  new image permanent on ESP32 and nRF. Until then, a reset returns the device to its old image.
- **Time**: an ESP32-S3 image (1.2 MB) takes about 47 s from a Mac, and the reboot and redraw
  that follow take up to 3 minutes. `update()` waits for both. macOS works for SMP updates.
- **Version shown**: ESP32 3.x builds show 3.0.0 on the boot screen and in
  `read_firmware_version()` whatever they run (Firmware_Unified#103). `update()` reads the version
  over SMP, which is correct.
- **Home Assistant** passes `find_device=` (a coroutine giving the `BLEDevice` for an address),
  so the update works through an ESPHome Bluetooth proxy.
- **A stalled or dropped transfer** goes on over a new link at 8 writes per request (what an
  ESPHome proxy's queue carries), up to three times, stopping when a link gains nothing. From 3.1.1
  an ESP32 keeps the upload for 120 s after a drop, so each new link resumes where the last one
  stopped; firmware that dropped it refuses the resume, and the upload starts over from 0.
- **Which image**: from 3.1 on a device reports its release board in READ_IDENT
  (`dev.profile.ident.board`, for example `s3-n16r8`; several boards share a chip), and
  `ReleaseManifest.ble_ota_file(board)` names its image. Older firmware reports none (`None`).
  `opendisplay update` without `--file` does this itself: it reads the board, downloads the image
  from the latest release (`--release TAG` for another) and checks it against the manifest.
  `--board NAME` stands in for a device that does not report its board.

### Setting up a display (setup and upgrade)

`opendisplay setup` takes a display from wherever it stands to set up, one step at a time:

- **No config, or a config that needs migration:** configure the panel.
- **Check the panel:** draw a test pattern and ask whether it looks right. In setup mode, which
  refuses every draw, ask instead whether the display shows SETUP MODE and its ID.
- **Key:** claim it, or use it without a key.
- **A claimed display with a 2.x key:** rotate that key.

It prints a line as each step starts. When it wrote the config itself, it always checks the panel
afterwards, with a key or without.

`opendisplay upgrade` is the same flow for a 2.x display:

1. **Back up** the config to `--backup` (default `opendisplay-backup-<device>.json`). The backup
   holds the key, so it is written with mode 0600 and never over an earlier file.
2. **Install 3.0.** A BG22 is installed over Bluetooth through its AppLoader, after you type
   `yes`, since it has no old image to return to. A board that needs USB pauses: the command names
   the files and the website flasher and saves its state to `--state-file`.
3. **Go on.** Once the display runs 3.0, run the same command again. It goes on from the state
   file, restores the config from the backup, then sets the display up.

```bash
opendisplay upgrade --device OD5A2F4C --no-key --state-file en05.json
# ... install over USB as it says, then (the display may advertise under a new name):
opendisplay upgrade --device ODD2292B --no-key --state-file en05.json
```

`--claim` / `--no-key` decide the key, otherwise it asks. `--board` names the board when several
share a chip (ESP32-S3), `--config FILE` gives a config when the display has none and there is no
backup, `--release TAG` picks a Firmware_Unified release (default: the latest; files are checked
against its manifest and cached), and `--yes-untested` writes drive data not yet seen on a panel
without asking.

In Python the flow is `opendisplay.setup`:

- `device_state()` and `plan_setup()` say where a device stands and which steps it needs.
- `SetupRun` performs those steps. `run_until_input(open_device)` returns the next request
  (`NeedClaimCode`, `NeedConfirmation`, `NeedWiredInstall`, ...), or `None` once the display is
  set up or the user declined a confirmation. `answer(request, value)` answers it.
- `on_step`, if set, is called with each step just before it runs.
- `to_json()` / `from_json()` save and restore a run across a restart; the saved run never holds a
  secret. A resumed run reads the device again, so it never repeats a step the device shows as done.

### Encryption (firmware 2.x)

Firmware 2.x devices with encryption enabled require authentication before accepting any commands (except `read_firmware_version`). For firmware 3.0, see "Firmware 3.0 security" above. Pass the 16-byte AES-128 master key to the constructor — authentication and session setup happen automatically before the first interrogation.

```python
from opendisplay import OpenDisplayDevice

key = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", encryption_key=key) as device:
    await device.upload_image(image)
```

All commands are transparently encrypted after authentication. Devices without encryption enabled work exactly as before — the `encryption_key` parameter is ignored if the device does not require it.

#### Getting the key

The encryption key is set when configuring the device via the [Open Display Config Builder](https://opendisplay.org/firmware/config/) web tool. It can be read from the device config once authenticated:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", encryption_key=key) as device:
    sc = device.config.security_config
    print(sc.encryption_key.hex())         # 32-char hex string
    print(sc.encryption_enabled_flag)      # True
    print(sc.rewrite_allowed)              # True if WRITE_CONFIG works without auth
    print(sc.session_timeout_seconds)      # How long before re-authentication is needed
```

#### Error handling

```python
from opendisplay import AuthenticationRequiredError, AuthenticationFailedError

try:
    async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
        pass
except AuthenticationRequiredError:
    # Device has encryption enabled but no key was provided
    # (or the session expired and re-authentication is needed)
    print("This device requires an encryption key")
except AuthenticationFailedError:
    # A key was provided but the device rejected it (wrong key or rate-limited)
    print("Wrong encryption key")
```

Both exceptions are subclasses of `AuthenticationError`, which can be used as a catch-all when the distinction doesn't matter.

### Rebooting the Device

Remotely reboot the device (useful after configuration changes or troubleshooting):

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    await device.reboot()
    # Device will reset after ~100ms
    # BLE connection will drop (this is expected)
```

**Note:** The device performs an immediate system reset and does not send an ACK response. The BLE connection will be terminated when the device resets. Wait a few seconds before attempting to reconnect.

### LED Activation (Firmware 1.0+)

Trigger the firmware LED flash routine (`0x0073`):

```python
from opendisplay import LedFlashConfig, OpenDisplayDevice

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Provide a typed flash pattern for this activation
    flash_config = LedFlashConfig.single(
        color=0xE0,            # RGB packed color byte used by firmware
        flash_count=2,         # Pulses per loop (0-15)
        loop_delay_units=2,    # 100ms units (0-15)
        inter_delay_units=5,   # 100ms units (0-255)
        brightness=8,          # 1-16
        group_repeats=1,       # 1-255, or None for infinite
    )
    await device.activate_led(led_instance=0, flash_config=flash_config, timeout=30.0)
```

`activate_led()` waits for the firmware response after the LED routine finishes. If firmware returns an LED-specific error response (`0xFF73`), the method raises `ProtocolError`.
It validates firmware version first and raises on versions below `1.0` where command `0x0073` is not supported.

### Configuration Inspection

Access detailed device configuration:

```python
from opendisplay import BoardManufacturer

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Board manufacturer (requires config from interrogation or config=...)
    manufacturer = device.get_board_manufacturer()
    if isinstance(manufacturer, BoardManufacturer):
        print(f"Manufacturer: {manufacturer.name}")
    else:
        print(f"Manufacturer ID (unknown): {manufacturer}")
    mfg = device.config.manufacturer
    print(f"Manufacturer slug: {mfg.manufacturer_name or f'unknown({mfg.manufacturer_id})'}")
    print(f"Board model: {mfg.board_type_name or f'unknown({mfg.board_type})'}")
    print(f"Board revision: {mfg.board_revision}")

    # Display configuration
    display = device.config.displays[0]
    print(f"Panel IC: {display.panel_ic_type}")
    print(f"Rotation: {display.rotation}")
    print(f"Diagonal: {display.screen_diagonal_inches:.1f}\"" if display.screen_diagonal_inches is not None else "Diagonal: unknown")
    print(f"Supports ZIP: {display.supports_zip}")
    print(f"Supports Direct Write: {display.supports_direct_write}")

    # System configuration
    system = device.config.system
    print(f"IC Type: {system.ic_type_enum.name}")
    print(f"Has external power pin: {system.has_pwr_pin}")

    # Power configuration
    power = device.config.power
    print(f"Battery: {power.battery_mah}mAh")
    print(f"Power mode: {power.power_mode_enum.name}")

    # Optional WiFi configuration (firmware packet 0x26)
    wifi = device.config.wifi_config
    if wifi is not None:
        print(f"WiFi SSID: {wifi.ssid_text}")
        print(f"WiFi encryption: {wifi.encryption_type}")
        print(f"WiFi server: {wifi.server_url_text}:{wifi.server_port}")
```
### Advertisement Parsing

Parse real-time sensor data from BLE advertisements:

```python
from opendisplay import parse_advertisement

# Parse manufacturer data from BLE advertisement
adv_data = parse_advertisement(manufacturer_data)
print(f"Battery: {adv_data.battery_mv}mV")
print(f"Temperature: {adv_data.temperature_c}°C")
print(f"Loop counter: {adv_data.loop_counter}")
print(f"Format: {adv_data.format_version}")  # "legacy" or "v1"

if adv_data.format_version == "v1":
    print(f"Reboot flag: {adv_data.reboot_flag}")
    print(f"Connection requested: {adv_data.connection_requested}")
    print(f"Dynamic bytes: {adv_data.dynamic_data.hex()}")
    print(f"Button byte 0 pressed: {adv_data.is_pressed(0)}")
```

`parse_advertisement()` auto-detects both firmware formats without connecting:
- Legacy payload: 11 bytes (`battery_mv`, signed `temperature_c`, `loop_counter`)
- v1 payload: 14 bytes (firmware 1.0+, encoded temperature/battery + status flags)

It also accepts payloads where the manufacturer ID (`0x2446`) is still prefixed.

Track button up/down transitions across packets with `AdvertisementTracker`:

```python
from opendisplay import AdvertisementTracker, parse_advertisement

tracker = AdvertisementTracker()

adv = parse_advertisement(manufacturer_data)
for event in tracker.update(address, adv):
    print(event.event_type, event.button_id, event.pressed, event.press_count)
```

#### Live Listener Script

Use the included script to scan and print parsed advertisement data live,
including v1 button transition events (`button_down`, `button_up`, `press_count_changed`):

```bash
uv run python examples/listen_advertisements.py --duration 60 --all
```

### Device Discovery

List all nearby OpenDisplay devices:
```python
from opendisplay import discover_devices

# Scan for 10 seconds
devices = await discover_devices(timeout=10.0)

for name, mac in devices.items():
    print(f"{name}: {mac}")

# Output:
# OpenDisplayA123: AA:BB:CC:DD:EE:FF
# OpenDisplayB456: 11:22:33:44:55:66
```

## Connection Reliability

py-opendisplay uses `bleak-retry-connector` for robust BLE connections with:
- Automatic retry logic with exponential backoff
- Connection slot management for ESP32 Bluetooth proxies
- GATT service caching for faster reconnections
- Better error categorization

### Displays that are redrawing

While a display redraws its panel it goes silent: no advertisements, and commands are held until
the refresh ends. A colour (Spectra) panel takes about 30 s. This is the firmware's design, so the
library waits for it:

- After the library itself makes the panel redraw (a config write, `reboot()`, the end of a claim,
  a key rotation while the key is shown on screen), the next replies, scans and connects to that
  display wait up to 60 s for it, in the same process. A command is never sent twice: the firmware
  answers it after the refresh. Waiting ends as soon as the display answers.
- Otherwise a silent display fails as fast as before, because "redrawing" and "gone" look the same.
  The error then ends with *"If the display is redrawing (some colour panels take about 30 s after a
  power-up, reboot or config change), try again in 30 s."*
- A firmware 3.0 handshake that the display answers with BUSY is retried every 10 s, which stays
  under the display's limit on handshake attempts. RATE_LIMITED is never retried.
- The CLI always allows for a redraw, because you have often just switched the display on. It
  prints `Waiting for the display to finish redrawing...` once when it waits. `--no-wait` gives the
  fast failure instead.

### Home Assistant Integration

When using py-opendisplay in Home Assistant custom integrations, pass the `BLEDevice` object for optimal performance:

```python
from homeassistant.components import bluetooth
from opendisplay import OpenDisplayDevice

# Get BLEDevice from Home Assistant
ble_device = bluetooth.async_ble_device_from_address(hass, mac_address)

async with OpenDisplayDevice(mac_address=mac_address, ble_device=ble_device) as device:
    await device.upload_image(image)
```

### Retry Configuration

Configure retry behavior for unreliable environments:

```python
# Increase retry attempts for poor BLE conditions
async with OpenDisplayDevice(
    mac_address="AA:BB:CC:DD:EE:FF",
    max_attempts=6,  # Try up to 6 times (default: 4)
) as device:
    await device.upload_image(image)

# Disable service caching after firmware updates
async with OpenDisplayDevice(
    mac_address="AA:BB:CC:DD:EE:FF",
    use_services_cache=False,  # Force fresh service discovery
) as device:
    await device.upload_image(image)
```



## Firmware Updates (OTA)

py-opendisplay supports over-the-air firmware updates for nRF52 and EFR32BG22 devices via BLE. ESP32 variants do not have a BLE OTA path.

> **macOS not supported for OTA.** CoreBluetooth caches GATT services and provides no API to invalidate the cache. After the device reboots into bootloader/AppLoader mode its GATT service table changes, but macOS continues to serve the stale cached services — making the OTA service invisible to bleak. OTA works on Linux (BlueZ) and is the intended path via Home Assistant running on a Linux host.

### Installation

Install the optional OTA dependencies for your device:

```bash
# nRF52840 / nRF52811
pip install "py-opendisplay[nrf-ota]"

# EFR32BG22
pip install "py-opendisplay[silabs-ota]"

# Both
pip install "py-opendisplay[all-ota]"
```

### nRF52840 / nRF52811 — Nordic Legacy DFU

nRF devices use the Nordic Legacy DFU protocol. After triggering the bootloader, the device reboots and advertises a new DFU service (often at MAC+1). `find_nrf_dfu_device()` polls for up to 30 s to locate it.

```python
from opendisplay import OpenDisplayDevice
from opendisplay.ota import find_nrf_dfu_device, perform_nrf_dfu

# Load the .zip firmware archive
with open("NRF52840.zip", "rb") as f:
    firmware_bytes = f.read()

# Step 1: trigger the bootloader
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    await device.trigger_dfu_bootloader()

# Step 2: wait for the DFU device to appear
dfu_device = await find_nrf_dfu_device("AA:BB:CC:DD:EE:FF")
if dfu_device is None:
    raise RuntimeError("DFU device not found after 30 s")

# Step 3: flash
await perform_nrf_dfu(
    firmware_bytes,
    dfu_device,
    on_progress=lambda pct: print(f"{pct:.0f}%"),
    on_log=print,
)
```

Requires the [`nrf-ota`](https://github.com/OpenDisplay-org/nrf-ota) package (`pip install nrf-ota`).

### EFR32BG22 — Silicon Labs AppLoader OTA

EFR32BG22 devices use the Silicon Labs AppLoader protocol. After the bootloader trigger, the device reboots into AppLoader mode at the **same BLE address**. `perform_silabs_ota()` waits for AppLoader to be ready automatically.

```python
from opendisplay import OpenDisplayDevice
from opendisplay.ota import perform_silabs_ota

# Load the .gbl firmware file
with open("opendisplay-bg22-v1.2.gbl", "rb") as f:
    firmware_bytes = f.read()

# Step 1: trigger the bootloader
ble_device = ...  # BLEDevice from BleakScanner or HA
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", ble_device=ble_device) as device:
    ota_device = ...  # same BLEDevice, or re-scan after disconnect
    await device.trigger_dfu_bootloader()

# Step 2: flash (perform_silabs_ota waits for AppLoader internally)
await perform_silabs_ota(
    firmware_bytes,
    ota_device,
    on_progress=lambda pct: print(f"{pct:.0f}%"),
    on_log=print,
)
```

The `on_progress` callback receives a float from 0–100. Both functions raise `OTAError` on failure.

### Determining IC type

Use `device.config.system.ic_type` (an `ICType` enum value) to select the right OTA path:

```python
from opendisplay.models.enums import ICType

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    ic = device.config.system.ic_type
    if ic in (ICType.NRF52840, ICType.NRF52811):
        # use Nordic DFU path
    elif ic == ICType.EFR32BG22:
        # use Silabs AppLoader path
    else:
        # ESP32 — no BLE OTA available
```

## Development

```bash
uv sync --all-extras
uv run pytest
```
