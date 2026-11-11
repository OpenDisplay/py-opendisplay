# py-opendisplay on epaper-dithering 7

Status: draft for review · 2026-11-10

## Why

epaper-dithering 7 (PR #100) removes `ColorScheme`, `ColorPalette` and every built-in palette. It
takes a runtime `Palette` instead, and `tone`/`gamut` become 0.0–1.0 strengths with no `"auto"`.
py-opendisplay imports all of those today (`display_palettes.py`, `device.py`, `encoding/`,
`models/`, `partial.py`, `__init__.py`), so it can't move to 7 without owning that data itself.

## Goal and success test

Today's tags upload exactly as they do now, on epaper-dithering 7. The unit suite passes, and an
upload to a real tag on today's firmware looks as it did on 6.x, apart from the warm-tone shift
documented in epaper-dithering's migration notes.

## Non-goals

- **Drive modes, `0x0085`, 16-colour uploads.** That is Step 2. It waits for the canonical
  protocol (`opendisplay-protocol`) to settle. Jonas's `origin/feat/od-epd-driver` branch is
  reference only, not to be merged.
- **Palettes read from the tag's config.** Also Step 2. Step 1 only shapes the lookup so one can
  slot in later.
- **The Home Assistant integration.** It stays pinned to epaper-dithering 6 until this release.

## Anti-goals

- **Any tag uploads differently** beyond the palette changes listed under "Palettes".
- **A palette's index order changes.** The encoders (`bitplanes.py`, the BWRY and 4-gray code
  tables, partial updates) assume today's order per scheme, for example BWRY black=0, white=1,
  yellow=2, red=3.

## Constraints

- Depends on epaper-dithering 7. Develop against PR #100; release only after 7 is published.
- Major py-opendisplay release: `tone`/`gamut` lose their string values and `ColorScheme` is no
  longer epaper-dithering's. release-please handles the version.

## What was decided

### ColorScheme

py-opendisplay defines its own `ColorScheme` (`IntEnum`, wire values as in `opendisplay-protocol`)
and exports it where it exports epaper-dithering's today. `from_value` keeps working.

### Palettes

One function returns the `Palette` for a display: a measured palette for its panel ID and scheme if
one exists, else the idealised palette for its scheme. `use_measured=False` skips the measured
step, as today.

- **Idealised palettes:** one per scheme, shipped as package data, taken from epaper-dithering's
  `examples/palettes/ideal_*.json` and reordered where needed to today's index order. Pure inks are
  named, so solid ink colours stay solid.
- **Measured palettes:** panel 35 (Spectra 7.3") and panel 66 (13.3" split) both use the 7.3"
  **v2** measurement (`SPECTRA_7_3_6COLOR_V2`); panel 55 (`BWRY_3_97`, EP397YR) is recovered from
  epaper-dithering 6.x history. v1 is dropped: its capture clipped three inks to a zero channel
  and implies a white/black contrast the panel can't produce (py-opendisplay 85eeee2). This
  changes panel 35's output, which used v1.
- **Dropped:** `MONO_4_26` (panel 39) and `SOLUM_BWR` (panel 33) were never measured, so those
  panels fall back to the idealised palette (decided with the owner: a visible change, accepted;
  the panels get measured later).
- A test pins each scheme's index order and ink names.

### Known output changes from epaper-dithering 7 itself

epaper-dithering 7 normalises measured palettes to the panel's white, so panels with a measured
palette render brighter than on 6.x even where the palette data is unchanged. On a random-colour
test image with no dithering, 49.7% of panel 55's pixel indices differ from 6.x (white chosen far
less often). This is the library's documented behaviour, not a py-opendisplay change, but the
release notes must say so: measured-palette panels (35, 55, 66) look different, and `tone` restores
some of the 6.x shadow lift. Solid-ink content (dashboards) is byte-identical.

### Dithering

`dither_image(image, palette, DitherOptions(...))`. `dither_mode`, `dbs` and the other knobs pass
through. `tone`/`gamut` become `float` (0.0–1.0, default 0.0, as today); `"auto"` and `"off"` are
no longer accepted. The CLI follows.

## Left to the builder

- Module layout and names, the data file layout inside the package.
- Whether the measured palettes ship as JSON files or Python constants.
