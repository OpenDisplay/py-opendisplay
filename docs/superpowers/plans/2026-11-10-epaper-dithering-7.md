# py-opendisplay on epaper-dithering 7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move py-opendisplay from epaper-dithering 6.1.1 to 7 with no change for today's tags beyond the palette changes the spec lists.

**Architecture:** py-opendisplay takes ownership of what epaper-dithering 7 dropped: a wire-value `ColorScheme` (in `models/enums.py`) and a palette table (in `display_palettes.py`) built with `Palette.from_srgb`. Task 1 is a mechanical import swap, done first on epaper-dithering 6 so it can be reviewed on its own; Task 2 switches the dependency, the dither call sites and `tone`/`gamut` in one step, because importing 7 breaks every 6.x import at once.

**Tech Stack:** Python ≥ 3.13, uv, pytest, epaper-dithering 7 (PR #100, branch `feat/generic-palettes`), Pillow.

**Spec:** `docs/superpowers/specs/2026-11-10-epaper-dithering-7-design.md`

## Global Constraints

- Branch `feat/epaper-dithering-7` cut from `origin/main` (not from `feat/sendspin-artwork`, which has the user's uncommitted work), in a worktree.
- Use `uv`, never `pip`. Stage explicit file names; never `git add -A` / `git add .`.
- Never bump `version`; release-please does. Conventional commits; breaking commits use `feat!:` with a `BREAKING CHANGE:` footer.
- Lint before each commit: `uv run prek run --all-files`.
- Do not release until epaper-dithering 7 is on PyPI; the local path source added in Task 2 must be replaced by a pin before merge (see "Before merge").
- Palette index order per scheme must stay exactly as in 6.x (e.g. BWRY black=0, white=1, yellow=2, red=3).

## Review Focus

1. A dashboard pixel exactly `#000000` / `#ff0000` on a measured panel (55, BWRY) must come out as the black / red index, not a dithered mix. → Task 2 test `test_pure_inks_stay_solid_on_measured_bwry`.
2. Solid ink stripes through the ideal palette with `DitherMode.NONE` must give indices `0..n-1` in 6.x order, so encoders see the same bytes. → Task 2 test `test_ideal_index_order_matches_6x`.
3. A caller still passing `tone="auto"` / `gamut="off"` gets epaper-dithering 7's clear error, not a silent default. → Task 2 test `test_string_strength_rejected`.
4. A caller passing a 6.x integer dither mode (`dither_mode=1`) gets a `ValueError` naming the mode, not a crash deep in the core. → Task 2 test `test_int_dither_mode_rejected`.
5. A config with color_scheme 9 or 99 still raises `ImageEncodingError` mentioning `(0–8)`. → existing tests in `tests/unit/test_device_upload_flow.py`, kept green in Task 1.

---

### Task 1: Own `ColorScheme`

**Files:**
- Modify: `src/opendisplay/models/enums.py` (add `ColorScheme`)
- Modify: every `from epaper_dithering import ColorScheme` in `src/` (`__init__.py`, `device.py`, `display_palettes.py`, `partial.py`, `encoding/bitplanes.py`, `encoding/images.py`, `models/capabilities.py`, `models/config.py`) and `tests/unit/` (16 files)
- Modify: `src/opendisplay/display_palettes.py` (temporary bridge to 6.x)
- Modify: `src/opendisplay/device.py:219-225` (drop the GRAYSCALE_8 guard)
- Test: `tests/unit/test_color_scheme.py`

**Interfaces:**
- Produces: `opendisplay.models.enums.ColorScheme(IntEnum)` with `MONO=0, BWR=1, BWY=2, BWRY=3, BWGBRY=4, GRAYSCALE_4=5, GRAYSCALE_16=6, SEVEN_COLOR=7, BWGBRY_SPLIT=8`, and `ColorScheme.from_value(value: int) -> ColorScheme` raising `ValueError(f"Invalid color scheme value: {value}")`. Re-exported as `opendisplay.ColorScheme`. No `GRAYSCALE_8`, no `.palette` / `.accent_color`.

- [ ] **Step 1: Set up the worktree** — `git worktree add .worktrees/epaper-dithering-7 -b feat/epaper-dithering-7 origin/main` (check `.worktrees` is ignored), `uv sync --all-extras`, `uv run prek install`, `uv run pytest -q`. Expected: all pass on epaper-dithering 6.1.1.

- [ ] **Step 2: Write the failing test** in `tests/unit/test_color_scheme.py`:

```python
from opendisplay import ColorScheme

def test_wire_values():
    assert [(s.name, int(s)) for s in ColorScheme] == [
        ("MONO", 0), ("BWR", 1), ("BWY", 2), ("BWRY", 3), ("BWGBRY", 4),
        ("GRAYSCALE_4", 5), ("GRAYSCALE_16", 6), ("SEVEN_COLOR", 7), ("BWGBRY_SPLIT", 8),
    ]

def test_from_value_rejects_library_local_9():
    with pytest.raises(ValueError, match="Invalid color scheme value: 9"):
        ColorScheme.from_value(9)

def test_not_epaper_dithering_enum():
    assert ColorScheme.__module__ == "opendisplay.models.enums"
```

- [ ] **Step 3: Run it** — `uv run pytest tests/unit/test_color_scheme.py -v`. Expected: FAIL (`test_wire_values` on GRAYSCALE_8, `test_not_epaper_dithering_enum` on module).

- [ ] **Step 4: Implement.** Add the enum; swap every import listed above. In `display_palettes.py` key `DISPLAY_PALETTE_MAP` on the new enum and, where it falls back to the scheme, return `epaper_dithering.ColorScheme.from_value(int(scheme))` (bridge, removed in Task 2). Remove the GRAYSCALE_8 branch in `_capabilities_from_config`; `from_value(9)` now raises and the caller's existing `ImageEncodingError` wrapping (`device.py` ~3045) still applies.

- [ ] **Step 5: Run the suite** — `uv run pytest -q`. Expected: all pass, including `test_grayscale8_config_raises_image_encoding_error` and `test_out_of_range_color_scheme_raises_image_encoding_error`.

- [ ] **Step 6: Commit** — `feat!: define ColorScheme in py-opendisplay instead of re-exporting epaper-dithering's` with footer `BREAKING CHANGE: ColorScheme is an IntEnum owned by py-opendisplay; GRAYSCALE_8, .palette and .accent_color are gone.`

### Task 2: Switch to epaper-dithering 7, numeric `tone`/`gamut`

**Files:**
- Modify: `pyproject.toml` (dependency + `[tool.uv.sources]`)
- Modify: `src/opendisplay/display_palettes.py` (palette table)
- Modify: `src/opendisplay/device.py` (`prepare_image` ~381, ink canvas ~485-496; `tone`/`gamut` signatures ~304, ~1870, ~1910 and docstrings ~331, ~1937)
- Modify: `src/opendisplay/cli.py:114-124` (`_parse_compression_value`), `:1074-1085` (help), `:1134` (type)
- Modify: `README.md:257-267` (tone/gamut examples)
- Modify: tests that call `dither_image(..., mode=...)` or import 6.x palettes (`test_prepare_composite.py`, the 4 `DbsParams` importers)
- Test: `tests/unit/test_display_palettes.py`

**Interfaces:**
- Consumes: `ColorScheme` from Task 1.
- Produces: `tone: float = 0.0`, `gamut: float = 0.0` on `prepare_image`, `upload_image` and the composite prepare/upload pair; validation is left to epaper-dithering 7.
- Produces: `get_palette_for_display(panel_ic_type: int | None, color_scheme: ColorScheme | int, use_measured: bool = True) -> epaper_dithering.Palette`, cached per `(panel, scheme, use_measured)`.

- [ ] **Step 1: Point at the local 7.x build** — dependency `"epaper-dithering>=7.0.0.dev0,<8"` plus `[tool.uv.sources] epaper-dithering = { path = "../epaper-dithering/packages/python" }` (adjust the relative path from the worktree), `uv sync --all-extras`. Expected: the build succeeds, and `uv run pytest -q` fails on import (6.x names gone).

- [ ] **Step 2: Write the failing tests** in `tests/unit/test_display_palettes.py`:

```python
IDEAL_NAMES = {
    ColorScheme.MONO: ["black", "white"],
    ColorScheme.BWR: ["black", "white", "red"],
    ColorScheme.BWY: ["black", "white", "yellow"],
    ColorScheme.BWRY: ["black", "white", "yellow", "red"],
    ColorScheme.BWGBRY: ["black", "white", "yellow", "red", "blue", "green"],
    ColorScheme.BWGBRY_SPLIT: ["black", "white", "yellow", "red", "blue", "green"],
    ColorScheme.SEVEN_COLOR: ["black", "white", "yellow", "red", "blue", "green", "orange"],
    ColorScheme.GRAYSCALE_4: ["black", "gray1", "gray2", "white"],
    ColorScheme.GRAYSCALE_16: ["black"] + [f"gray{i}" for i in range(1, 15)] + ["white"],
}

def test_every_scheme_has_an_ideal_palette(): ...   # names == IDEAL_NAMES[s], measured is False
def test_ideal_index_order_matches_6x(): ...        # one stripe per ink nominal, DitherMode.NONE → indices 0..n-1
def test_measured_palettes():                       # (35, BWGBRY), (66, BWGBRY_SPLIT), (55, BWRY): measured is True,
    ...                                             # names in scheme order, white appearance == (100, 0, 0) ± 1e-6
def test_spectra_panels_share_v2(): ...             # entries of panel 35 and panel 66 are equal
def test_dropped_placeholders_fall_back(): ...      # (39, MONO) and (33, BWR) → measured is False
def test_use_measured_false(): ...                  # (55, BWRY, use_measured=False) → measured is False
def test_pure_inks_stay_solid_on_measured_bwry(): ... # #000000 / #ffffff / #ffff00 / #ff0000 stripes, BURKES → 0,1,2,3 exactly
def test_int_dither_mode_rejected(): ...            # prepare_image(..., dither_mode=1) raises ValueError

@pytest.mark.parametrize("value", ["auto", "off", True])
def test_string_strength_rejected(value): ...       # prepare_image(..., tone=value) raises TypeError matching "tone must be a number from 0.0 to 1.0"
```

Plus a CLI test: `--gamut auto` exits 1 with `--gamut must be a number from 0.0 to 1.0`.


- [ ] **Step 3: Run them** — Expected: FAIL (import errors, then wrong types).

- [ ] **Step 4: Build the table** in `display_palettes.py`. Ideal palettes: `Palette.from_srgb([(hex, name, hex), ...])` from the 6.x inks in `IDEAL_NAMES` order — black `#000000`, white `#ffffff`, red `#ff0000`, yellow `#ffff00`, blue `#0000ff`, green `#00ff00`, orange `#ff8000`, GRAYSCALE_4 grays `#555555`/`#aaaaaa`, GRAYSCALE_16 grays `#111111` to `#eeeeee` in 0x11 steps. Measured palettes: `Palette.from_srgb([(rgb, name, nominal), ...], measured=True).with_source(...)` from the 6.x sRGB constants:

| Key | Source label | black | white | yellow | red | blue | green |
| --- | --- | --- | --- | --- | --- | --- | --- |
| (35, BWGBRY), (66, BWGBRY_SPLIT) | `Waveshare 7.3in Spectra 6, measured 2026-03-15 (6.x SPECTRA_7_3_6COLOR_V2)` | 31,24,41 | 168,180,182 | 180,173,0 | 113,24,19 | 36,70,139 | 50,84,60 |
| (55, BWRY) | `EP397YR 3.97in BWRY (6.x BWRY_3_97)` | 10,7,14 | 173,178,174 | 172,128,0 | 85,24,14 | | |

Drop `MONO_4_26`, `SOLUM_BWR`, the v1 Spectra constant, the bridge from Task 1 and the commented-out entries. Keep `PANELS_4GRAY` and the code tables unchanged.

- [ ] **Step 5: Update the call sites.** `prepare_image`: `dither_image(image, palette, DitherOptions(mode=dither_mode, serpentine=..., exposure=..., saturation=..., shadows=..., highlights=..., tone=..., gamut=..., dbs=_resolve_dbs(dbs, display_cfg)))`. A non-`DitherMode`, non-`str` `dither_mode` raises `ValueError(f"unknown dither mode {dither_mode!r}")` before dithering. Ink canvas: `white_index = palette.index_of("white")`, `black_index = palette.index_of("black")` (raise `ImageEncodingError` if either is `None`), canvas palette from `palette.preview_rgb()`, photo dithered with `DitherOptions(mode=dither_mode, dbs=...)`. `DbsParams` and `DitherMode` keep their import names. Update the tests that used 6.x keyword arguments or palette constants.

- [ ] **Step 6: `tone`/`gamut` become numbers.** Type them `float` everywhere listed above; docstrings say `Black-point lift, 0.0–1.0 (default: 0.0)` and `Gamut compression, 0.0–1.0 (default: 0.0)`. No own validation: epaper-dithering 7 raises for strings and out-of-range values. The CLI parser returns `float` and calls `_error(f"{flag} must be a number from 0.0 to 1.0, got {value!r}")` for non-numbers and out-of-range values; help text `"Black-point lift, 0.0–1.0 (default: 0)"` / `"Gamut compression, 0.0–1.0 (default: 0)"`. README: drop the `"auto"` and `"off"` examples, keep `tone=1.0`. Remove or update existing tests that pass `"auto"`/`"off"`.

- [ ] **Step 7: Run the suite and lint** — `uv run pytest -q` and `uv run prek run --all-files`. Expected: all pass.

- [ ] **Step 8: Commit** — `feat!: move to epaper-dithering 7 with py-opendisplay's own palette table` with footer `BREAKING CHANGE: requires epaper-dithering 7. tone and gamut take 0.0–1.0 numbers only ("auto"/"off" removed). Panels 39 and 33 use idealised palettes until measured; panel 35 uses the v2 Spectra 6 measurement; DitherMode is a string enum.`

### Task 3: Hardware check and docs

**Files:**
- Modify: `README.md` (palette section, if it names 6.x constants or `ColorScheme.palette`), `CLAUDE.md` (`display_palettes.py` description)

- [ ] **Step 1:** `grep -rn "ColorPalette\|accent_color\|SPECTRA_7_3\|MONO_4_26\|SOLUM_BWR\|epaper_dithering import ColorScheme" README.md CLAUDE.md docs/ src/` — Expected after edits: no hits outside `docs/superpowers/`.
- [ ] **Step 2:** Upload one photo and one dashboard image from the CLI to a tag on today's firmware (`opendisplay upload ...`), and compare with the same images on `origin/main`. Expected: same look apart from the warm-tone shift; dashboard text solid. Record the tag and the result in the PR description.
- [ ] **Step 3: Commit** — `docs: describe py-opendisplay's palette table`.

## Before merge

- Replace the `[tool.uv.sources]` path entry with `"epaper-dithering==7.x.y"` once 7 is on PyPI; `uv lock`; rerun the suite.
- Run the Task 3 hardware comparison on a measured-palette panel (55 or 35), and put the measured-palette note from the spec's "Known output changes" in the PR body / release notes.
- `feat/sendspin-artwork` (`now_playing.py`, `cli_sendspin.py`) imports `DbsParams`/`DitherMode` and calls `dither_image`; rebase it after this lands.
