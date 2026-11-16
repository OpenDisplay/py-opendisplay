# Device Seams (piece 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `src/opendisplay/device.py` (3,021 lines) into the package `src/opendisplay/device/` with the secure channel and the upload paths as separate units, without changing the public API or a single byte on the wire.

**Architecture:** Traffic flows `OpenDisplayDevice` / `Uploader` -> `SecureChannel` -> `Transport`. `SecureChannel` is one object per connection that holds an optional `Session`; no session means plaintext, `LegacySession` is the 2.x scheme, and a SEC2 session plugs into the same slot in piece 4. Code is moved, not rewritten.

**Tech Stack:** Python 3.13, `uv`, pytest (asyncio_mode=auto), ruff, mypy strict, pylint, prek.

**Spec:** `docs/superpowers/specs/2026-11-15-v3-architecture-design.md` (section "Piece 1: seams in `device.py`").

## Global Constraints

- Base: `main` at `8184dba` (= `origin/main`, 1,208 tests passing). Work on branch `refactor/device-seams` in the main checkout. No worktrees.
- Everything importable from `opendisplay` and `opendisplay.device` today stays importable, as do the submodule paths the two HA integrations use (Task 1 lists them).
- Public method names, signatures and exceptions of `OpenDisplayDevice` do not change. Its public class constants (`TIMEOUT_*`, `MAX_STRAY_*`) keep their names and values.
- Bytes on the wire and their order do not change, for plain and encrypted sessions.
- Move code verbatim. A body changes only where it reads state that now lives on another object. No behaviour fixes, no renames of public names, no new features.
- Tests move with the code: patch strings and private attribute access are updated to the new location. No delegating private attribute or property is added to the facade to keep an old test line working.
- Pylint limits in `pyproject.toml` are not raised (`max-module-lines = 1500`, `max-public-methods = 21`, `max-attributes = 22`). Existing inline disables move with the code they belong to.
- `uv` only. Stage files by name, never `git add -A` or `git add .`. Commits use Conventional Commits (`refactor:` / `test:`). Do not touch `version` in `pyproject.toml`.
- After every task: `uv run pytest -q` passes and `uv run prek run --all-files` is clean.

## Review Focus

1. **Command before connecting.** Calling a command on a device that was never connected must raise `RuntimeError("Device not connected")`, the same text as today, not an `AttributeError` from a missing channel. Test in Task 4.
2. **Link drop before or without a channel.** `BLEConnection` gets `disconnected_callback=self._on_disconnect` before the channel exists; the callback firing early, or twice, must not raise. Test in Task 4.
3. **Device object reused for a second connection.** After a disconnect and a new connect, the session is gone and pipe probe state is fresh, so the second connection re-authenticates and re-probes. Test in Task 5.
4. **`authenticate()` called by hand on a device built without a key** (explicit `transport=`, as HA and tests do). The session lands on the existing channel and the next direct write uses the encrypted chunk size 154. Test in Task 4.
5. **Re-authentication at 90 % of the session timeout.** It happens before a normal write and never from `write_stream_frame`, and the nonce counter restarts at 0. Test in Task 4.

---

## File Structure

| File | Responsibility | Source in today's `device.py` |
|---|---|---|
| `src/opendisplay/device/__init__.py` | Re-exports `OpenDisplayDevice`, `prepare_image`, `preview_image` | new |
| `src/opendisplay/device/imaging.py` | Image preparation, no I/O | lines 186-447: `_capabilities_rotation`, `_rotate_source_image`, `_capabilities_from_config`, `_warn_firmware_upload_limitations`, `_resolve_dbs`, `prepare_image`, `preview_image` |
| `src/opendisplay/device/channel.py` | `Session` protocol, `LegacySession`, `SecureChannel` | lines 783-971 minus `_on_disconnect`: `_clear_session`, `_encrypt_frame`, `_write`, `_write_pipe_frame`, `_reauthenticate_if_needed`, `_read`, `authenticate`, `_read_auth_step2`; plus the session branches of `_direct_write_chunk_size` and `_pipe_data_size` |
| `src/opendisplay/device/upload.py` | `Uploader`, `UploadTarget`, pipe exceptions | lines 161-184 (`_PipePartialEtagMismatch`, `_PipePartialRejected`), 991-1025 (`_display_config`, `_supports_compression`, `_pipe_write_allowed`), 2011-3001 (`_upload_prepared` to `_await_pipe_end_ack`) |
| `src/opendisplay/device/facade.py` | `OpenDisplayDevice`: construction, connect, interrogate, simple commands, config, the command lock | everything else |

