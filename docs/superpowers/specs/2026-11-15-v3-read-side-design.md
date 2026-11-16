# Piece 2a: reading a 3.0 device

Date: 2026-11-15. Status: draft for approval. Parent: `2026-11-15-v3-architecture-design.md`.

Lines marked **(call)** are choices made while writing this, not things agreed in conversation.

## Intent

Let the library recognise a firmware 3.0 device and read everything it says about itself,
without yet changing how anything is written or drawn. After this piece a caller can tell
"2.x", "3.0 and fine", "3.0 but needs migration", "waiting to be claimed" and "claimed" apart,
and a 3.0 config survives a read-modify-write byte for byte.

## Why split piece 2 **(call)**

The parent doc lists the read side and the first new public names (`opendisplay.connect()`,
`dev.profile`, `dev.config`) as one piece. They are two different kinds of work: pure parsing
that can be released as it lands, and API design that Home Assistant has to agree with.

- **2a (this doc):** protocol read side. Additive, no new top-level entry point.
- **2b (own doc):** `DeviceProfile` and the first part of the new surface, built on 2a.

## What exists today (main at 6781cb5)

- READ_IDENT (0x0046) is not implemented at all.
- The advertisement parser reads status bit 2 as `connection_requested` and ignores bit 3.
- An upload START refusal with a code the library does not know becomes a generic protocol error.
- The config parser stops at the first packet type it does not know and keeps the rest as
  `unparsed_tail`. On the alpha 3.0 tag in the inventory that is 1,499 bytes of drive data.
- Draft PR #198 fixes that last point. It was written against the `Firmware_Silabs` structs, but
  checked on 2026-11-15 the four packet heads are field-for-field the same in `Firmware_Unified`
  main (37, 4, 4 + 8 per entry, and 13 bytes), and the branch still merges cleanly into main.
  It is kept and updated, not rewritten. To verify while updating: the signedness of the
  temperature and CIELAB fields, and the 20-byte sleep-sequence limit.

Source of truth for everything below: `Firmware_Unified` `upstream/main`,
`shared/protocol/opendisplay_protocol.h` and `opendisplay_structs.h`, and
`docs/PROTOCOL_CHANGES_3.0.md`.

## Goals

1. **Device identity.** `OpenDisplayDevice.read_ident()` returns a `DeviceIdent`:
   - `proto_minor`, `fw_target` (enum: ESP32 0x01, nRF54 0x02, EFR32BG22 0x03, nRF52811 0x04,
     nRF52840 0x05, Telink 0x06 provisional; unknown values kept as int),
   - the display records (panel id, width, height, colour scheme),
   - the 3.0 tail when present: `config_driven`, `needs_migration`, `builtin_driver`,
     `max_config`, `max_seq_steps`, `escapes` (WINDOW, FILL),
   - any bytes after the known tail, kept verbatim.
   A device that ends after the records is 2.x: the tail fields are `None`. A device that
   refuses the opcode raises a typed "not supported" error, so callers can treat it as "old".
   The command is plaintext on every firmware, so it goes straight to the transport, not
   through the session, exactly like the firmware-version read.
2. **Advertisement state.** `AdvertisementData` gains:
   - `session_required` (status bit 3; on 2.x this means "encryption enabled"),
   - `claim_mode` (status bit 2), with `connection_requested` kept as an alias,
   - `awaiting_claim` (bit 2 set and bit 3 clear),
   - `unconfigured` (the 11 dynamic bytes are all 0xFF).
   When `unconfigured` is true, the button, touch and sensor slots are not decoded and the
   trackers report no events, so Home Assistant sees no phantom presses.
3. **Typed refusals.** New exceptions, each a subclass of `ProtocolError` so existing
   `except` clauses keep working:
   - `NeedsMigrationError`: upload START refused with 0x08 on 0x70, 0x76 or 0x80.
   - `PanelBusyError`: upload START refused with 0x09 on 0x70 or 0x76. Transient.
   - `ConfigWriteBusyError`: config write refused with 0x01. Transient.
   None is retried by the library in this piece; the message says what the caller should do.
