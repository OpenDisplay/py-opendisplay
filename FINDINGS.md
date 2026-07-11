# py-opendisplay — Performance & Correctness Findings

Analysis of py-opendisplay (all 15.8k lines of `src/`) cross-checked against the device
firmware at `../opendisplay-firmware` (OpenDisplay/Firmware, commit `4f4b503`), including
the vendored bb_epaper and uzlib sources. Performance numbers were measured with
cProfile + `perf_counter` benchmarks (py-spy 0.4.2 cannot sample Python 3.14); all
vectorized prototypes were verified **byte-identical** to the current implementations.

Baseline: `uv run pytest -q` → 439 passed.

Severity legend: 🔴 critical (data corruption / broken feature / security),
🟠 major (wrong behavior in realistic scenarios), 🟡 minor.

Some findings are firmware bugs that the library walks into silently; they are marked
**[FW]** and included because the library is the side that can mitigate today.

---

## 1. Performance

### P1. 🔴 Per-pixel Python loops in all encoders — 100–1300× slower than numpy equivalents

Every encoder iterates pixel-by-pixel with numpy scalar indexing (`pixels[y, x]` allocates
a numpy scalar object per pixel). cProfile on `prepare_image()` @ 800×480 shows the encoder
loops are **60–73 % of total pipeline CPU** (e.g. MONO: `encode_1bpp` 0.135 s of 0.224 s;
BWGBRY: `encode_4bpp` 0.258 s plus 1.15 M `dict.get` calls costing another 0.089 s).

Measured (median, current → vectorized prototype):

| Function | 296×128 | 800×480 | 1600×1200 | Speedup @800×480 |
|---|---|---|---|---|
| `encode_1bpp` (`encoding/images.py:120`) | 4.33 ms → 8.2 µs | 45.1 ms → 42 µs | 225 ms → 172 µs | **1066×** |
| `encode_2bpp` (`encoding/images.py:153`) | 7.02 ms → 25 µs | 71.2 ms → 193 µs | 355 ms → 921 µs | **370×** |
| `encode_4bpp` (`encoding/images.py:197`) | 6.48 ms → 22 µs | 66.6 ms → 176 µs | 311 ms → 858 µs | **378×** |
| `encode_4bpp` bwgbry | 6.23 ms → 60 µs | 64.8 ms → 555 µs | 318 ms → 2.8 ms | **117×** |
| `encode_bitplanes` (`encoding/bitplanes.py:53`) | 5.05 ms → 13 µs | 51.3 ms → 65 µs | 260 ms → 268 µs | **790×** |
| `encode_gray4_bitplanes` (`encoding/bitplanes.py:102`) | 6.00 ms → 52 µs | 61.8 ms → 454 µs | 310 ms → 2.2 ms | **136×** |

Fixes (all verified byte-identical on widths 122, 250, 799, 127, 1 — i.e. not divisible
by 8/4/2 — and full 0–255 palette indices):

- `encode_1bpp`: `np.packbits(np.asarray(image) > 0, axis=1).tobytes()` —
  `packbits(axis=1)` zero-pads each row to a byte boundary, exactly matching the current
  `bytes_per_row` layout.
- `encode_bitplanes`: `np.packbits(pixels == 1, axis=1)` / `np.packbits(pixels == 2, axis=1)`.
- `encode_gray4_bitplanes`: `codes = np.asarray(gray_codes, np.uint8)[pixels & 3]`, then
  `np.packbits(codes & 1, axis=1)` / `np.packbits(codes & 2, axis=1)`.
- `encode_2bpp`: mask to 2 bits, pad width to a multiple of 4, reshape `(h, -1, 4)`,
  combine `(p0<<6)|(p1<<4)|(p2<<2)|p3`, `.tobytes()`.
- `encode_4bpp`: replace the `BWGBRY_MAP` dict (rebuilt on every call, `.get` per pixel)
  with a 16-entry `np.uint8` LUT (`[0,1,2,3,5,6] + [0]*10` — preserves the `.get(…, 0)`
  default), index `lut[pixels & 0x0F]`, pad to even width, `(hi<<4)|lo`.