Test files created: `tests/unit/test_public_api_snapshot.py`, `tests/unit/data/public_api.json`, `tests/unit/test_device_channel.py`, `tests/unit/test_device_seams.py`.

---

### Task 1: Pin the public API and take the hardware baseline

**Files:**
- Create: `tests/unit/test_public_api_snapshot.py`
- Create: `tests/unit/data/public_api.json` (generated)
- Create: `scripts/smoke_seams.py` (the `scripts/` directory is untracked on purpose; do not commit it)

**Interfaces:**
- Produces: the snapshot test every later task must keep green; `scripts/smoke_seams.py --device NAME [--key HEX] [--partial]` for Task 7.

- [ ] **Step 1: Create the branch**

```bash
git switch -c refactor/device-seams
```

- [ ] **Step 2: Write the snapshot test**

`tests/unit/test_public_api_snapshot.py` holds three tests.

`test_public_api_matches_snapshot` builds this dict and compares it with `tests/unit/data/public_api.json`; when the environment variable `UPDATE_API_SNAPSHOT=1` is set it writes the file instead of comparing:

```python
{
    "opendisplay.__all__": sorted(opendisplay.__all__),
    "opendisplay.device": sorted(n for n in vars(opendisplay.device) if not n.startswith("_")
                                 and n in {"OpenDisplayDevice", "prepare_image", "preview_image"}),
    "OpenDisplayDevice": {
        name: describe(member)   # for every name in vars(OpenDisplayDevice) not starting with "_", plus "__init__"
    },
}
```

`describe` returns `"property"` for a property, `str(inspect.signature(...))` for a function, staticmethod or classmethod (unwrap with `inspect.unwrap` so `@_serialized` does not hide the signature), and `repr(value)` for a class constant.

`test_home_assistant_imports` is parametrized over these (module, name) pairs and asserts `getattr(importlib.import_module(module), name)` succeeds:

```python
HA_IMPORTS = {
    "opendisplay": [
        "MANUFACTURER_ID", "AdvertisementTracker", "AuthenticationFailedError",
        "AuthenticationRequiredError", "BLEConnectionError", "BLETimeoutError",
        "BuzzerActivateConfig", "ColorScheme", "DitherMode", "FitMode", "GlobalConfig",
        "LedFlashConfig", "LedFlashStep", "NfcNotSupportedError", "NfcRecordType",
        "NfcWriteError", "OpenDisplayConnectionError", "OpenDisplayDevice",
        "OpenDisplayError", "OpenDisplayTimeoutError", "PartialState", "RefreshMode",
        "Rotation", "parse_advertisement", "prepare_image", "voltage_to_percent",
    ],
    "opendisplay.device": ["OpenDisplayDevice"],
    "opendisplay.exceptions": [
        "AuthenticationFailedError", "AuthenticationRequiredError", "BLEConnectionError", "OTAError",
    ],
    "opendisplay.models": ["FirmwareVersion"],
    "opendisplay.models.advertisement": [
        "AdvertisementData", "ButtonChangeEvent", "Sht40Reading", "TouchChangeEvent", "TouchTracker",
    ],
    "opendisplay.models.config": ["BinaryInputs", "SensorData"],
    "opendisplay.models.config_json": ["config_from_json", "config_to_json"],
    "opendisplay.models.enums": ["CapacityEstimator", "ICType", "PowerMode", "SensorType"],
    "opendisplay.models.firmware": ["firmware_ota_asset", "firmware_release_repo"],
    "opendisplay.ota": ["perform_silabs_ota"],
}
```

`test_device_module_is_importable_by_old_path` asserts `from opendisplay.device import OpenDisplayDevice, prepare_image, preview_image` works and that `opendisplay.OpenDisplayDevice is opendisplay.device.OpenDisplayDevice`.

- [ ] **Step 3: Generate the snapshot on unchanged code and run**

```bash
UPDATE_API_SNAPSHOT=1 uv run pytest tests/unit/test_public_api_snapshot.py -q
uv run pytest tests/unit/test_public_api_snapshot.py -q
```