4. **Length-prefixed config packets.** Rebase #198 onto main and onto the `Firmware_Unified`
   structs: packets 0x80 and above are stepped over by length, kept verbatim, written back
   after the legacy packets, and exposed read-only as `display_modes`, `display_luts`,
   `display_calibrations`, `display_panels`. Add the same for 0xB2 (action table) and 0xB3
   (display controller) as opaque-but-named packets. Also parse legacy packet 0x2D (FindMy),
   or step over it by its fixed size, because it currently stops the parser before any
   length-prefixed packet is reached.
5. **`opendisplay info`** shows the identity and the security state when the device has them.

## Non-goals

- Using drive data: no mode selection, no `code_map` applied, no 0x0085, no migration. Piece 3.
- SEC2: no SEC_STATUS, no sessions, no claim. Piece 4.
- `DeviceProfile` and any new top-level API. Piece 2b.
- Reading identity automatically on every connect. It costs a round trip per connection, and
  Home Assistant connects often to battery devices; 2b decides when it is worth it. **(call)**

## Anti-goals

- A 2.x device behaving differently after this piece, apart from the `unconfigured` rule,
  which protocol 2.2 already defines.
- Re-encoding a packet the library only partly understands. Raw bytes stay the single source.

## Approaches considered

1. **One PR.** Simple to describe, but it ties the mechanical parts to the rebase of #198.
2. **Four small PRs, one per goal 1 to 4 (chosen).** Each is independent, each is testable
   without hardware, and they can merge in any order. Goal 5 rides with goal 1.
3. **Generate the models from `opendisplay-protocol`.** The right end state, but the 3.0
   additions are not in that repository yet, so there is nothing to generate from.

## Testing

- Unit tests built from the header layouts, including a 2.x-length reply, a 3.0 reply, a reply
  with unknown trailing bytes, and `display_count = 0`.
- Config round-trip: the bytes read from the alpha 3.0 tag (`OD58D951`) parse with no unparsed
  tail and serialise back identically. The capture goes into `tests/fixtures/` with the key
  field zeroed.
- Hardware, read-only: `read_ident()` against one 2.x device per chip, to learn what each
  answers today.
- Hardware on 3.0: one of the USB boards (the two ESP32-S3 reTerminals or the nRF52840 kit),
  flashed with current `Firmware_Unified` main. The two EFR32BG22 tags have no USB port, so
  they can only be moved by Bluetooth OTA and only recovered with a debugger; they stay on
  what they run now. The alpha tag (`OD58D951`) is still useful as the source of a real config
  with drive packets, read as it is.

## Hardware available (2026-11-15)

The reTerminal E1002 (`OD4064C9`, ESP32-S3) now runs `Firmware_Unified` 3.0.2 from the public
test release, flashed over USB. It has no config and sits in fresh claim mode, which is exactly
the state goals 1 and 2 need. Its real answers are recorded in
`scratch/captures-OD4064C9-fw3.0.2.md` and become test fixtures:

- READ_IDENT `004604010029a00f800003`: `display_count` 0, tail flags 0x29, `max_config` 4000,
  `max_seq_steps` 128, escapes 0x03. The flags byte has bits 3 and 5 set, which are security
  bits; goal 1 keeps the raw flags byte as well as the three named bits.
- Advertisement `ffffffffffffffffffffff860016`: dynamic bytes all 0xFF, status bit 2 set and
  bit 3 clear, so `unconfigured` and `awaiting_claim` are both true.
- READ_FW_VERSION reports 3.0.0 with an all-zero hash from a 3.0.2 image. Not ours to fix, but
  the library must not treat the patch byte as reliable on 3.0 yet.

## Open points

- `fw_target` 0x06 (Telink) and security flag bit 6 are provisional in the firmware.
- The E1002 has no config, so it cannot show a 3.0 reply with display records or
  `needs_migration` set until a config is written. Writing one with drive data is piece 3; a
  2.x-style config without drive data can be written earlier to produce `needs_migration`.
