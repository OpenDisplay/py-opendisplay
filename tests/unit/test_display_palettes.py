"""py-opendisplay's palette table on epaper-dithering 7."""

from __future__ import annotations

from importlib.resources import files

import pytest
from epaper_dithering import DitherMode, DitherOptions, Palette, dither_image
from PIL import Image

from opendisplay.device import prepare_image
from opendisplay.display_palettes import (
    IDEAL_PALETTE_FILES,
    MEASURED_PALETTE_FILES,
    get_palette_for_display,
    load_palette_file,
)
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme

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

MEASURED = [(35, ColorScheme.BWGBRY), (66, ColorScheme.BWGBRY_SPLIT), (55, ColorScheme.BWRY)]


def _names(palette: Palette) -> list[str | None]:
    return [e.name for e in palette.entries]


def _stripes(colors: list[tuple[int, int, int]]) -> Image.Image:
    img = Image.new("RGB", (len(colors) * 4, 4))
    for i, c in enumerate(colors):
        img.paste(c, (i * 4, 0, i * 4 + 4, 4))
    return img


def _indices(dithered: Image.Image) -> list[list[int]]:
    """Palette index per pixel, row by row."""
    data = dithered.tobytes()
    return [list(data[y * dithered.width : (y + 1) * dithered.width]) for y in range(dithered.height)]


def _stripe_indices(dithered: Image.Image, count: int) -> list[int]:
    return [_indices(dithered)[1][i * 4 + 1] for i in range(count)]


def test_palette_files_ship_with_the_package() -> None:
    shipped = {p.name for p in files("opendisplay").joinpath("palettes").iterdir() if p.name.endswith(".json")}
    referenced = set(IDEAL_PALETTE_FILES.values()) | set(MEASURED_PALETTE_FILES.values())
    assert referenced == shipped
    for name in referenced:
        assert len(load_palette_file(name)) >= 2


def test_every_scheme_has_an_ideal_palette() -> None:
    assert set(IDEAL_NAMES) == set(ColorScheme)
    for scheme, names in IDEAL_NAMES.items():
        palette = get_palette_for_display(None, scheme)
        assert _names(palette) == names, scheme
        assert palette.measured is False


def test_ideal_index_order_matches_6x() -> None:
    for scheme, names in IDEAL_NAMES.items():
        palette = get_palette_for_display(None, scheme)
        nominals = [e.nominal for e in palette.entries if e.nominal is not None]
        assert len(nominals) == len(names)
        dithered = dither_image(_stripes(nominals), palette, DitherOptions(mode=DitherMode.NONE))
        assert _stripe_indices(dithered, len(names)) == list(range(len(names))), scheme


@pytest.mark.parametrize(("panel", "scheme"), MEASURED)
def test_measured_palettes(panel: int, scheme: ColorScheme) -> None:
    palette = get_palette_for_display(panel, scheme)
    assert palette.measured is True
    assert _names(palette) == IDEAL_NAMES[scheme]
    white = palette.entries[1].appearance
    assert white == pytest.approx((100.0, 0.0, 0.0), abs=1e-6)


def test_spectra_panels_share_v2() -> None:
    a = get_palette_for_display(35, ColorScheme.BWGBRY).entries
    b = get_palette_for_display(66, ColorScheme.BWGBRY_SPLIT).entries
    assert [e.appearance for e in a] == [e.appearance for e in b]


@pytest.mark.parametrize(("panel", "scheme"), [(39, ColorScheme.MONO), (33, ColorScheme.BWR)])
def test_dropped_placeholders_fall_back(panel: int, scheme: ColorScheme) -> None:
    assert get_palette_for_display(panel, scheme).measured is False


def test_use_measured_false() -> None:
    assert get_palette_for_display(55, ColorScheme.BWRY, use_measured=False).measured is False


def test_accepts_int_scheme() -> None:
    assert _names(get_palette_for_display(None, 3)) == IDEAL_NAMES[ColorScheme.BWRY]


def test_pure_inks_stay_solid_on_measured_bwry() -> None:
    palette = get_palette_for_display(55, ColorScheme.BWRY)
    colors = [(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)]
    dithered = dither_image(_stripes(colors), palette, DitherOptions(mode=DitherMode.BURKES))
    rows = _indices(dithered)
    for i in range(4):
        stripe = {row[x] for row in rows for x in range(i * 4, i * 4 + 4)}
        assert stripe == {i}, (colors[i], stripe)


def _caps(scheme: ColorScheme = ColorScheme.BWRY) -> DeviceCapabilities:
    return DeviceCapabilities(width=8, height=8, color_scheme=scheme)


@pytest.mark.parametrize("name", ["tone", "gamut"])
@pytest.mark.parametrize("value", ["auto", "off", True])
def test_string_strength_rejected(name: str, value: object) -> None:
    # A 6.x caller's "auto"/"off" must fail loudly, not fall back to a default.
    kwargs: dict[str, object] = {name: value}
    with pytest.raises(TypeError, match=f"{name} must be a number from 0.0 to 1.0"):
        prepare_image(Image.new("RGB", (8, 8)), capabilities=_caps(), **kwargs)  # type: ignore[arg-type]