Expected: second run passes. If a name in `HA_IMPORTS` does not exist on `main`, stop and report it; do not delete it from the list silently.

- [ ] **Step 4: Write `scripts/smoke_seams.py`**

Uses the public API only. Arguments: `--device NAME` (BLE name, passed as `device_name=`), `--key HEX` (optional, passed as `encryption_key=bytes.fromhex(...)`), `--partial`. It prints one `PASS <step>` or `FAIL <step>: <error>` line per step and exits non-zero on any failure. Steps, in one connection:

1. `read_firmware_version()`
2. `device.config` is not None and `device.width`, `device.height` are positive
3. `upload_image(img_a, refresh_mode=RefreshMode.FULL)` with `compress=True`
4. `upload_image(img_b, refresh_mode=RefreshMode.FULL, compress=False)`
5. with `--partial`: `state = PartialState()`; upload `img_a` with `state=state`, then `img_c` (= `img_a` with one 64x64 block inverted) with `state=state`

`img_a` is a white image with a black border and the text of the current git short hash; `img_b` is its inverse.

- [ ] **Step 5: Hardware baseline (needs the user and the devices)**

```bash
uv run python scripts/smoke_seams.py --device OD405BD8 --partial 2>&1 | tee scratch/smoke-before-e1003.txt
uv run python scripts/smoke_seams.py --device OD1F9C04 --key "$(awk -F, '$1=="OD1F9C04"{print $2}' device_inventory.csv)" 2>&1 | tee scratch/smoke-before-bg22.txt
uv run python scripts/smoke_seams.py --device OD4064C9 --key "$(awk -F, '$1=="OD4064C9"{print $2}' device_inventory.csv)" 2>&1 | tee scratch/smoke-before-e1002.txt
```

OD405BD8 is the reTerminal E1003 (no key, supports partial). OD1F9C04 is a Solum M3 (EFR32BG22, firmware 2.2.1, encrypted, no partial): it covers the encrypted session. OD4064C9 is the reTerminal E1002 (ESP32-S3, firmware 2.26.7, encrypted, six-colour Spectra, no partial): it covers the encrypted session on the ESP32 firmware line, including the encrypted streaming path that the BG22 firmware does not have. Expected: every line `PASS`. If a step fails on unchanged code, record it as a known baseline failure and carry on.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_public_api_snapshot.py tests/unit/data/public_api.json
git commit -m "test: snapshot the public API before the device split"
```

---

### Task 2: Turn the module into a package

**Files:**
- Move: `src/opendisplay/device.py` -> `src/opendisplay/device/facade.py`
- Create: `src/opendisplay/device/__init__.py`
- Modify: every test that patches or imports a private name through `opendisplay.device`

**Interfaces:**
- Produces: `opendisplay.device.facade` as the module that holds everything, for now.

- [ ] **Step 1: Move the file**

```bash
mkdir src/opendisplay/device && git mv src/opendisplay/device.py src/opendisplay/device/facade.py
```

In `facade.py` change every relative import by one level (`from .crypto` -> `from ..crypto`, `from .discovery` -> `from ..discovery`, and so on, including the lazy import inside `__aenter__`).

- [ ] **Step 2: Write `src/opendisplay/device/__init__.py`**

```python
"""OpenDisplayDevice and the image preparation helpers."""

from .facade import OpenDisplayDevice, prepare_image, preview_image