Full-pipeline effect (`prepare_image` = fit + Rust dither + encode + zlib-6):
**2.3–2.9× faster overall** (e.g. MONO 800×480: 74 ms → 28 ms; BWGBRY 1600×1200:
550 ms → 244 ms). Afterwards the already-Rust dither step dominates (~85–90 %), which is
where the time should be.

Prototypes/benchmarks (reproducible):
`/tmp/claude-1000/-home-paulus-dev-hass-py-opendisplay/152e76eb-1aaa-45e4-bee7-ead60beb363c/scratchpad/{vectorized,verify,bench}.py`

### P2. 🟠 `compute_bounding_rect` is an O(W·H) pure-Python double loop — `partial.py:140-163`

Runs on **every** partial-capable upload; scans the full width of every row even when the
row is unchanged. Measured 13.9 ms @800×480, 71.6 ms @1600×1200 → 49 µs / 166 µs
vectorized (~300×):

```python
diff = (np.frombuffer(old, np.uint8) != np.frombuffer(new, np.uint8)).reshape(height, width)
rows = np.flatnonzero(diff.any(axis=1))
if rows.size == 0:
    return None
cols = np.flatnonzero(diff.any(axis=0))
return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)
```

### P3. 🟠 `encode_segment_wire` repeats the per-pixel loops — `partial.py:199-230`

0.51 ms → 6.6 µs for a small rect @800×480; grows to full-frame numbers for large diffs.
Same packbits/LUT patterns as P1 apply, with one caveat: this function packs **flat across
row boundaries** (unlike `encode_1bpp`'s per-row padding), so the vectorized version must
use flat `np.packbits`, not `axis=1`.

### P4. 🟠 `upload_image` always encodes + compresses the full frame before trying the partial path — `device.py:1136-1152`

When the partial upload succeeds, the full-frame encode (45–71 ms today) and zlib-6
compression (2.7 ms @800×480, 15.7 ms @1600×1200) are pure waste. After vectorizing P1 the
waste is mostly the compression. Fix: when `state is not None`, prepare with
`compress=False` and compress lazily on the full-upload fallback — `_dispatch_upload`
already recompresses when the zlib window mismatches (`device.py:1244-1245`), so the
machinery exists.

### P5. 🟠 BLE throughput: stop-and-wait, write-with-response, fixed chunk sizes

- **Partially addressed:** 0x71 data chunks now use BLE Write Without Response
  (`transport/connection.py` `write_command(..., response=False)`, opted in from
  `_send_data_chunks`/`_send_partial_chunks`), removing the GATT write-with-response
  round-trip. The firmware ACK notification per chunk is still awaited (stop-and-wait),
  which preserves flow control and needs no firmware change — so exactly one write is in
  flight at a time. This roughly halves the RTTs per chunk. The characteristic is probed
  for the `write-without-response` property and falls back to write-with-response if absent.
- Remaining: the per-chunk firmware ACK is still serialized. A true in-flight window /
  periodic ACK checkpoints would multiply throughput further, but that DOES need a
  firmware-coordinated change (firmware ACKs every 0x71, and the ESP32 command queue is
  only 5 deep). `PIPELINE_CHUNKS` (`protocol/commands.py:52`) stays 1 for now.
- No MTU negotiation: `CHUNK_SIZE = 230` / `ENCRYPTED_CHUNK_SIZE = 154`
  (`protocol/commands.py:47-48`) are constants; `client.mtu_size` is never consulted, so
  links that negotiate 247+ leave throughput on the table.
- `MAX_COMPRESSED_SIZE = 50 KB` gate (`protocol/commands.py:54`, used at
  `device.py:1247-1248`) is obsolete for streaming firmware — current firmware inflates
  through a 256-byte chunk buffer (`display_service.h:7`, `od_zlib_stream.c`), no 50 KB
  buffer exists. For non-ZIPXL configs this needlessly forces large panels to upload
  uncompressed.

### P6. 🟡 Small stuff

- Per-chunk `bytes` slices + `cmd + chunk_data` concatenation allocate ~2 copies per chunk
  (`device.py:1565`, `protocol/commands.py:214`); a `memoryview` over the source would
  avoid the slice copies. Negligible next to P5.
- `_maybe_upload_partial` builds the old image via `palette_image.copy()` +
  `frombytes` (`device.py:1357-1358`); simpler and slightly faster as
  `Image.frombytes("P", (w, h), region.old_palette)`.
- zlib level 6 is the right choice — measured level 9 is 2.2–2.6× slower for <0.3 pp of
  ratio; level 1 saves CPU but costs ~1.5 pp and BLE time dominates. No change.
- `ota.py`'s fixed 0.05 s inter-packet delay (`ota.py:92-100`) is intentional proxy
  pacing; `fast=True` already bypasses it. No change.

---

## 2. Correctness — Critical

### C1. 🔴 [FW] BWR/BWY uploads lose the red/yellow plane — 3-color direct write is broken end-to-end

Python sends `plane1 + plane2` (`device.py:247-249`, `encoding/bitplanes.py:14-68`), but
firmware sets `directWriteTotalBytes = (pixels+7)/8` — **one** plane — for bitplane
uploads (`display_service.cpp:1431`) and never switches to `PLANE_1`
(`directWritePlane2` is written at `display_service.cpp:1426` but never read;
`bbepStartWrite` is issued once for `PLANE_0` at `display_service.cpp:1471`).
Consequence chain: compressed START is NACKed (decompressed-size mismatch,
`display_service.cpp:1445-1451`) → Python silently falls back to uncompressed
(`device.py:1490-1505`) → firmware auto-refreshes after plane 1 and the R/Y plane is
discarded → BW layer renders over stale red RAM, **no error surfaced**.
Primary fix is firmware (double the total, switch planes at the boundary like
`streamGray4Bytes`, `display_service.cpp:1365-1383`). Library mitigation: warn or refuse
on BWR/BWY until firmware parity, and document the contract.

### C2. 🔴 [FW] Widths not divisible by 8 (or 4/2 at 2/4 bpp): bottom rows silently truncated

Python row-pads to byte boundaries (`encoding/images.py:117,150,194`) — which matches
panel RAM pitch — but firmware computes the expected upload size from raw pixel count
(`pixels = w*h; total = (pixels+7)/8`, `display_service.cpp:1430-1436`); only the gray4
path is row-padded (`display_service.cpp:1442`). On 122-px-wide 2.13" panels (EP213
family): Python sends 16×250 = 4000 bytes, firmware expects 3813 → auto-END fires 187
bytes early, the last ~12 rows are never written, refresh proceeds, no error. Compressed
uploads NACK at START and fall back to the same truncated path. Firmware fix: use
row-padded math like `calc_controller_plane_bytes` (`display_service.cpp:2008-2010`).
Library mitigation: detect `width % 8 != 0` panels and warn.

