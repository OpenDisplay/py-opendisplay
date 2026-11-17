# Waiting Out a Panel Redraw — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `EXECUTOR_RULES.md` in this folder first.

**Goal:** After the library makes a display redraw, connects and replies wait for it instead of failing at 5–10 s; the CLI always allows for a redraw; SEC2 BUSY is retried safely; the ad-hoc retries are removed.

**Architecture:** A new pure module `opendisplay/redraw.py` holds an in-process record "display → redrawing until T", keyed by every identity the library knows for the display (name, BLE address, host). The library's own transports (`BLEConnection`, `TcpTransport`) stretch a reply's timeout to that deadline. The facade's name resolution and BLE connect keep trying until it. The actions that make the firmware redraw set a note. Because the firmware **queues** a command that arrives during a refresh (`loop()` is blocked for the whole refresh; Firmware_Unified `communication.cpp`), a pending command is never re-sent: its reply is waited for longer.

**Tech Stack:** Python ≥ 3.13, asyncio, bleak; pytest (asyncio_mode=auto); uv, prek.

**Spec:** `docs/superpowers/specs/2026-11-17-redraw-allowance-design.md`

## Global Constraints

- Allowance: `REDRAW_ALLOWANCE_S = 60.0` (the firmware's "a refresh takes up to 60 s").
- BUSY retry spacing: `BUSY_RETRY_S = 10.0`, which keeps BUSY at ≤ 6 events per minute against the device limiter's 10. `RateLimitedError` is never retried automatically.
- Waiting ends as soon as the display answers; a note only ever extends, never clears early, and expires on its own.
- No new public API. `tests/unit/data/public_api.json` must not change. `redraw` stays internal; `connect(expect_redraw=…)` is deferred to piece 6 (Home Assistant), when a caller needs it.
- Without a note, library failures stay as fast as today and carry this hint, verbatim: `If the display is redrawing (some colour panels take about 30 s after a power-up, reboot or config change), try again in 30 s.`
- CLI waiting line, verbatim, printed at most once per command: `Waiting for the display to finish redrawing...`
- No test sleeps more than 0.5 s. Use the clock and sleep seams in `redraw.py`.
- Mutation-check every rule (`PYTHONDONTWRITEBYTECODE=1`), as in earlier pieces. Commit with `uv run git commit`; stage files by name.

## Review Focus

1. **A display that never comes back after a write** must fail at the deadline (≈ 60 s), not hang. Task 3 pins "read with an expired note times out at the normal timeout" and "a pending note never makes a read wait past the deadline".
2. **A connect by name after a note made by address, or the other way round**, must still wait. Task 2 pins lookups across identities; Task 4 pins that name resolution links the name to the address.
3. **A command that is not idempotent, sent during a redraw** (a config write chunk), must not be sent twice. Task 3 pins that the wait is one longer read, with no second `write_command`.
4. **BUSY on every attempt** must stop at the deadline and never exceed 6 attempts a minute. Task 6 pins the spacing and the stop.
5. **A caller-provided transport** (tests, a custom `Transport`) has no redraw support and must behave exactly as today. Task 3 pins that `redraw_keys` absent means no change.

---

### Task 1: Check which actions make the firmware redraw

**Files:**
- Modify: `docs/superpowers/specs/2026-11-17-redraw-allowance-design.md` (the "What sets a note" table)

No code. Read Firmware_Unified `upstream/main` (fetch first; f6d3a83 or newer). Check `targets/esp32-idf` and `targets/efr32bg22-slc` for each row:
- config write;
- `reboot()`;
- `deep_sleep()` / `power_off()`;
- claim END: CONFIRM success then disconnect (U5), EXIT, ATTEMPTS_EXHAUSTED;
- `rotate_key()` with SHOW_KEY_ON_SCREEN set;
- unclaim;
- whether the upload's final ACK comes after the refresh.

- [ ] **Step 1:** For each row, record in the table: redraws (yes/no), when (while connected / after disconnect / after reboot), and the file:line seen.
- [ ] **Step 2:** Mark rows "no redraw" as **not** setting a note. Expected outcomes to confirm or correct: deep sleep and power-off do not set a note, because a note would only make the next connect wait 60 s for a device that sleeps longer; the upload does not set one.
- [ ] **Step 3:** Commit: `git add docs/superpowers/specs/2026-11-17-redraw-allowance-design.md && uv run git commit -m "docs: record which commands make the firmware redraw"`.

Tasks 5 and 6 set notes only for the rows this task confirms.

### Task 2: The redraw record

**Files:**
- Create: `src/opendisplay/redraw.py`
- Test: `tests/unit/test_redraw.py`

**Interfaces:**
- Produces:
  - `REDRAW_ALLOWANCE_S: Final = 60.0`, `BUSY_RETRY_S: Final = 10.0`
  - `REDRAW_HINT: Final[str]` (the Global Constraints hint)
  - `mark_redrawing(*keys: str | None, allowance: float = REDRAW_ALLOWANCE_S) -> None`: sets every non-empty key's deadline to `max(existing, now + allowance)`.
  - `link(*keys: str | None) -> None`: gives every key the latest deadline any of them has (name resolution uses it).
  - `remaining(*keys: str | None) -> float`: seconds until the latest pending deadline among the keys, `0.0` if none.
  - `wait_timeout(keys: Sequence[str | None], timeout: float) -> float`: `max(timeout, remaining(*keys))`.
  - `_clock: Callable[[], float] = time.monotonic`, `_sleep = asyncio.sleep`: module-level seams that tests patch.
  - `_reset() -> None`: test helper; clears the record. An autouse fixture in `tests/unit/conftest.py` calls it.
- Keys are compared case-insensitively (BLE addresses arrive in either case).

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_redraw.py`, with a fake clock (`monkeypatch.setattr(redraw, "_clock", lambda: t[0])`):
  - `test_a_note_makes_its_keys_wait_until_the_deadline`: `mark_redrawing("OD4064C9", "AA:BB")`; `remaining("aa:bb") == 60.0`; at t=+59, `remaining("OD4064C9") == 1.0`; at t=+61, `0.0`.
  - `test_a_later_mark_extends_and_never_shortens`: mark with 60, then at t=+10 mark with 5; `remaining(...) == 50.0`.
  - `test_link_shares_the_latest_deadline`: mark `"OD4064C9"`; `link("OD4064C9", "AA:BB")`; `remaining("AA:BB") == 60.0`.
  - `test_empty_and_none_keys_are_ignored`: `mark_redrawing(None, "")` leaves `remaining(None) == 0.0`.
  - `test_wait_timeout_is_never_shorter_than_the_normal_timeout`: no note → `wait_timeout(["x"], 5.0) == 5.0`; note 60 → `60.0`; note at t=+58 → `5.0`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_redraw.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `src/opendisplay/redraw.py` as specified: a module-level `dict[str, float]` of deadlines; no I/O, no logging.
- [ ] **Step 4:** Run the same command. Expected: 5 passed.
- [ ] **Step 5:** Commit `feat: an in-process record of displays that are redrawing` (`src/opendisplay/redraw.py tests/unit/test_redraw.py tests/unit/conftest.py`).

### Task 3: The library's transports wait for a reply while a note is pending

**Files:**
- Modify: `src/opendisplay/transport/connection.py` (`BLEConnection.read_response`, around line 489)
- Modify: `src/opendisplay/transport/ip.py` (`TcpTransport.read_response`, around line 167)
- Modify: `src/opendisplay/device/facade.py` (`_attach`, line 378)
- Test: `tests/unit/test_redraw_transport.py`

**Interfaces:**
- Consumes: `redraw.wait_timeout`.
- Produces:
  - `BLEConnection.redraw_keys: tuple[str, ...]` and `TcpTransport.redraw_keys: tuple[str, ...]`, default `()`;
  - each `read_response(timeout)` uses `redraw.wait_timeout(self.redraw_keys, timeout)` for its wait. For TCP, that covers the header wait only; the body keeps its own timeout.
  - `_attach` sets `transport.redraw_keys = (self._device_name, self.mac_address, self._host)` filtered of `None`, **only** if the transport has the attribute (a caller-provided `Transport` is left alone).
  - Not part of the `Transport` Protocol.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_ble_read_waits_past_its_timeout_while_a_note_is_pending`: a `BLEConnection` with a real `_notification_queue`, `redraw_keys=("AA:BB",)`, a note of 0.3 s, a reply put on the queue after 0.15 s. `await read_response(timeout=0.05)` returns the reply.
  - `test_a_ble_read_without_a_note_times_out_as_before`: same, no note → `BLETimeoutError` after ≈ 0.05 s.
  - `test_a_note_never_makes_a_read_wait_past_its_deadline`: note 0.2 s, no reply → raises within 0.4 s.
  - `test_the_wait_is_one_longer_read_not_a_resend`: through `OpenDisplayDevice` with a recording transport that has `redraw_keys`, a note, and a reply delayed 0.15 s. `read_firmware_version()` succeeds and `write_command` was called once.
  - `test_a_caller_transport_without_redraw_keys_is_unchanged`: `FakeTransport` gets no attribute from `_attach`.
  - The TCP equivalent of the first test, using the existing TCP test fixtures in `tests/unit/test_transport_ip*.py`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_redraw_transport.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement as in Interfaces. The `BLETimeoutError` message still names the caller's `timeout` when no note is pending.
- [ ] **Step 4:** Run it, then `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: wait for a reply while the display is redrawing`.

### Task 4: Connect waits for a display that is redrawing

**Files:**
- Modify: `src/opendisplay/device/facade.py` (`__aenter__`: name resolution, lines 268–286; `await self._conn.connect()`, line 322)
- Modify: `src/opendisplay/device/api.py` (`Device.__aenter__`, line 307)
- Test: `tests/unit/test_redraw_connect.py`

**Interfaces:**
- Consumes: `redraw.remaining`, `redraw.link`, `redraw.REDRAW_HINT`, `redraw._sleep`.
- Produces: no new names.

Behaviour:
- **Name resolution:** while `remaining(name) > 0`, repeat `discover_devices(timeout=min(discovery_timeout, remaining))` until the name is found, with one INFO log: `"%s may be redrawing; waiting up to %.0f s"`. On success, call `redraw.link(name, address)`.
- **Not found:** with no note, raise as today; otherwise raise at the deadline. Either way the message gains `REDRAW_HINT`.
- **BLE connect:** a `BLEConnectionError` from `self._conn.connect()` is retried after `redraw._sleep(2.0)` while `remaining(name, address) > 0`; then it re-raises with `REDRAW_HINT` appended.
- **`Device.__aenter__`:** an `OpenDisplayTimeoutError` from the first `read_firmware_version()` with no note pending is re-raised with `REDRAW_HINT` appended (same type).

- [ ] **Step 1: Write the failing tests**, patching `opendisplay.discovery.discover_devices`, a short note, and `redraw._sleep` to a no-op:
  - `test_resolution_keeps_scanning_while_a_note_is_pending`: found on the third scan; three calls.
  - `test_resolution_without_a_note_fails_after_one_scan_with_the_hint`: `BLEConnectionError`, `REDRAW_HINT` in the message, one call.
  - `test_resolution_links_the_name_to_the_address`: after a found name, `remaining(address) > 0`.
  - `test_ble_connect_is_retried_while_a_note_is_pending`: a transport whose `connect()` fails twice, then succeeds.
  - `test_a_silent_first_command_without_a_note_carries_the_hint`: `opendisplay.connect(transport=FakeTransport([]))` raises `OpenDisplayTimeoutError` with `REDRAW_HINT`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_redraw_connect.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement as above. Keep `__aenter__` within the pylint limits by moving resolution into a private `_resolve_name()`.
- [ ] **Step 4:** Run it, then `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: connect waits for a display that is redrawing`.

### Task 5: Actions that make the panel redraw set a note; the ad-hoc read-back loop goes

**Files:**
- Modify: `src/opendisplay/device/facade.py` (`write_config`, line 1141; `reboot`, line 762)
- Modify: `src/opendisplay/device/api.py` (`ConfigView.write`, lines 103–123; delete `_REDRAW_WAIT_S`, `_REDRAW_RETRIES`)
- Modify: `src/opendisplay/device/security.py` (after a successful claim CONFIRM, `use_without_key`, `rotate_key` when SHOW_KEY_ON_SCREEN is set in the cached config, and `unclaim` if Task 1 confirmed it)
- Test: `tests/unit/test_redraw_marks.py`

**Interfaces:**
- Consumes: `redraw.mark_redrawing`. The keys are the same tuple `_attach` built (`self._device_name`, `self.mac_address`, `self._host`). Expose it once as `OpenDisplayDevice._redraw_keys() -> tuple[str, ...]`, a private method used by `security.py`.
- Produces: no new names.

Rules (adjusted to Task 1's findings):
- Mark after the device **acknowledged** the action, never before. A refused write sets no note.
- Claim: a mark after `confirm()` succeeds; after `use_without_key()` (EXIT); and when the device refuses with `ClaimAttemptsExhaustedError`.
- A successful claim does **not** send `SEC_CLAIM EXIT` on its own: the spec's flow (§ 13 step 9) writes the secrets in that same session first. The device reboots when the connection drops (U5), so the note covers the reconnect.
- `ConfigView.write`: `await legacy.write_config(config)` then `await self._device.refresh()`, with no loop. The note makes the read-back wait.

- [ ] **Step 1: Write the failing tests:** one per confirmed row, asserting `redraw.remaining(name) > 0` after the action and `== 0` after a refused one (`ConfigWriteBusyError` for the write). Plus:
  - `test_config_write_reads_back_after_a_silent_redraw`: through `opendisplay.connect` with a transport whose read-back reply arrives after 0.2 s, with the allowance patched to 0.5 s. It succeeds with one read request, not several.
  - `test_config_view_has_no_retry_loop_left`: `not hasattr(api, "_REDRAW_RETRIES")`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_redraw_marks.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the marks and delete the loop and its constants. Update `ConfigView.write`'s docstring: the read-back waits out a redraw the write caused (up to `REDRAW_ALLOWANCE_S`).
- [ ] **Step 4:** Run it, then `uv run pytest -q`. Expected: all pass. The existing config-write tests that scripted retries are rewritten to the single read; say so in the commit body.
- [ ] **Step 5:** Commit `feat: commands that make the panel redraw note it, and the read-back waits instead of retrying`.

### Task 6: SEC2 BUSY is retried after the pass

**Files:**
- Modify: `src/opendisplay/device/security.py` (the `authenticate` calls in `_open_session`, `_install`'s renew, `rotate_key`'s reauthenticate)
- Modify: `src/opendisplay/sec2/claim.py` (`ClaimFlow.begin`)
- Modify: `src/opendisplay/redraw.py` (add the helper)
- Test: `tests/unit/sec2/test_sec2_busy.py`

**Interfaces:**
- Produces: `async def retry_busy(op: Callable[[], Awaitable[T]], *keys: str | None) -> T` in `redraw.py`.
  - Calls `op()`. On `Sec2BusyError`, if no note is pending for the keys, it marks one (the allowance starts at the first BUSY).
  - Then `await _sleep(BUSY_RETRY_S)` and retries while the note is pending, re-raising the last `Sec2BusyError` at the deadline.
  - Any other exception, `RateLimitedError` included, propagates at once.

- [ ] **Step 1: Write the failing tests**, with fake clock and sleep, against `RefDevice` with a `busy_replies: int` knob added (BUSY for the first N HELLO/BEGIN, then normal):
  - `test_busy_hello_is_retried_until_the_session_opens`: N=2; the session opens; the sleeps recorded are `[10.0, 10.0]`.
  - `test_busy_retries_stop_at_the_deadline`: N=99; `Sec2BusyError` raised; at most 6 attempts within the 60 s.
  - `test_rate_limited_is_never_retried`: a `RATE_LIMITED` reply raises `RateLimitedError` after one attempt.
  - `test_busy_begin_is_retried`: the same, for `ClaimFlow.begin`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/sec2/test_sec2_busy.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement `retry_busy` and wrap the four call sites.
- [ ] **Step 4:** Run it, then `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: retry a busy SEC2 handshake after the panel's pass, within the rate limit`.

### Task 7: The CLI always allows for a redraw

**Files:**
- Modify: `src/opendisplay/cli_common.py` (`_add_device_options`, line 95: add `--no-wait`)
- Modify: `src/opendisplay/cli.py` (`main`, line 822)
- Modify: `src/opendisplay/cli_migrate.py` (delete the Spectra warning, line 339)
- Modify: `scratch/hw_sec2_claim.py`, `scratch/hw_sec2_upload.py`, `scratch/hw_sec2_negative.py`, `scratch/hw_sec2_unclaim.py` (untracked; drop their retry wrappers)
- Test: `tests/unit/test_cli_redraw.py`

**Interfaces:**
- Consumes: `redraw.mark_redrawing`.
- Produces:
  - `--no-wait` (`store_true`, help: `Fail at once if the display does not answer, instead of allowing about 60 s for it to finish redrawing`).
  - In `main()`, after `parse_args`: if `getattr(args, "no_wait", True) is False`, call `redraw.mark_redrawing(args.device, args.host)`.
  - The waiting line is printed by an INFO-log handler or a one-shot hook: the builder's call. It is printed only when a wait actually happens, at most once.

- [ ] **Step 1: Write the failing tests:**
  - `test_cli_marks_the_target_before_connecting`: parse `["info", "--device", "OD4064C9"]`, run `main` with `args.func` patched; `redraw.remaining("OD4064C9") > 0`.
  - `test_no_wait_marks_nothing`: same with `--no-wait`; `== 0`.
  - `test_the_waiting_line_is_printed_once_and_only_when_waiting`: name resolution found on the second scan → the line appears in stderr exactly once; found on the first scan → it does not appear.
  - `test_migrate_no_longer_prints_the_spectra_warning`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/test_cli_redraw.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Remove the scratch scripts' `with_retries` / `retry` wrappers (not committed; they are untracked).
- [ ] **Step 4:** Run it, `uv run pytest -q`, then `UPDATE_API_SNAPSHOT=1 uv run pytest tests/unit/test_public_api_snapshot.py -q && git diff --exit-code tests/unit/data/public_api.json`. Expected: all pass, no snapshot diff.
- [ ] **Step 5:** Search for leftovers: `grep -rniE "redraw|retry|retries" src/opendisplay | grep -v redraw.py`. Expected: only uses of `redraw.*`, the BLE cache retries in `transport/connection.py`, and the SEC2 handshake restart. Commit `feat(cli): allow for a redraw by default, with --no-wait to fail fast`.
- [ ] **Step 6:** `uv run prek run --all-files`. Expected: clean.

### Task 8: Hardware, with the user

Each write needs the user's go-ahead (EXECUTOR_RULES). Devices: E1002 OD4064C9 (Spectra, ESP32; claimed, key in `~/.config/opendisplay/keys.json`) and BG22 OD58D951.

- [ ] **Step 1:** E1002: `opendisplay write-config` (back up first), then `opendisplay info` within 5 s. Expected: info succeeds after the waiting line.
- [ ] **Step 2:** E1002: `opendisplay reboot`, then `info` at once. Expected: succeeds.
- [ ] **Step 3:** User power-cycles the E1002; `info` at once. Expected: the waiting line, then success. Repeat with `--no-wait`. Expected: fast failure with the hint.
- [ ] **Step 4:** BG22: the same as Step 1 (its redraw starts after the disconnect).
- [ ] **Step 5:** Claim, then reconnect in one CLI run, only if the user wants to unclaim and reclaim the E1002.
- [ ] **Step 6:** Record the results in the ledger `.superpowers/sdd/2026-11-17-redraw/progress.md`, then ask the user before pushing.