__all__ = ["OpenDisplayDevice", "prepare_image", "preview_image"]
```

- [ ] **Step 3: Run the suite and see the patch-string failures**

Run: `uv run pytest -q`
Expected: failures only in tests that patch `opendisplay.device.<name>` or import a private name from `opendisplay.device`.

- [ ] **Step 4: Point those tests at `opendisplay.device.facade`**

Replace in `tests/`: `"opendisplay.device.<name>"` patch strings, `from opendisplay.device import _<private>`, and `import opendisplay.device as device_module` / `device_mod` (becomes `import opendisplay.device.facade as ...`). Leave `from opendisplay.device import OpenDisplayDevice` and `from opendisplay.device import prepare_image` alone: those are the public path.

- [ ] **Step 5: Verify**

Run: `uv run pytest -q && uv run prek run --all-files`
Expected: all pass (the 1,208 from `main` plus the snapshot tests), lint clean.

- [ ] **Step 6: Commit**

```bash
git add src/opendisplay/device tests/unit
git commit -m "refactor: make opendisplay.device a package"
```

---

### Task 3: Extract `imaging.py`

**Files:**
- Create: `src/opendisplay/device/imaging.py`
- Modify: `src/opendisplay/device/facade.py`, `src/opendisplay/device/__init__.py`
- Modify: tests that patch `fit_image`, `dither_image`, `get_palette_for_display`, `encode_image` or import `_resolve_dbs`, `_rotate_source_image`, `_warn_firmware_upload_limitations`

**Interfaces:**
- Produces, all with today's signatures: `prepare_image`, `preview_image`, `_capabilities_from_config(config: GlobalConfig) -> DeviceCapabilities`, `_capabilities_rotation`, `_rotate_source_image`, `_warn_firmware_upload_limitations`, `_resolve_dbs`.

- [ ] **Step 1: Move the seven functions** listed under File Structure into `imaging.py` with the imports they need. `facade.py` imports back the ones it still calls (`prepare_image`, `_capabilities_from_config`, and whichever of the others its remaining methods use).

- [ ] **Step 2: `__init__.py` imports `prepare_image`, `preview_image` from `.imaging`** and `OpenDisplayDevice` from `.facade`.

- [ ] **Step 3: Update test patch targets.** Rule for this and every later task: a name is patched in the module where the moved code looks it up. Here: `fit_image`, `dither_image`, `get_palette_for_display` and the three private imports go to `opendisplay.device.imaging`; `encode_image` goes there only if its call site moved (check where it is called).

- [ ] **Step 4: Verify**

Run: `uv run pytest -q && uv run prek run --all-files`
Expected: all pass, lint clean. `tests/unit/test_wire_bytes_golden.py` passing is the proof that image bytes are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/opendisplay/device tests/unit
git commit -m "refactor: move image preparation into device/imaging.py"
```

---

### Task 4: Extract the secure channel

**Files:**
- Create: `src/opendisplay/device/channel.py`
- Create: `tests/unit/test_device_channel.py`
- Modify: `src/opendisplay/device/facade.py`
- Modify: tests that set `device._connection`, `device._session_key`, `device._session_id`, `device._nonce_counter`, `device._auth_time`, or call `device._read`, `device._write`, `device._clear_session`

**Interfaces:**
- Produces:

```python
# channel.py
MAX_STRAY_AUTH_FRAMES = 4
TIMEOUT_AUTH = 5.0                      # today's TIMEOUT_ACK as used by the handshake

class Session(Protocol):
    direct_chunk_size: int              # data bytes per 0x71 chunk inside this session
    established_at: float               # time.monotonic() of the handshake
    def seal(self, frame: bytes) -> bytes: ...          # plaintext cmd+payload -> wire frame; advances the counter
    def open(self, raw: bytes) -> bytes: ...            # wire frame -> plaintext cmd+payload; passes plaintext replies through
    def pipe_data_size(self, frame_eff: int) -> int: ...

@dataclass
class LegacySession:                    # implements Session
    session_key: bytes
    session_id: bytes
    nonce_counter: int = 0
    established_at: float = 0.0
    direct_chunk_size: int = ENCRYPTED_CHUNK_SIZE       # 154
    @classmethod
    async def establish(cls, transport: Transport, key: bytes) -> LegacySession: ...

class SecureChannel:
    def __init__(self, transport: Transport, *, session_timeout: Callable[[], int] = lambda: 0) -> None: ...
    transport: Transport
    session: Session | None             # None = plaintext
    async def authenticate(self, key: bytes) -> None: ...        # LegacySession.establish; remembers key for re-auth
    async def write(self, frame: bytes, response: bool = True) -> None: ...
    async def write_stream_frame(self, frame: bytes, *, response: bool) -> None: ...
    async def read(self, timeout: float) -> bytes: ...
    def direct_chunk_size(self) -> int: ...
    def pipe_data_size(self, frame_eff: int) -> int: ...
    def reset(self) -> None: ...        # drop the session, keep the transport
```

```python
# facade.py
def _attach(self, transport: Transport) -> None    # sets self._connection and self._channel
@property
def _channel(self) -> SecureChannel                 # raises RuntimeError("Device not connected") when not attached
```

Where each piece of today's code goes:

