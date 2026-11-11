# Adding a palette

A palette tells the dithering which colours a panel can actually show. py-opendisplay ships one
idealised palette per colour scheme (pure black, white, red, …) and measured palettes for the
panels that have been measured. Measured palettes give noticeably better photos, so adding one
for your panel is worthwhile.

Palettes are [`epaper-palette`](https://github.com/OpenDisplay/epaper-dithering#palettes) JSON
files in [`src/opendisplay/palettes/`](../src/opendisplay/palettes/), mapped to panels in
[`display_palettes.py`](../src/opendisplay/display_palettes.py).

## The two rules

**1. Entries must be in the scheme's index order.** The encoders read a palette index as a fixed
ink: in BWRY, index 2 is always sent to the panel as yellow. A file in the wrong order puts
colours on the wrong ink, and nothing raises an error.

| Scheme | Entry order |
| --- | --- |
| `MONO` | black, white |
| `BWR` | black, white, red |
| `BWY` | black, white, yellow |
| `BWRY` | black, white, yellow, red |
| `BWGBRY`, `BWGBRY_SPLIT` | black, white, yellow, red, blue, green |
| `SEVEN_COLOR` | black, white, yellow, red, blue, green, orange |
| `GRAYSCALE_4` | black, gray1, gray2, white |
| `GRAYSCALE_16` | black, gray1 … gray14, white |

The idealised files (`ideal_*.json`) are the reference: copy their order and ink names.

**2. Every pure ink is named, with its nominal colour.** The name and nominal (`"black"`,
`"#000000"`) are what make a dashboard's `#000000` text come out as solid black instead of being
dithered against the measured black. Use the names and nominals from the idealised file for your
scheme.

## Measuring a panel

Measure each ink as a solid fill, for example with
[epaper-colorcal](https://github.com/OpenDisplay/epaper-colorcal) and a CR30 colorimeter.
epaper-colorcal currently prints sRGB triples (D65/2°) in a Rust block meant for
epaper-dithering 6.x; take the three numbers per ink from it.

Find the panel's ID with `opendisplay info`: the `Panel` line ends with it in hex, e.g.
`(0x0021)` is panel 33.

## Writing the file

Build the palette from the measured sRGB values and save it. epaper-dithering normalises every
entry to the measured white, so you don't need to.

```python
from pathlib import Path

from epaper_dithering import Palette

palette = Palette.from_srgb(
    [
        # (measured sRGB, ink name, nominal colour) in the scheme's entry order
        ((24, 21, 30), "black", "#000000"),
        ((176, 180, 178), "white", "#ffffff"),
        ((128, 30, 26), "red", "#ff0000"),
    ],
    measured=True,
).with_source("Solum 2.6in BWR (ep26r_152x296), CR30, 2026-12-01, 23 °C")

Path("src/opendisplay/palettes/solum_2in6_bwr.json").write_text(palette.to_json() + "\n")
```

The `source` is free text; say what panel, which instrument, when, and at what temperature.

If you have absolute CIELAB (D65/2°) instead of sRGB, use
`Palette.from_absolute_lab(entries, white_index=1, measured=True)` with
`epaper_dithering.PaletteEntry` objects.

## Registering it

1. Add a line to `MEASURED_PALETTE_FILES` in `display_palettes.py`, keyed by panel ID and scheme:

   ```python
   # Solum 2.6" BWR (ep26r_152x296).
   (33, ColorScheme.BWR): "solum_2in6_bwr.json",
   ```

2. Add the panel to `MEASURED` in `tests/unit/test_display_palettes.py`. That checks the file is
   marked measured, has the scheme's ink names in order, and that white is normalised.

3. Run `uv run pytest`. `test_palette_files_ship_with_the_package` fails if a referenced file is
   missing or a file in the folder isn't referenced.

4. Upload a photo and a dashboard to the panel and compare with `use_measured_palettes=False`.
   Dashboard colours should be identical; photos should look closer to the original.
