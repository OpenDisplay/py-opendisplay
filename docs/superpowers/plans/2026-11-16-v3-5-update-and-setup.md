# Piece 5: Firmware Updater and the Setup / Upgrade Flow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `EXECUTOR_RULES.md` in this folder first.

**Goal:** Update a firmware 3.x device over Bluetooth with one call, and guide a device from any starting state (2.x, fresh 3.0, needs migration, unclaimed) to a working, optionally claimed 3.0 device, with Home Assistant and the CLI calling the same code.

**Architecture:** Two separate things. The **updater** picks a transfer method from the device's firmware target and runs the same six steps around it. The **setup flow** is a pure planner (a port of the website's `od-wizard.js`) plus a runner that executes steps and pauses with a typed request whenever a person is needed; its state serialises to JSON so it survives a reboot and a Home Assistant restart.

**Tech Stack:** Python 3.13; `smpclient` (PyPI, 7.4.0 at the time of writing) as a new optional extra for mcumgr SMP; the existing `nrf-ota` and `silabs-ble-ota` extras; `uv`, pytest, ruff, mypy strict, pylint, prek.

**Spec:** `../specs/2026-11-15-v3-architecture-design.md` (sections "Setup and upgrade flow" and "Firmware updater"). Firmware: `docs/UPGRADING.md`, `docs/RELEASE_ARTEFACTS.md`, the release `manifest.json`, `docs/SECURITY_V2.md` § 6.1 (who may update when) and § 15 (the setup flow). Reference: `Web` `origin/v3/website` `httpdocs/js/od-wizard.js`.

**Depends on:** pieces 2b, 3a and 4 merged.

## Read this first

- **An update can strand a device.** The firmware's own rule (UPGRADING.md): never send an image to a device unless that image's no-touch recovery is proven on that target, and never to an EFR32BG22 that failed its RAM gate. BG22 has no rollback slot and the tags in the inventory have no USB port. Every hardware update in this plan needs the user's explicit go-ahead, and on BG22 the user must have a debugger at hand or accept the risk in words.
- **After an update the host must connect once and send any command.** On ESP32 and nRF that is what confirms the new image; without it the device returns to the old image at the next reset.
- **Images are unsigned.** The protection is that updates must be enabled first; see Task 2.
- Much of what the firmware documents here is marked UNTESTED. Expect surprises and record them.

## Decisions to confirm with the user

1. **Use `smpclient` for SMP, as a new extra `smp`.** Check first that its `bleak` requirement (`>=3.0.2,<4` in 7.4.0) is compatible with this project's and with what Home Assistant ships; if not, the fallback is a minimal own client for image upload, test and confirm (three SMP commands over one GATT characteristic). Do this check before writing any code and report the result.
2. **Wired first installs are not done by the library in this piece.** The flow stops with a request that names the file and the website flasher, and resumes when the device reappears on 3.0. A CLI `--usb` path with `esptool` is a later, separate change.
3. **Where release files come from:** GitHub releases of `OpenDisplay/Firmware_Unified`, selected through the release's `manifest.json` (`boards[].flows`), cached under the user's cache directory. Home Assistant may pass its own downloader.
4. **macOS.** The library documents that macOS cannot do OTA because of the CoreBluetooth GATT cache. SMP runs inside the application, without a separate bootloader device, so this may not apply; the firmware docs list SMP as untested from non-Linux hosts. Find out on hardware (Task 2) and update the documentation with what was seen.

## Global Constraints

- Everything in `EXECUTOR_RULES.md`.
- `opendisplay.ota` and `opendisplay.models.firmware` stay importable with their current names (Home Assistant imports them); new code lives in `opendisplay/firmware/` and the old modules re-export.
- The planner performs no I/O. The runner performs I/O only through `Device`.
- No step deletes a config backup. A backup contains the key; the flow never writes it anywhere the caller did not choose.

## Review Focus

1. **Power loss or disconnect in the middle of a transfer.** The updater reports "not updated", leaves the device on its old image, and the plan can be resumed from the transfer step.
2. **The device comes back on the old version after an update** (rolled back). Reported as its own outcome, not as success and not as a connection error.
3. **A BG22 that reappears advertising as `OTA`** (its AppLoader). Reported as "waiting for an image" with the next action.
4. **A plan resumed after a Home Assistant restart** with a device that changed state meanwhile (already updated, already claimed). The runner re-reads the device and skips steps that are done; it never repeats a transfer because its saved state said "pending".
5. **A device in claim mode when an update is requested.** Updates are refused there; the flow leaves claim mode first (or claims), and says which it did.
6. **A user who declines a confirmation** (untested panel, erase of config). The plan stops cleanly and can be resumed later.