| Today | After |
|---|---|
| `authenticate` body, `_read_auth_step2` | `LegacySession.establish` (reads and writes go straight to `transport`, as today) |
| `_encrypt_frame` | `LegacySession.seal` |
| the decrypt branch of `_read` (frames of 31 bytes or more) | `LegacySession.open` |
| the rest of `_read` (3-byte `FE` and `FF` frames) | `SecureChannel.read`, after `open` |
| `_write`, `_reauthenticate_if_needed` | `SecureChannel.write`; the timeout comes from `session_timeout()`, 0 meaning never |
| `_write_pipe_frame` | `SecureChannel.write_stream_frame` |
| `_clear_session` | `SecureChannel.reset` |
| session branch of `_direct_write_chunk_size` | `SecureChannel.direct_chunk_size`: session's value if a session is active, else the transport rule (`max_frame - 2` above `DEFAULT_MAX_FRAME`, else `CHUNK_SIZE`) |
| session branch of `_pipe_data_size` | `SecureChannel.pipe_data_size`: `frame_eff - 31 - 1` in a legacy session, else `frame_eff - PIPE_FRAME_OVERHEAD` |
| the two `ENCRYPTED_CHUNK_SIZE if self._session_key is not None else MAX_START_PAYLOAD` expressions | `ENCRYPTED_CHUNK_SIZE if self._channel.session is not None else MAX_START_PAYLOAD` (unchanged in meaning; they move to the uploader in Task 5) |

The facade keeps `authenticate(self, key: bytes)` as a public `@_serialized` method that calls `self._channel.authenticate(key)`. It keeps `self._encryption_key` (the TLS-PSK derivation and `landing_url` read it). The facade passes `session_timeout=lambda: ...` returning `self._config.security_config.session_timeout_seconds` when both exist and 0 otherwise. `OpenDisplayDevice.MAX_STRAY_AUTH_FRAMES = channel.MAX_STRAY_AUTH_FRAMES` keeps the public constant. `__aenter__` calls `self._attach(transport)` where it assigns `self._connection` today; `__aexit__`, the `except BaseException` path and `_on_disconnect` call `self._channel.reset()` only when a channel exists.

- [ ] **Step 1: Write the failing channel tests** in `tests/unit/test_device_channel.py`, using `FakeTransport` from `tests/conftest.py`:

```python
async def test_plain_channel_passes_frames_through(): 
    ch = SecureChannel(FakeTransport([b"\x00\x43"]))
    await ch.write(b"\x00\x43")
    assert ch.transport.written == [b"\x00\x43"]
    assert await ch.read(1.0) == b"\x00\x43"

def test_chunk_sizes_plain_ble():
    ch = SecureChannel(FakeTransport(max_frame=244))
    assert ch.direct_chunk_size() == 230 and ch.pipe_data_size(244) == 241

def test_chunk_sizes_large_frame_transport():
    assert SecureChannel(FakeTransport(max_frame=4094)).direct_chunk_size() == 4092

def test_chunk_sizes_in_legacy_session():
    ch = SecureChannel(FakeTransport(max_frame=244))
    ch.session = LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8)
    assert ch.direct_chunk_size() == 154 and ch.pipe_data_size(244) == 212

def test_seal_advances_the_nonce_counter():
    s = LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8)
    first, second = s.seal(b"\x00\x71abc"), s.seal(b"\x00\x71abc")
    assert s.nonce_counter == 2 and first != second

async def test_reset_drops_the_session():
    ch = SecureChannel(FakeTransport()); ch.session = LegacySession(b"\x01" * 16, b"\x02" * 8)
    ch.reset()
    assert ch.session is None

async def test_reauth_runs_before_write_at_90_percent(monkeypatch):      # Review Focus 5
    # session_timeout=lambda: 100, established_at = time.monotonic() - 91; replace ch.authenticate with a spy
    # write() -> spy called once before the frame is written; write_stream_frame() -> spy not called

async def test_no_reauth_when_timeout_is_zero(monkeypatch):
    # session_timeout=lambda: 0, established_at far in the past -> spy never called
```

Add to the same file, against the facade:

