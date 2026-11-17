# Piece 3b: using the drive data — design note

Written 2026-11-16, after piece 3a was merged and proven on the E1002. This note settles the two
points the 3b outline in `../plans/2026-11-16-v3-3-panels-and-migration.md` left open. It is
**approved**: the user confirmed decisions 1–4 as recommended on 2026-11-16 and widened 5 (see below).

Sources: Firmware_Unified `f6d3a83` (`opendisplay_protocol.h` `CMD_DRIVE_WRITE_START` and section
4e, `opendisplay_structs.h` 0xAE / 0xB0, `docs/CONFIG_DRIVEN_DISPLAY.md`), Web `v3/website`
`b7df514` (`ble-common.js`, the only working `0x0085` client), the vendored panel database.

## What 3b adds

1. **Task 4:** 4-colour (BWRY) uploads take their colour-to-code table from the device's config.
2. **Task 5:** `dev.display.show(image, mode="colour16")`: an upload in a named drive mode,
   including modes that drive the panel several times (multi-pass).

Nothing changes for an upload without `mode=`, and nothing changes on a 2.x device.

## Facts this design rests on

| Fact | Source |
|---|---|
| The database's `code_map` equals the library's built-in table for all 13 BWRY panels, including the swapped one (0x001D). | compared 2026-11-16 |
| The alpha firmware's config for panel 0x001C carried `code_map (0, 1, 3, 2)`; the 3.0 database says `(0, 1, 2, 3)`. `OD58D951` now runs 3.0.2 with the database's packets, and a black/white/red/yellow image was correct on glass, so the database's value is the proven one. No test device stores a map that differs from the built-in table any more. | fixture `bg22_alpha_drive_packets.bin`; build session's report, 2026-11-16 |
| Firmware stores `code_map` and does not apply it: hosts send panel codes. | CONFIG_DRIVEN_DISPLAY "code_map" |
| The only multi-pass mode in the database is `colour16`: 7 BWRY panels, mode index 1, 3 passes, scheme 10, temperature policy "advisory". | database |
| All 7 `colour16` palettes (0xB0, 16 entries) are flagged **not measured**: nominal colours, not this unit's. | database |
| A multi-pass transfer is one transfer: `passes` frames back to back, one zlib stream. Each pass frame of scheme 10 is an ordinary 2 bit-per-pixel frame (4 pixels per byte, first pixel in the high bits, rows padded to a byte). | protocol header, `ble-common.js` |
| A pixel's code in pass `p` is bits `[2p+1:2p]` of its palette entry's `codes`. These are panel codes: `code_map` is never applied to them. | structs header |
| A pass boundary is a panel refresh (2.5 s to 35 s) and can fall on any data chunk. With a compressed START that carries data, every refresh can fall before the START is answered (measured: 23 s for two). | protocol header `@timing`, `ble-common.js` |
| START answer: `[00][85][mode][passes][temp_c]`. `mode` is the mode the device runs, which can differ from the one asked. Refusal: `[FF][85][err][mode]`, errors 0x01-0x08. | protocol header |
| `PIPELINED` (flag bit 1) is "Firmware target only" and no client uses it. | protocol header |

## Task 4: `code_map`

When display 0 has a 0xB1 packet, BWRY encoding uses its `code_map`; otherwise the built-in table,
as today. The map travels in `DeviceCapabilities` (new optional field `bwry_codes`), so
`prepare_image` stays free of I/O. A map that is not a permutation of 0..3 raises
`ImageEncodingError`. Applied whenever the config has 0xB1, on any firmware version.

On every test device the stored map now equals the built-in table, so this change alters no
bytes there. The swap itself is proven by a unit test only (a map of `(0, 1, 3, 2)` exchanges the
red and yellow codes); the hardware check is that a red/yellow image on `OD58D951` still draws
correctly after the change.

## Task 5: named modes

