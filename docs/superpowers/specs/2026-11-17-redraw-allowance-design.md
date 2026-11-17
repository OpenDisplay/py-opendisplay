# Waiting out a panel redraw — design

Status: approved approach (2026-11-17); this document awaits approval.

## Why

A display that is redrawing its panel goes silent. A Spectra/colour panel takes about 30 s; a
BWRY panel about 24 s. During that time the display sends no advertisements and answers no
commands. The library has met this in several places and handles it in each one differently, or
not at all:

- `ConfigView.write` retries its read-back every 10 s, 9 times.
- `migrate` prints a warning about Spectra panels.
- the hardware scripts of piece 4 wrapped every connect in a retry loop;
- `connect()` gives up after a 10 s scan or 5 s of silence. This broke the first claim attempt on
  the E1002 and two later reconnects.

The user wants one rule, applied everywhere, instead of more special cases.

## The firmware contract (not a bug)

The firmware does this on purpose; it is the client's job to wait (Firmware_Unified
`upstream/main` f6d3a83):

- After any boot, Bluetooth starts only once the boot screen is drawn
  (`targets/esp32-idf/src/main.cpp`, "Full BLE after display").
- During a refresh, commands are not processed (`communication.cpp`, `epdRefreshInProgress`), and
  advertising does not restart.
- SECURITY_V2.md, limits table: *"A client allows at least 30 s to find the device again after a
  config write, or retries."*
- A BEGIN or HELLO during a refresh is answered with BUSY (0x0C). **Each BUSY counts in the
  handshake rate limiter**: 10 events per sliding window; RATE_LIMITED means wait 60 s.
- BG22 measurement: no adverts for about 24 s after a config write or at boot.

A silent redrawing display looks exactly like a display that is gone (flat battery, out of
range). Waiting longer therefore makes a missing display slower to fail.

## Goals

1. When the library itself caused a redraw, the next command or connect waits for the display to
   come back, instead of failing at 5 or 10 s. This holds within the same connection and across a
   new connection in the same process.
2. The interactive CLI always allows for a redraw, because a person is watching and has often
   just touched the device.
3. A BUSY reply to a handshake is retried after the pass, spaced so the rate limiter is never hit.
4. One mechanism replaces the special cases: the `ConfigView.write` loop, the migrate warning, and
   the script retry loops.

## Non-goals

- Telling "redrawing" from "gone" when the library did not cause the redraw. That isn't possible
  over the air. In library use, such failures stay fast and come with a clear message.
- Persisting the redraw state across process restarts.
- Per-panel tuning of the wait.
- Changing the firmware.

## Anti-goals (a failure even if it works)

- Home Assistant blocking for a minute on a display that is really gone, because the library
  waited by default.
- Retrying BUSY fast enough to trip RATE_LIMITED.
- A second, parallel retry mechanism surviving somewhere: after this piece, `grep` for ad-hoc
  redraw retries finds none.

## Approaches considered

**Who pays for the wait** when the library did not cause the redraw (decided by the user: **C**):

- A. Wait only when the library knows it caused the redraw; otherwise fail fast with a "may be
  redrawing, try again in 30 s" message. A missing display still fails in about 10 s.
- B. Every connect allows about 60 s. Nothing fails from a redraw, but a missing display takes
  over a minute to fail and blocks Home Assistant's updates.
- **C. A in the library, and the interactive CLI opts into B**, with a "waiting for the display to
  finish redrawing…" line. Picked: the CLI cases were all right after a power cycle by the user,
  and Home Assistant prefers to fail fast and retry at its next update.

**Where the "it is redrawing" knowledge lives** (decided by the user: **1**):

1. **A per-process record keyed by display.** It notes "OD4064C9 redrawing until T" whenever the
   library sends something that makes the panel redraw. `connect()` and the next command consult
   it. It outlives the connection, which covers the reconnect after a claim and Home Assistant's
   next update in the same process. Callers pass nothing. Picked.
2. On the `Device` object. Lost when the connection closes, so it misses the commonest case: the
   claim ends, the display reboots and redraws, and the next connect fails.
3. A caller-owned state object, like `PartialState`. Survives restarts, but adds API surface that
   every caller must carry, for a 60 s window that rarely spans a restart.

## Design

### The allowance