```python
async def test_command_before_connect_raises_not_connected():                # Review Focus 1
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    with pytest.raises(RuntimeError, match="Device not connected"):
        await device.read_firmware_version()

def test_disconnect_callback_before_attach_does_not_raise():                 # Review Focus 2
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._on_disconnect(); device._on_disconnect()

async def test_manual_authenticate_uses_encrypted_chunk_size(monkeypatch):   # Review Focus 4
    # device built with transport=FakeTransport(scripted v1 handshake), no encryption_key;
    # script and nonce patching as in tests/unit/test_auth_server_proof.py
    # await device.authenticate(key) -> device._channel.session is not None
    # and device._channel.direct_chunk_size() == 154
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_device_channel.py -q`
Expected: ImportError for `opendisplay.device.channel`.

- [ ] **Step 3: Implement `channel.py` and rewire `facade.py`** by moving the bodies per the table. Remove from the facade: `_session_key`, `_session_id`, `_nonce_counter`, `_auth_time`, `_ENCRYPTED_RESPONSE_MIN_LEN`, and the eight moved methods. Every `self._write(` / `self._read(` in the facade becomes `self._channel.write(` / `self._channel.read(`.

- [ ] **Step 4: Update the existing tests** with this mapping:

| Old test code | New test code |
|---|---|
| `device._connection = fake` | `device._attach(fake)` |
| `device._session_key = k` (with or without `_session_id`, `_nonce_counter`) | `device._channel.session = LegacySession(session_key=k, session_id=<the id the test set, else b"\x00" * 8>, nonce_counter=<if set>)`, after `_attach` |
| `device._auth_time = t` | `device._channel.session.established_at = t` |
| `device._session_key is None` assertions | `device._channel.session is None` |
| `device._read(...)`, `device._write(...)` | `device._channel.read(...)`, `device._channel.write(...)` |
| `device._clear_session()` | `device._channel.reset()` |
| patch `opendisplay.device.facade.generate_client_nonce` / `encrypt_command` / `decrypt_response` | patch `opendisplay.device.channel.<same name>` |

Some tests set only `_session_key` and relied on writes staying plaintext because `_session_id` was `None`. That half-session cannot occur outside tests. Give those a full `LegacySession` and patch `opendisplay.device.channel.encrypt_command` with `lambda key, sid, counter, cmd, payload: cmd + payload` when the test asserts on written bytes.

- [ ] **Step 5: Verify**

Run: `uv run pytest -q && uv run prek run --all-files`
Expected: all pass, lint clean.

- [ ] **Step 6: Commit**

```bash
git add src/opendisplay/device tests/unit
git commit -m "refactor: move the session into device/channel.py"
```

---

### Task 5: Extract the uploader

**Files:**
- Create: `src/opendisplay/device/upload.py`
- Create: `tests/unit/test_device_seams.py`
- Modify: `src/opendisplay/device/facade.py`
- Modify: tests that call `device._execute_upload`, `_dispatch_upload`, `_maybe_upload_partial`, `_send_pipe_chunks`, `_run_pipe_upload`, `_negotiate_pipe`, `_negotiate_pipe_partial`, `_pipe_data_size`, or read or set `_pipe_probed`, `_pipe_supported`, `_pipe_partial_supported`

**Interfaces:**
- Consumes: `SecureChannel` from Task 4 (`write`, `write_stream_frame`, `read`, `direct_chunk_size`, `pipe_data_size`, `session`, `transport`).
- Produces:

```python
# upload.py
@dataclass(frozen=True)
class UploadTarget:
    config: GlobalConfig | None
    color_scheme: ColorScheme | None     # None when capabilities are unknown
    name: str                            # mac address, for log lines

class Uploader:
    TIMEOUT_FIRST_CHUNK = 10.0
    TIMEOUT_ACK = 5.0
    TIMEOUT_DIRECT_WRITE_DATA_ACK = 90.0
    TIMEOUT_UNCOMPRESSED_END_ACK = 90.0
    TIMEOUT_COMPRESSED_END_ACK = 90.0
    TIMEOUT_REFRESH = 90.0
    TIMEOUT_PIPE_DATA_COMPRESSED = 5.0
    TIMEOUT_PIPE_DATA_UNCOMPRESSED = 90.0
    TIMEOUT_PIPE_TAIL_FLUSH = 0.5

    def __init__(self, channel: SecureChannel, *, target: Callable[[], UploadTarget],
                 blocks_per_ack: int, max_queue_size: int) -> None: ...
    pipe_probed: bool                    # was _pipe_probed
    pipe_supported: bool                 # was _pipe_supported
    pipe_partial_supported: bool | None  # was _pipe_partial_supported
    def reset_pipe_state(self) -> None: ...
    @property
    def supports_compression(self) -> bool: ...
    def pipe_write_allowed(self, display: DisplayConfig | None) -> bool: ...
    async def upload_prepared(self, image_data, compressed_data, processed_image, refresh_mode,
                              compress, progress_callback, state) -> None: ...   # today's _upload_prepared signature
```