---

## Part 5a: the updater

### File Structure

| File | Responsibility |
|---|---|
| `src/opendisplay/firmware/__init__.py` | exports; re-exports of today's `ota.py` and `models/firmware.py` names |
| `src/opendisplay/firmware/releases.py` | parse `manifest.json`, pick the files for a board and a flow |
| `src/opendisplay/firmware/smp.py` | SMP upload, test, confirm (through `smpclient`) |
| `src/opendisplay/firmware/updater.py` | `update()`: the six steps, method dispatch, outcomes |
| `tests/unit/firmware/` | tests; `tests/fixtures/firmware/manifest-3.0.2.json` vendored from the 3.0.2 release |

### Task 1: Release manifest

```python
@dataclass(frozen=True)
class BoardRelease:
    board: str; target: str; chip: str
    ble_ota_files: tuple[str, ...]; ble_ota_protocol: str | None        # "smp", "silabs-apploader", ...
    usb_install_files: tuple[str, ...]; usb_install_erases_config: bool
class ReleaseManifest:
    version: str; commit: str; signing: Mapping[str, str]
    @classmethod
    def parse(cls, data: bytes) -> ReleaseManifest
    def board(self, name: str) -> BoardRelease | None
    def boards_for_chip(self, chip: str) -> list[BoardRelease]
def can_roll_back(target: FirmwareTarget | int | None) -> bool          # True: ESP32, nRF52840, nRF54. False: BG22, nRF52811, Telink
```

Tests against the vendored 3.0.2 manifest: the `s3-n32r8-extuart` board lists its merged image for USB install with `erases_config` true; the `bg22` board's Bluetooth flow is the AppLoader with `erases_config` false; an unknown board is `None`. **Open point to solve here:** nothing maps a device to its board name (the E1002 needed flash size, PSRAM and console type to pick `s3-n32r8-extuart`). Read `docs/RELEASE_ARTEFACTS.md` and the website toolbox for how they choose; if the answer is "the user picks", `update()` takes the board name as an argument and the flow asks for it.

### Task 2: SMP client and the hardware question

Wrap `smpclient` behind `async def smp_upload(ble_device, image: bytes, *, on_progress) -> None`, `smp_test_and_reset(...)`, `smp_confirm(...)`. Unit-test the wrapper with a fake client. Then, **with the user's go-ahead and on the E1002 only** (ESP32-S3 on 3.0.2, USB recovery available): upload the same 3.0.2 `-app.bin` it already runs. Record: does it work from macOS; how long it takes; what the device does when the link drops mid-transfer. This is the answer to Decision 4.

### Task 3: `update()`

```python
class UpdateOutcome(Enum): UPDATED; ROLLED_BACK; WAITING_IN_BOOTLOADER; NOT_UPDATED
@dataclass(frozen=True)
class UpdateResult: outcome: UpdateOutcome; version_before: str; version_after: str | None; detail: str
async def update(device: Device, image: bytes, *, method: str | None = None,
                 on_progress: Callable[[int, int], None] | None = None) -> UpdateResult
```

The six steps, whatever the method:
1. Refuse early when the device cannot take it: claim mode (say so), a 2.x device (this is the first-install flow, Part 5b), an image that is not for this target.
2. Enable the update by security state (SECURITY_V2 § 6.1): unclaimed in normal mode needs nothing, except BG22, which needs a plaintext ENTER_DFU to reboot into the AppLoader; claimed needs a sealed ENTER_DFU first.
3. Transfer: SMP for ESP32 / nRF52840 / nRF54, `silabs-ble-ota` for BG22, `nrf-ota` for nRF52811 (check first whether it speaks Secure DFU; the extra is documented as Legacy DFU).
4. Wait for the reboot and reconnect; Spectra boards need about 30 seconds before they advertise.
5. Send READ_IDENT. This confirms the image on ESP32 and nRF.
6. Read the version and decide the outcome. Note the finding from piece 2a: a 3.0.2 release image reported 3.0.0 with a zero hash, so compare what the device reports before and after, and do not rely on the patch byte alone.

Add `Device.firmware.update(...)`, `Device.firmware.can_roll_back`, `Device.firmware.enable_ota()`. Tests drive `update()` with fake transfer functions and a scripted device for each outcome, including Review Focus 1–3 and 5.

## Part 5b: the setup and upgrade flow