**Surface.** `Device.display.show(image, *, mode: str | None = None, refresh=..., fit=..., ...)`.
`mode=None` is today's upload. `"fast"` and `"partial"` are reserved names reached through the
existing refresh byte and partial path, so `mode=` rejects them with a message naming the right
argument. Any other name is looked up, case-insensitively, among the config's 0xAE packets
(`profile.drive_modes`); an unknown name raises `ValueError` listing the names the device has.
Availability depends on the config declaring the mode, not on the firmware version.

**Encoding a multi-pass frame** (new `encoding/multipass.py`, no I/O):

1. Build a `Palette` at run time from the mode's 0xB0 entries (their CIELAB values, in entry
   order) and dither with `epaper-dithering`, as every other upload does. Result: one palette
   index per pixel.
2. For each pass `p`, map index to `entries[index].code_for_pass(p)` and pack 2 bits per pixel.
3. Concatenate the pass frames. Rotation and fit happen before step 1, as today.

A mode with more than one pass and no 0xB0 packet cannot be encoded: `ImageEncodingError`.
A single-pass named mode is encoded as its scheme's normal frame.

**Wire.** `[00][85][mode][refresh][flags][00 00]`, plus `uncompressed_size` and the first
compressed bytes when compressed; data over `0x71`; end with `0x72`. `PIPELINED` is never set.
The session (2.x key or, later, SEC2) wraps it like any other command.

**Timeouts.** Data chunks keep the existing 90 s acknowledgement timeout, which covers one
refresh. START with inline data: `10 s + max(90 s, (passes - 1) x 45 s)`. END: `max(90 s,
passes x 45 s)`. Both are the website's rule.

**Substitution.** If the START answer names another mode than requested, the frames already
encoded do not fit it. The library sends nothing further and raises `DriveModeSubstitutedError`
(requested mode, mode run, temperature). No automatic fall-back upload: the caller decides.

**Refusals.** New typed errors, all `ProtocolError`: `DriveModeTemperatureError` (0x06, carries
the mode's window), `DriveSequenceUnsupportedError` (0x07), and the existing `PanelBusyError`
(0x08, transient) and `NeedsMigrationError` (0x02). Other codes raise `InvalidResponseError`
with the code named.

## Proving it on hardware

- **E1002 `OD4064C9`** (3.0.2, Spectra 6, modes `full` and `fast`): proves the `0x0085` command,
  its answer and the single-pass path. It has no multi-pass mode.
- **BG22 `OD58D951`** (upgraded to 3.0.2 on 2026-11-16; unclaimed, no key, panel 0x001C, modes
  `full` and `colour16` with 3 passes): proves multi-pass on real 3.0 firmware. `0x0085` is
  documented for the Silabs target. BG22 has no pipe transfer, so data goes over `0x71`, which is
  the only path this design uses.

## Decisions (confirmed by the user, 2026-11-16)

1. **Dither with `epaper-dithering` and a run-time palette** (recommended), rather than porting
   the website's own Floyd-Steinberg loop. Same quality controls as every other upload; the
   website's loop would be a second, slower dithering path to maintain.
2. **Substitution raises** (recommended), rather than silently uploading in mode 0. No shipped
   panel uses the "substitute" policy today.
3. **Nominal palettes are used as they are**, with one line in the CLI output saying the colours
   are nominal. Per-unit measurement is out of scope.
4. **`OD58D951` is the multi-pass test device.** A 16-swatch `colour16` image and a red/yellow
   image in the default mode are checked on its glass before 3b merges.
5. **CLI: `opendisplay upload --mode NAME`, and `opendisplay info` lists the drive modes.**
   *(Widened by the user 2026-11-16; the recommendation was the `--mode` flag only.)* One row per
   mode from `profile.drive_modes`: index, name, scheme (the display's scheme when the mode
   inherits it) and passes; the same data under a `drive_modes` key in the JSON output. A config
   without 0xAE shows no such section. It lives in `cli_info.py`'s table, not in `cli.py`.

## Not in 3b

Pipelined drive writes, measuring palettes, partial updates in a named mode, key handover
(piece 4), the upgrade flow (piece 5).