- One constant, **60 s**, the firmware's "a refresh takes up to 60 s"
  (`Uploader.TIMEOUT_REFRESH`'s comment).
- Waiting ends the moment the display answers, so the 60 s is paid only by a display that is
  really gone. That is why it is not tuned per panel. *(Builder's call: name and module.)*

### The record

- A small in-process registry: display identity → monotonic deadline.
  - Identity: the BLE address, plus the name when known. Both are recorded, so a connect by name
    finds a note made by address, and the other way round.
  - Thread- and task-safe for one event loop; no I/O.
- `mark_redrawing(identity, allowance=60 s)` sets the deadline to `now + allowance`; a later mark
  extends it. Nothing clears a note early; it simply expires.
- `redraw_deadline(identity)` returns the deadline, or `None` once it has passed.
- Not public API, unless the builder finds a caller that needs it. In that case, add it to the
  snapshot deliberately.

### What sets a note

The note is set when the library has sent a command after which the firmware redraws. The builder
verifies each row against the firmware sources (ESP32 and BG22 can differ: the BG22 checklist says
"no refresh while a link is open", so its redraw starts at disconnect):

| Library action | Why the display goes silent |
|---|---|
| config write (`write_config`, `ConfigView.write`, migrate) | redraws the boot screen; on ESP32 while connected (seen on the E1002), on BG22 after the disconnect |
| `reboot()` | reboot → boot screen before Bluetooth |
| deep sleep / power-off commands | silent until wake; check whether a redraw follows |
| the end of a claim (CONFIRM success, ABORT/EXIT, attempts exhausted) | claim mode ends → reboot into normal mode → boot screen (§ 7.3, § 13 step 10, U5) |
| `rotate_key()` while SHOW_KEY_ON_SCREEN is set | the device redraws the boot screen with the new key (§ 10.7) |
| unclaim | check: the scrub may redraw or reboot |
| a config write that changes SHOW_KEY_ON_SCREEN | redraws the boot screen (§ 10.7) |

Uploads already wait for their own refresh (`TIMEOUT_REFRESH`, 90 s, on the END/refresh reply).
They set a note only if the builder finds a path where the reply comes before the refresh ends.

### What consults a note

- **`connect()` / `OpenDisplayDevice.__aenter__`:**
  - Before the deadline, discovery keeps scanning until the deadline instead of stopping at its
    timeout.
  - A silent first command (`read_firmware_version`) is retried, each with its normal timeout,
    until the deadline.
  - Log once at INFO: "OD4064C9 may be redrawing; waiting up to N s".
  - Past the deadline: today's behaviour.
- **The next command on a live connection** (the `ConfigView.write` read-back case): a timeout
  before the deadline retries the read, instead of raising. Place this at one level (channel or
  facade), so callers don't each carry a loop. The `ConfigView.write` loop is then deleted.
- **SEC2 BUSY (`Sec2BusyError`)** from HELLO or BEGIN: retried every 10 s while a deadline is
  pending, or for one allowance when none is. That is at most 6 BUSY events a minute, under the
  limiter's 10. `RateLimitedError` is never retried automatically; its message says to wait 60 s.

### Fail-fast message (library, no note)

When discovery finds nothing or the first command is silent, and no note is pending, the error
says: "The display did not answer. If it is redrawing (some colour panels take about 30 s after a
power-up, reboot or config change), try again in 30 s." Same error types as today.

### The CLI

- Every CLI command that connects allows for a redraw by default: it sets a note for the target
  with the allowance before connecting. This reuses the same path; no second mechanism.
- While waiting, the CLI prints "Waiting for the display to finish redrawing…" once.
- `--no-wait` turns this off (for scripts that want fast failure). *(Builder's call: the flag's
  exact name.)*
- The migrate warning about Spectra panels goes; the waiting line replaces it.

### Library API

- No new required parameter. `connect()` gains an optional way to ask for the CLI's behaviour,
  for callers like the CLI. Builder's call between `connect(..., expect_redraw=True)` and the
  CLI calling the internal `mark_redrawing` directly. Prefer the parameter if a non-CLI caller
  plausibly wants it; a Home Assistant config flow right after a power cycle is one.
- Typed errors unchanged.

## Testing

- Unit tests use a fake clock or a short allowance. No test waits 60 s.
- With a note: discovery that finds the display on the third scan succeeds; a first command
  silent twice, then answered, succeeds.
- With a note expired, or no note: today's fast failure, with the new message.
- Config-write read-back: silent for 3 reads, then answered, succeeds with no loop left in
  `ConfigView.write`.
- BUSY: retried at ≥ 10 s spacing (assert on the fake clock), at most 6 per minute;
  RATE_LIMITED: not retried.
- Each row of "What sets a note" has a test that the action sets it.
- CLI: waits by default, prints the line once; `--no-wait` fails fast.
- Mutation-check every rule, as in earlier pieces.
- Hardware, with the user's go-ahead for each write, on the E1002 (Spectra) and the BG22
  OD58D951:
  - a config write, then an immediate reconnect;
  - a reboot, then an immediate reconnect;
  - the CLI right after a power cycle;
  - claim, then reconnect, in one CLI run;
  - a BUSY handshake, if it can be provoked.

## Left to the builder

- Module placement and names (constant, registry, CLI flag).
- Verifying each row of "What sets a note" against the firmware, and dropping or adding rows to
  match.
- Whether ClaimFlow should send `SEC_CLAIM EXIT` after a successful CONFIRM (spec § 13 step 10)
  rather than leaving the reboot to the disconnect (U5). Either way the end of a claim sets a
  note.
- Removing every ad-hoc retry found by a search of `src/` and `scratch/` hardware scripts. Delete
  the scratch scripts' loops, or leave them only where they test something else.