### File Structure

| File | Responsibility |
|---|---|
| `src/opendisplay/setup/state.py` | `device_state()`: classify a device from advertisement, identity, SEC_STATUS, firmware version |
| `src/opendisplay/setup/planner.py` | `plan_setup()`: ordered steps. Port of `planWizard` and `planMigrationAssistant` |
| `src/opendisplay/setup/requests.py` | the typed requests a runner can pause on |
| `src/opendisplay/setup/runner.py` | `SetupRun`: execute, pause, resume, serialise |
| `src/opendisplay/cli_setup.py` | `opendisplay setup` and `opendisplay upgrade` |

### Task 4: State and planner (pure)

```python
class DeviceState(Enum): LEGACY; VIRGIN; FRESH; GESTURE; OPEN; CLAIMED; CLAIMED_IN_CLAIM_MODE; LOCKED; UNKNOWN
def device_state(*, firmware, ident, sec_status, advertisement) -> DeviceState
class Step(Enum): BACK_UP; INSTALL_3_0; CONFIRM_IMAGE; CONFIGURE_DISPLAY; VERIFY_PANEL; CLAIM; ROTATE_KEY; SECRETS; DONE
@dataclass(frozen=True)
class SetupPlan: state: DeviceState; steps: tuple[Step, ...]; notes: tuple[str, ...]
def plan_setup(*, state, profile, sec_status, want_key: bool | None, rotate_legacy_key: bool = True) -> SetupPlan
```

Port `deviceState`, `planWizard` and `planMigrationAssistant` from `od-wizard.js` branch by branch; port its test file `tests/od-wizard.test.js` case by case as the tests. Two rules from our own hardware work that the tests must pin: a device with **no config** gets `CONFIGURE_DISPLAY` before `CLAIM` (it cannot show the code), and "no config" and "needs migration" are different inputs that both lead to `CONFIGURE_DISPLAY`. A 2.x device (`LEGACY`) gets `BACK_UP`, `INSTALL_3_0`, `CONFIRM_IMAGE`, then the 3.0 steps.

### Task 5: Requests and runner

```python
class NeedClaimCode; NeedPanelChoice(candidates); NeedConfirmation(reason, detail); NeedGesture(kind)   # two power cycles, five power cycles
class NeedWiredInstall(file_name, url, erases_config); NeedBoardChoice(candidates)
class SetupRun:
    @classmethod
    def start(cls, plan: SetupPlan, *, backup_sink: Callable[[bytes], None]) -> SetupRun
    @classmethod
    def from_json(cls, data: str) -> SetupRun
    def to_json(self) -> str                        # never contains the key in clear unless the caller's sink does
    async def run_until_input(self, device_factory) -> Request | None     # None: finished
    def answer(self, request, value) -> None
```

The runner re-reads the device at the start of every `run_until_input` and re-plans, so a saved run never repeats a finished step (Review Focus 4). Steps map to code that exists by then: `BACK_UP` → `Device.config.read()` to the sink; `INSTALL_3_0` → `update()` where a Bluetooth path exists (BG22, nRF54, nRF52811), else `NeedWiredInstall`; `CONFIGURE_DISPLAY` → `plan_migration` + write (piece 3a); `VERIFY_PANEL` → upload a test pattern and ask the user what they see; `CLAIM` → `ClaimFlow` (piece 4); `ROTATE_KEY` → `rotate_key`.

Tests: one scripted run per starting state, each including a save and restore through JSON in the middle, and the declined-confirmation case.

### Task 6: CLI

`opendisplay setup` runs the flow interactively, mapping each request to a prompt. `opendisplay upgrade` is the same flow starting from a 2.x device, with `--state-file` so it can be resumed after the wired install. `opendisplay update --file IMAGE` calls `update()` directly on a 3.x device and prints, before the transfer, whether this device can undo a bad image by itself.

### Task 7: Hardware — each update needs the user's go-ahead

In this order, stopping at the first surprise:
1. E1002 (3.0.2): `opendisplay setup` from "no config, unclaimed" to configured and, if the user wants, claimed.
2. E1002: `opendisplay update` to a newer 3.x test release when one exists.
3. nRF52840 kit (2.27.0): `opendisplay upgrade` up to the wired-install request; the user does the two serial DFU steps; resume. The firmware docs call the way back to 2.x on this board untested, so ask before starting.
4. EFR32BG22: only if the user says so, knowing there is no USB and no debugger-free recovery.

## Not in this piece

USB flashing from the library; signed images; the Telink updater.
