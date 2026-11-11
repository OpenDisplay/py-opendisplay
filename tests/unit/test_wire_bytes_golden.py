"""Solid-ink images encode to the same wire bytes as on epaper-dithering 6.x.

The expected hashes were produced by py-opendisplay on origin/main with epaper-dithering 6.1.1.
They pin the palette index order and the encoders together: a solid-ink image (a dashboard)
must reach the panel exactly as before.
"""

from __future__ import annotations

import hashlib

import pytest
from epaper_dithering import DitherMode
from PIL import Image

from opendisplay.device import prepare_image
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme

_SPECTRA6 = ["#000000", "#ffffff", "#ffff00", "#ff0000", "#0000ff", "#00ff00"]
INKS = {
    "MONO": ["#000000", "#ffffff"],
    "BWR": ["#000000", "#ffffff", "#ff0000"],
    "BWY": ["#000000", "#ffffff", "#ffff00"],
    "BWRY": ["#000000", "#ffffff", "#ffff00", "#ff0000"],
    "BWGBRY": _SPECTRA6,
    "BWGBRY_SPLIT": _SPECTRA6,
    "SEVEN_COLOR": [*_SPECTRA6, "#ff8000"],
    "GRAYSCALE_4": ["#000000", "#555555", "#aaaaaa", "#ffffff"],
    "GRAYSCALE_16": ["#" + f"{i * 17:02x}" * 3 for i in range(16)],
}

# (scheme, panel_ic_type, dither mode, first 12 hex digits of sha1 of the uncompressed payload)
GOLDEN_6X = [
    ("MONO", None, DitherMode.NONE, "1385c910663f"),
    ("MONO", None, DitherMode.BURKES, "1385c910663f"),
    ("MONO", 39, DitherMode.NONE, "1385c910663f"),
    ("MONO", 39, DitherMode.BURKES, "1385c910663f"),
    ("BWR", None, DitherMode.NONE, "f705cc1ccbe8"),
    ("BWR", None, DitherMode.BURKES, "f705cc1ccbe8"),
    ("BWR", 33, DitherMode.NONE, "f705cc1ccbe8"),
    ("BWR", 33, DitherMode.BURKES, "f705cc1ccbe8"),
    ("BWY", None, DitherMode.NONE, "47140ed16d77"),
    ("BWY", None, DitherMode.BURKES, "47140ed16d77"),
    ("BWRY", None, DitherMode.NONE, "77c40254e9a2"),
    ("BWRY", None, DitherMode.BURKES, "77c40254e9a2"),
    ("BWRY", 55, DitherMode.NONE, "77c40254e9a2"),
    ("BWRY", 55, DitherMode.BURKES, "77c40254e9a2"),
    ("BWRY", 29, DitherMode.NONE, "a56766623b51"),
    ("BWRY", 29, DitherMode.BURKES, "a56766623b51"),
    ("BWGBRY", None, DitherMode.NONE, "45220dfe49d5"),
    ("BWGBRY", None, DitherMode.BURKES, "45220dfe49d5"),
    ("BWGBRY", 35, DitherMode.NONE, "45220dfe49d5"),
    ("BWGBRY", 35, DitherMode.BURKES, "45220dfe49d5"),
    ("BWGBRY_SPLIT", None, DitherMode.NONE, "e05ee7f16973"),
    ("BWGBRY_SPLIT", None, DitherMode.BURKES, "e05ee7f16973"),
    ("BWGBRY_SPLIT", 66, DitherMode.NONE, "e05ee7f16973"),
    ("BWGBRY_SPLIT", 66, DitherMode.BURKES, "e05ee7f16973"),
    ("SEVEN_COLOR", None, DitherMode.NONE, "f6c713f72f73"),
    ("SEVEN_COLOR", None, DitherMode.BURKES, "f6c713f72f73"),
    ("GRAYSCALE_4", None, DitherMode.NONE, "f5d433d62721"),
    ("GRAYSCALE_4", None, DitherMode.BURKES, "f5d433d62721"),
    ("GRAYSCALE_16", None, DitherMode.NONE, "6f9aeac2868f"),
    ("GRAYSCALE_16", None, DitherMode.BURKES, "6f9aeac2868f"),
]


def _stripes(inks: list[str]) -> Image.Image:
    width = (8 * len(inks) + 15) // 16 * 16
    img = Image.new("RGB", (width, 16), "white")
    for i, color in enumerate(inks):
        img.paste(color, (i * 8, 0, i * 8 + 8, 16))
    return img


@pytest.mark.parametrize(("scheme", "panel", "mode", "expected"), GOLDEN_6X)
def test_solid_inks_encode_as_on_6x(scheme: str, panel: int | None, mode: DitherMode, expected: str) -> None:
    img = _stripes(INKS[scheme])
    caps = DeviceCapabilities(width=img.width, height=img.height, color_scheme=ColorScheme[scheme])
    data, _, _ = prepare_image(img, capabilities=caps, panel_ic_type=panel, dither_mode=mode, compress=False)
    assert hashlib.sha1(data).hexdigest()[:12] == expected