Every other method from lines 2011-3001 moves onto `Uploader` with its leading underscore dropped and its signature otherwise unchanged (`dispatch_upload`, `update_partial_state`, `send_partial_chunks`, `maybe_upload_partial`, `execute_upload`, `await_refresh_complete`, `send_data_chunks`, `negotiate_pipe`, `effective_pipe_params`, `negotiate_pipe_partial`, `run_pipe_upload`, `send_pipe_chunks`, `await_pipe_end_ack`). `_direct_write_chunk_size` and `_pipe_data_size` are not moved: their callers use `self._channel.direct_chunk_size()` and `self._channel.pipe_data_size(...)`.

State mapping inside the moved code:

| Reads today | Becomes |
|---|---|
| `self._read`, `self._write`, `self._write_pipe_frame` | `self._channel.read`, `.write`, `.write_stream_frame` |
| `self._conn.max_frame` | `self._channel.transport.max_frame` |
| `self._session_key is not None` | `self._channel.session is not None` |
| `self._config`, `self._display_config` | `self._target().config`, and its first display or `None` |
| `self.color_scheme` | `self._target().color_scheme`; if `None`, raise the same `RuntimeError` text `_ensure_capabilities` raises today |
| `self.mac_address` | `self._target().name` |
| `self._blocks_per_ack`, `self._max_queue_size` | same names on `Uploader` |

Facade side: `_attach` also creates `self._uploader = Uploader(self._channel, target=self._upload_target, blocks_per_ack=..., max_queue_size=...)`; `_upload_target()` builds the `UploadTarget` from `self._config`, `self._capabilities` and `self.mac_address`. `_on_disconnect` calls `self._uploader.reset_pipe_state()` when an uploader exists. The two `await self._upload_prepared(...)` call sites become `await self._uploader.upload_prepared(...)`; `upload_image` reads `self._uploader.supports_compression`. The facade's nine upload timeouts become aliases so one definition exists: `TIMEOUT_REFRESH = Uploader.TIMEOUT_REFRESH`, and so on. `TIMEOUT_CONFIG_CHUNK` and `TIMEOUT_NFC_WRITE` stay defined on the facade. The lock stays on the facade: `Uploader` methods run only inside the facade's `@_serialized` upload methods and take no lock themselves.

- [ ] **Step 1: Write the failing test** in `tests/unit/test_device_seams.py`:

```python
async def test_second_connection_starts_clean():                             # Review Focus 3
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=<a config from an existing test helper>)
    device._attach(FakeTransport())
    device._channel.session = LegacySession(b"\x01" * 16, b"\x02" * 8)
    device._uploader.pipe_probed = True; device._uploader.pipe_supported = True
    device._on_disconnect()
    assert device._channel.session is None
    assert (device._uploader.pipe_probed, device._uploader.pipe_supported,
            device._uploader.pipe_partial_supported) == (False, False, None)
    device._attach(FakeTransport())
    assert device._channel.session is None and device._uploader.pipe_probed is False

def test_facade_timeouts_are_the_uploader_values():
    for name in ("TIMEOUT_FIRST_CHUNK", "TIMEOUT_ACK", "TIMEOUT_DIRECT_WRITE_DATA_ACK",
                 "TIMEOUT_UNCOMPRESSED_END_ACK", "TIMEOUT_COMPRESSED_END_ACK", "TIMEOUT_REFRESH",
                 "TIMEOUT_PIPE_DATA_COMPRESSED", "TIMEOUT_PIPE_DATA_UNCOMPRESSED", "TIMEOUT_PIPE_TAIL_FLUSH"):
        assert getattr(OpenDisplayDevice, name) == getattr(Uploader, name)
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/unit/test_device_seams.py -q`
Expected: ImportError for `opendisplay.device.upload`.

- [ ] **Step 3: Implement `upload.py` and rewire `facade.py`** per the Interfaces block and the mapping. Delete `_display_config` from the facade if nothing there reads it any more.