### C3. 🔴 zlib window mismatch: current firmware only accepts wbits ≤ 9; Python defaults to 15

All current firmware builds compile uzlib with `OPENDISPLAY_ZLIB_WINDOW_BITS 9`
(`lib/uzlib/src/uzlib.h:21-22`; the 15-bit override is commented out in every
`platformio.ini` env) and hard-reject any zlib header advertising more
(`od_zlib_stream.c:641-644`). Python selects wbits=9 only when the config advertises
ZIPXL (`device.py:259-262,1242-1245`); otherwise wbits=15
(`encoding/compression.py:10-11`). For ZIP-but-not-ZIPXL devices:

- **Full uploads**: firmware answers `{0xFF,0xFF}` to START → Python falls back to
  uncompressed. Works, but *every* upload silently loses compression (e.g. 192 KB raw for
  a Spectra 7.3").
- **Partial uploads**: the wbits=15 stream rides inside the 0x76 initial bytes
  (`device.py:1378-1381`) → firmware NACKs `ERR_PARTIAL_STREAM` → Python **raises
  ProtocolError** (`device.py:1424`) instead of falling back. Hard failure.

Fix: use wbits=9 whenever compressing for this protocol (a 9-bit stream decodes fine on
any 15-bit-window firmware — the firmware check is `<=`), and make non-etag 0x76 NACKs
fall back (see C6).

### C4. 🔴 Chunked `write_config` first packet is 2 bytes short — configs > 200 bytes corrupt on device

`build_write_config_command` sends a first multi-chunk payload of
`total_size(2) + 198 data = 200` bytes (`protocol/commands.py:298-350`, esp. `:338`).
Firmware enters chunked mode only when payload `len > 200` and expects
`[total:2LE][200 data]` = 202 bytes (`communication.cpp:368,373-379`). With 200 bytes the
firmware takes the *single-chunk* path and `saveConfig()`s the raw 200 bytes — the 2-byte
size header plus truncated config stored as the whole config — then every subsequent 0x42
chunk is NACKed (`communication.cpp:416-419`). The 198-byte first chunk also breaks the
firmware's `expectedChunks = ceil(total/200)` accounting. Since a realistic full config
(system+manufacturer+power+display+… ≈ 200+ bytes) exceeds 200 bytes, **`write_config`
is broken against this firmware for most real configs**.
Fix: first payload = 2 size bytes + 200 data bytes (202 total), 200-byte continuations.

### C5. 🔴 No command serialization → AES-CCM nonce reuse and response mixups under concurrency

There is no lock around the write→read transaction (`device.py:437-447`,
`transport/connection.py:270-288`). Two concurrent commands (e.g.
`asyncio.gather(dev.upload_image(...), dev.read_firmware_version())`, or re-entry from a
progress callback) can read the same `_nonce_counter` before either increments → identical
(key, nonce) pair, which breaks AES-CCM confidentiality/integrity outright. The firmware
side also rejects the replay (`encryption.cpp:136`) and after repeated integrity failures
clears the session (`encryption.cpp:679-682`), killing an in-flight upload. Even
unencrypted, both commands read answers from the same shared queue and get each other's
responses. Fix: an `asyncio.Lock` held across the full command round-trip; increment the
nonce counter inside it.

### C6. 🔴 Shared notification queue desyncs permanently after any timeout

`_notification_callback` enqueues every notification; `read_response` pops the next item
with no request/response correlation and no flush on error
(`transport/connection.py:236-288`). If a response arrives just after its
`BLETimeoutError` fired (e.g. an END ACK at 90.001 s), it is returned as the answer to the
**next** command, and every subsequent read is off by one until reconnect. Any unsolicited
frame (late 0x74, firmware debug frame) has the same effect. Fix: drain the queue at the
start of each command transaction and after any timeout; ideally correlate by command
code. Related: the `asyncio.wait_for(queue.get(), …)` pattern can also *lose* an item
delivered during cancellation (`transport/connection.py:282-288`) — re-check
`get_nowait()` on timeout before giving up.

### C7. 🔴 ManufacturerData BLE round-trip corrupts bytes 4–21 (verified by execution)

`_parse_manufacturer_data` (`protocol/config_parser.py:297-310`) stores `data[4:22]` into
`reserved` and leaves the simple-config fields at 0, but the serializer
(`protocol/config_serializer.py:107-141`) writes `simple_config_driver_index/
display_index/power_index/configured_at` at offsets 4–15 and only `reserved[:6]` at
16–21. A read-modify-write therefore zeroes offsets 4–15 (destroying toolbox
"simple config" metadata) and writes the original bytes 4–9 into 16–21.
`ManufacturerData.from_bytes` (`models/config.py:171-186`) parses the layout correctly but
is not used by the parser — delegate to it.

### C8. 🔴 Config serializers emit truncated packets when `reserved` fields are short — desyncs the whole config (verified)

Every serializer except display does `reserved[:N]` without `.ljust(N, b"\x00")`
(`protocol/config_serializer.py:101,196,300,330,372,422,432,451,465,490,511`). The TLV
format has no per-packet length — firmware `memcpy`s fixed `sizeof(struct)`
(`config_parser.cpp:251-257`) — so one short packet shifts and misparses **everything
after it** on the device. This is not hypothetical: `DataBus.from_bytes`
(`models/config.py:514`) produces exactly such a short `reserved` (12 bytes instead of
14, see C9). Fix: pad all fixed-size buffers on serialize.

---

## 3. Correctness — Major

### M1. 🟠 Partial updates attempted on panels firmware always rejects, and NACKs abort instead of falling back

Firmware only allows partial refresh when `getBitsPerPixel() == 1`
(`display_service.cpp:1519-1525` → `ERR_PARTIAL_UNSUPPORTED`) and requires x/w multiples
of **8** regardless of bpp (`display_service.cpp:1534`). Python:

- `compute_partial_region` only excludes BWR/BWY/GRAYSCALE_4 (`partial.py:90`), so BWRY,
  BWGBRY and GRAYSCALE_16 proceed — and `align_rect` aligns to 4/2 pixels for them
  (`partial.py:107-116`), guaranteeing rejection.
- `_maybe_upload_partial` treats only `(0x76, ERR_ETAG_MISMATCH)` as `"fallback_full"`;
  every other NACK (`ERR_RECT_OOB/ALIGN/FLAGS/STREAM/UNSUPPORTED`) raises `ProtocolError`
  (`device.py:1417-1424`), losing the upload entirely.
- Concrete mono trigger: 122-px-wide panels — a full-width diff yields `w = 122`, fails
  the firmware `rectW & 7` check → `ERR_RECT_ALIGN` → exception.

Fix: restrict partial to `ColorScheme.MONO`, align to 8 pixels, and treat all pre-refresh
0x76/0x71 NACKs as fallback-to-full.

**Update (pipe-partial):** partial-region refresh now rides the sliding window when the
device advertises `supports_pipe_write` and `max_queue_size > 1`. `_maybe_upload_partial`
negotiates an extended `0x0080` START (flags bit1 `PIPE_FLAG_PARTIAL` + 12-byte LE
`[old_etag][x][y][w][h]` geometry, `total_size = plane_size*2`); the device confirms with
ACK flags bit1. New START NACK codes gate the fallback ladder: `0x05 ETAG_MISMATCH` → skip
0x76, go full (device already cleared its etag); `0x06 PARTIAL_UNSUPPORTED`/`0x07
RECT_INVALID` → go full (0x76 would fail identically; 0x06 caches a per-connection
negative); `0x02` on a partial request (after one uncompressed-still-partial retry) or an
ACK without bit1 → disable pipe-partial for the connection and fall back to legacy 0x76;
silence/garble → 0x76. Partial transfers never auto-complete (firmware waits for the
explicit `0x0082` END which alone carries the refresh selector `2` + new_etag), so the
sender uses the same explicit-END contract compressed transfers use. Encryption parity
holds: the 24-byte plaintext START fits one CCM frame, data frames size to 212 B @ frame
244, and the 0x76 fallback rung still caps at `ENCRYPTED_CHUNK_SIZE`.

### M2. 🟠 [FW-interplay] Etag never committed on uncompressed full uploads — partial mode never engages, and a stale-etag hazard exists

Uncompressed uploads always finish via firmware auto-END at the exact byte count
(`display_service.cpp:1609-1610`), so Python's END-with-etag is never sent
(`device.py:1517-1524` skipped when `auto_completed`) and the device's `displayed_etag` is
never set — yet Python still stores `full_upload_etag` in `PartialState`
(`device.py:1174-1175,1283-1301`). Result: on uncompressed-only devices every partial
attempt gets `ERR_ETAG_MISMATCH` (wasted round-trip, permanent full-upload fallback).
Worse, firmware doesn't clear `displayed_etag` on etag-less uploads, so a persisted
`PartialState` matching a stale device etag passes the check
(`display_service.cpp:1512`) after unrelated full uploads changed the screen → partial
renders against the wrong old-plane → visual corruption. Fixes: don't populate
`state.etag` when the upload auto-completed; firmware should zero `displayed_etag` in
`handleDirectWriteStart`.

### M3. 🟠 BWRY red/yellow swapped on EP29YR_128x296 and EP29YR_168x384 panels

Python hardcodes palette order black/white/yellow=2/red=3 (`encoding/images.py:131-162`,
`partial.py:210-215`). Most YR panels use bb_epaper `u8Colors_4clr_v2` (matches), but
panel tags 0x001D/0x001E use `u8Colors_4clr` where native 2=red, 3=yellow
(`bb_ep.inl:50-68, 3773-3812`), and the firmware direct-write path streams nibbles raw
(`display_service.cpp:1605`). Fix: per-panel BWRY code table in `display_palettes.py`,
analogous to `_GRAY4_CODES_BY_PANEL`.

### M4. 🟠 NFC config packet 0x2A is unknown to firmware and aborts its parse — flash/data_extended packets after it are silently dropped

Python emits 0x2A before 0x2B/0x2C (`protocol/config_serializer.py:615-629`); firmware has
no `case 0x2A` and its `default:` skips to the CRC (`config_parser.cpp:568-571`), so any
config containing NFC entries loses flash_config and data_extended on device load.
Fix: emit unknown-to-firmware packets last and/or gate on firmware version.

### M5. 🟠 Public `from_bytes` layouts disagree with firmware structs

`DisplayConfig.SIZE = 66` vs firmware 46 (`models/config.py:353` vs `structs.h:93-121`),
`PowerOption.SIZE = 32` vs 30 (`models/config.py:236` vs `structs.h:44-61`),
`DataBus.SIZE = 28` vs 30 (`models/config.py:492` vs `structs.h:165-180`). The main parse
path (`protocol/config_parser.py:256-273`) uses correct private sizes, but the public
APIs reject or misparse real packets, and `DataBus.from_bytes` → serialize produces the
short packet of C8. Fix: align sizes and make the parser delegate to `from_bytes` so each
layout is defined once.

### M6. 🟠 `tx_power` sign confusion crashes serialization (verified)

BLE parse path unpacks signed `b` (`protocol/config_parser.py:334`),
`PowerOption.from_bytes` reads unsigned (`models/config.py:248`), serializer packs signed
`b` (`protocol/config_serializer.py:181`); firmware field is `uint8_t` (`structs.h:48`).
Byte 0xF4 → −12 via one path, 244 via the other; serializing 244 raises `struct.error`.
Negative values also export to JSON as `"0x-c"`, which re-import can't parse. Fix: treat
as unsigned end-to-end, expose a signed-dBm helper if desired.

### M7. 🟠 JSON export silently zeroes real config data

`models/config_json.py:153-160` hardcodes `full_update_mC` (firmware energy accounting,
`structs.h:119`) and `reserved_pin_2..8` to `"0x0"`; the binary_input export
(`config_json.py:233-243`) omits the 8 GPIO `reserved_pins` (`structs.h:187-194`), the
ADC-ladder thresholds, and `power_off_flags`/`power_off_hold_sec` (`structs.h:200-201`) —
import recreates them as zeros. A binary→JSON→binary round-trip written back to a device
silently misconfigures hardware pins. Related (🟡): several real firmware fields live
inside Python `reserved` blobs (`charge_enable_pin`/`charge_state_pin`/`charger_flags`,
touch `enable_pin`) — preserved on binary RMW but uneditable and lost via JSON.

### M8. 🟠 Server proof from authentication is never verified — mutual auth is one-way

Firmware computes `CMAC(session_key, server_nonce‖client_nonce‖device_id)` precisely so
the client can authenticate the device (`encryption.cpp:612-627`);
`parse_authenticate_success` returns the 16-byte proof and `authenticate()` discards it
(`device.py:521-522`). A device (or MITM) that returns status 0x00 without knowing the
key is accepted; detection defers to the first encrypted exchange. Fix: recompute and
constant-time-compare, raise `AuthenticationFailedError` on mismatch.

### M9. 🟠 Session state not cleared on disconnect; no bleak disconnect callback

`disconnect()`/`__aexit__` keep `_session_key`/`_session_id`/`_nonce_counter`
(`device.py:420-428`), and no `disconnected_callback` is registered
(`transport/connection.py:168-177`). Reconnecting the same object encrypts against a
session the firmware no longer has → misleading `AuthenticationRequiredError`; an
unexpected drop mid-read manifests as a full timeout instead of an immediate failure.

### M10. 🟠 Encrypted partial START exceeds the encrypted payload budget

The compressed START correctly caps its inline payload at `ENCRYPTED_CHUNK_SIZE`
(`device.py:1476`), but the partial START uses the default `MAX_START_PAYLOAD = 200`
(`device.py:1403-1413`, `protocol/commands.py:152-193`) → encrypted frames up to ~229
bytes vs the ~185-byte envelope every other encrypted write respects. Works on high-MTU
links, may fragment/fail on constrained ones. Fix: thread the encrypted budget in, as the
compressed path does.

### M11. 🟠 `LedFlashConfig(group_repeats=255)` silently means "repeat forever"

Python allows 1–255 and encodes `group_repeats - 1` (`models/led_flash.py:50-51,100`);
raw 0xFE is the firmware's infinite sentinel (`device_control.cpp:224,259`). A request for
255 finite repeats loops forever. Fix: validate 1–254. Converse (🟡): `from_bytes` on raw
0xFF (firmware-valid, 0 repeats) raises (`models/led_flash.py:127-128`).

---

## 4. Correctness — Minor

- 🟡 **Error frames crash with the wrong exception type**: `check_response_type` /
  `unpack_command_code` raise raw `struct.error` on <2 bytes and `ValueError` for unknown
  codes (`protocol/responses.py:27-37,73`); firmware's 2-byte `{0xFF,0xFF}`
  compressed-failure frame and 4-byte NACKs hit exactly this in the upload loop
  (`device.py:1534,1575`). Wrap in `InvalidResponseError`/`ProtocolError`.
- 🟡 **`interrogate()` misreads the READ_CONFIG error frame** `{0xFF,0x40,0x00,0x00}`
  (`communication.cpp:352-354`) as a zero-length config instead of "device has no config"
  (`device.py:692-696`).
- 🟡 **3-byte `0xFF` integrity-failure frames** (`communication.cpp:527`) aren't
  distinguished in `_read` (`device.py:482-488`) — surfaced as a vague ACK mismatch
  instead of "session integrity failure — re-authenticate".
- 🟡 **Buzzer NACK codes discarded**: firmware sends `{0xFF,0x77,err,0x00}` with codes
  0x01–0x06 (`buzzer_control.cpp:96-145`); `activate_buzzer` uses plain
  `validate_ack_response` (`device.py:926`) unlike `activate_led` which decodes them.
  Also no client-side validation of empty patterns / step counts / `outer_repeats` range.
- 🟡 **Unknown config packet type aborts Python's parse silently**
  (`protocol/config_parser.py:129-131` → `break` + warning), and a subsequent
  `serialize_config` persists the loss. Should raise or mark the config partial.
  Similarly, duplicate `(type, number)` packets collapse in a dict
  (`config_parser.py:153-154`) where firmware keeps both.
- 🟡 **Python requires system/manufacturer/power/display packets** that firmware treats as
  optional (`config_parser.py:208-219` vs `config_parser.cpp` — no required check); a
  display-less node config is unreadable.
- 🟡 **Config header/CRC conventions**: Python writes 0x0000 in bytes 0–1 and CRC32-low16;
  the factory/toolbox convention is total length + CRC-16/CCITT
  (`factory_config.cpp:8-35`). Firmware currently validates neither on write
  (`communication.cpp:357-399`), but a Python-written blob fails `factoryPacketValid`.
- 🟡 **Flash-config cap mismatch**: Python allows 4 instances
  (`config_serializer.py:621-623`), firmware loads max 2 and silently drops the rest
  (`config_parser.cpp:407-413`).
- 🟡 **DataExtended strings**: Python allows full 32-byte unterminated strings
  (`models/config.py:42-45`); firmware force-terminates at 31 (`config_parser.cpp:390-398`)
  → last byte lost; UTF-8 truncation can also split a multibyte char. Cap at 31.
- 🟡 **`Rotation` enum stores degrees but the wire field is an index**: assigning
  `DisplayConfig(rotation=Rotation.ROTATE_90)` serializes 90, not 1
  (`models/enums.py:281-287`; firmware `config_parser.cpp:663` prints `rotation * 90`).
  Read side maps correctly.
- 🟡 **`SensorType` missing `BQ27220 = 5`** (`models/enums.py:159-166` vs `structs.h:153`);
  degrades to raw int.
- 🟡 **`AdvertisementData.touch_event` accepts negative `start_byte`**
  (`models/advertisement.py:97-101`) — negative indexing returns garbage; firmware valid
  range is 0–6 (`touch_input.cpp:483-484`).
- 🟡 **`RefreshMode.PARTIAL` on a full upload silently degrades to FULL** (firmware only
  distinguishes `data[0]==1`, `display_service.cpp:1671-1672`); only meaningful in a 0x76
  session. Worth documenting/validating.
- 🟡 **`encode_image` GRAYSCALE_4 returns packed 2bpp** (`encoding/images.py:90-91`) that
  no firmware path accepts — the device needs two 1-bit planes. `prepare_image` bypasses
  it correctly; direct callers get an unusable format. Raise like the BWR/BWY branch.
- 🟡 **4-gray V2 code table missing `EP368_792x528_4GRAY` (0x0048)**
  (`display_palettes.py:32-34` vs `bb_ep.inl:3787,3818`) → swapped mid-grays on that
  panel; `PANELS_4GRAY` also lacks newer 4-gray ids 0x0043/0x0044/0x0046/0x004C.
- 🟡 **`write_config` doesn't pre-validate size** against the firmware's 4096-byte /
  20-chunk limits (`communication.cpp:431`) — oversized configs fail mid-stream with a
  generic ACK mismatch.
- 🟡 **`find_nrf_dfu_device` MAC+1 wraps without carry** (`ota.py:183-184`): last octet
  0xFF → 0x00 with no carry, silent 30 s scan failure on those devices.
- 🟡 **Discovery duplicate-name handling** is order-dependent and can overwrite entries on
  base-name + last-4-MAC collisions (`discovery.py:60-72`).
- 🟡 **Stale comment**: `device.py:475-478` claims direct-write ACKs stay plaintext during
  encrypted sessions; firmware encrypts them (`communication.cpp:161-185`). The code works
  only because encrypted ACKs are exactly ≥31 bytes — zero margin on the length gate.
- 🟡 **Buzzer docstrings cite command 0x0075** (`models/buzzer_activate.py:1,45,75`);
  actual code is 0x0077 (0x0075 is LED STOP). Wire bytes are correct.
- 🟡 **Legacy 65-byte wifi_config** only recognized as the final packet
  (`config_parser.py:134-147`) — fine for legacy firmware behavior, worth a comment.
- 🟡 **Coverage gaps** (not bugs): no Python encoder for LED STOP 0x0075, READ MSD 0x0044,
  CLEAR CONFIG 0x0045, DEEP SLEEP 0x0052; `authenticate`'s status-0x02 retry is dead
  against current firmware (it always clears existing sessions).
- 🟡 **[FW] firmware quirks noted for reference**: security_config bounds check reads the
  2 CRC bytes into `reserved` (`config_parser.cpp:526` uses `configLen` where siblings use
  `configLen - 2`).

---

## 5. Verified correct (audit coverage)

Byte-for-byte agreement with firmware was explicitly verified for:

- **Command set & framing**: all command codes (0x0040/41/42/43/0F/50/51/70/71/72/73/74/76/77),
  2-byte BE command header, chunk framing/ACKs, auto-complete 0x72 handling, 0x73/0x74
  refresh handshake and timeouts, compressed/uncompressed START detection (LE uint32
  size), END-with-etag layout, READ_CONFIG chunk stream.
- **Partial protocol**: 0x76 header (flags + etags BE + x/y/w/h u16 BE), NACK frame and
  all error codes, old‖new plane-major stream, plane sizes, old→PLANE_1/new→PLANE_0,
  MONO 8-pixel alignment, 1-byte partial END.
- **Crypto**: AES-128-CCM tag 12, nonce = `session_id(8)‖counter(8 BE)` → CCM nonce
  bytes 3..16, AAD = command bytes, session-key KDF (label "OpenDisplay session",
  CMAC + AES-ECB), session-id CMAC[:8], challenge CMAC input order, encrypted frame
  layouts both directions, 31-byte minimum, replay-window compliance, plaintext 0xFE/0xFF
  and 0x0043/0x0050 exemptions.
- **Encodings**: 1bpp MSB-first, 2bpp/4bpp packing, BWGBRY value map {0,1,2,3,5,6}
  (confirmed against the firmware's own swatch table), gray4 code tables base/V2 and
  plane bit assignment, BWR/BWY plane semantics (encoding itself; transport broken per C1).
- **Config wire format**: all packet IDs and on-wire sizes on the main parse path, field
  offsets for all 14 packet types, LE endianness (and the deliberate BE `server_port`),
  CRC32-low16 algorithm and coverage, 4096-byte cap, wrapper format.
- **Advertisements**: manufacturer ID 0x2446, v1 layout (temp (t+40)×2, 9-bit battery
  ×10 mV, status bits, button byte, 5-byte touch block), legacy signed temp.
- **Other**: battery interpolation edge cases, landing-URL payload + base64url, rotation
  handled host-side against native panel orientation, compressed-START rejection fallback.

All 439 unit tests pass; the vectorized encoder prototypes (P1–P3) were verified
byte-identical against the current implementations across 9 sizes including
non-byte-aligned widths and full 0–255 palette-index inputs.