- [ ] **Step 4: Update the existing tests**

| Old test code | New test code |
|---|---|
| `device._execute_upload(...)` and the other moved methods | `device._uploader.execute_upload(...)` (underscore dropped) |
| `device._pipe_probed`, `_pipe_supported`, `_pipe_partial_supported` | `device._uploader.pipe_probed`, `.pipe_supported`, `.pipe_partial_supported` |
| `device._pipe_data_size(n)` | `device._channel.pipe_data_size(n)` |
| `from opendisplay.device.facade import _PipePartialEtagMismatch, _PipePartialRejected` | `from opendisplay.device.upload import ...` |
| tests that replace a moved method on the device (`device._execute_upload = spy`, `monkeypatch.setattr(device, "_run_pipe_upload", ...)`) | set it on `device._uploader` under the new name |
| patch strings for names the moved code looks up | `opendisplay.device.upload.<name>` |

- [ ] **Step 5: Verify**

Run: `uv run pytest -q && uv run prek run --all-files`
Expected: all pass, lint clean, `wc -l src/opendisplay/device/*.py` shows every file under 1,500 lines.

- [ ] **Step 6: Commit**

```bash
git add src/opendisplay/device tests/unit
git commit -m "refactor: move the upload paths into device/upload.py"
```

---

### Task 6: Tidy up and update the project notes

**Files:**
- Modify: `src/opendisplay/device/facade.py`, `CLAUDE.md`

- [ ] **Step 1: Remove what the moves left behind** in `facade.py`: unused imports, and any inline pylint disable whose reason moved away (for example `too-many-instance-attributes` on the class, if the attribute count is now at or under 22). Do not add new disables.

- [ ] **Step 2: Check for leftovers**

Run: `grep -nE "_session_key|_session_id|_nonce_counter|_auth_time|_pipe_probed|_write_pipe_frame" -r src tests`
Expected: no output.

- [ ] **Step 3: Update `CLAUDE.md`**, section Architecture: replace the `device.py` bullet with the four files of `device/` and one line each on their responsibility; change the `crypto.py` bullet's "in `device.py`'s read/write path" to "in `device/channel.py`"; in "Upload flow" name `Uploader.dispatch_upload()` instead of `_dispatch_upload()`.

- [ ] **Step 4: Verify**

Run: `uv run pytest -q && uv run prek run --all-files && uv run pytest tests/unit/test_public_api_snapshot.py -q`
Expected: all pass, lint clean. `git diff main -- tests/unit/data/public_api.json` shows the file only as added, with no later change.

- [ ] **Step 5: Commit**

```bash
git add src/opendisplay/device/facade.py CLAUDE.md
git commit -m "refactor: drop leftovers from the device split and update the notes"
```

---

### Task 7: Hardware check and pull request

- [ ] **Step 1: Repeat the smoke test (needs the user and the devices)**

```bash
uv run python scripts/smoke_seams.py --device OD405BD8 --partial 2>&1 | tee scratch/smoke-after-e1003.txt
uv run python scripts/smoke_seams.py --device OD1F9C04 --key "$(awk -F, '$1=="OD1F9C04"{print $2}' device_inventory.csv)" 2>&1 | tee scratch/smoke-after-bg22.txt
uv run python scripts/smoke_seams.py --device OD4064C9 --key "$(awk -F, '$1=="OD4064C9"{print $2}' device_inventory.csv)" 2>&1 | tee scratch/smoke-after-e1002.txt
diff <(cut -d: -f1 scratch/smoke-before-e1003.txt) <(cut -d: -f1 scratch/smoke-after-e1003.txt)
diff <(cut -d: -f1 scratch/smoke-before-bg22.txt) <(cut -d: -f1 scratch/smoke-after-bg22.txt)
diff <(cut -d: -f1 scratch/smoke-before-e1002.txt) <(cut -d: -f1 scratch/smoke-after-e1002.txt)
```

Expected: all three diffs empty, and the panels show the expected images. A step that passed before and fails after blocks the PR.

- [ ] **Step 2: Open the pull request (ask the user before pushing)**

Title, plain, no Conventional Commits prefix: `Split the device module into channel, upload and imaging`. Body: what moved where (the File Structure table), the statement that the public API and the wire bytes are unchanged with the snapshot test and the smoke results as evidence, and a note that this prepares firmware 3.0 support (link the spec).
