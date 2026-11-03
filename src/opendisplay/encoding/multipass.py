"""Encode an image for a multi-pass drive mode (e.g. BWRY 16-colour).

A multi-pass mode drives the panel several times with DIFFERENT pixel data each
time; a pixel's colour is set by its code in ALL passes. So encoding is:

    image -> nearest palette entry per pixel -> one 2bpp frame per pass

The palette comes from the DEVICE (0x30 display_calibration): per-entry
per-pass codes plus the CIELAB those codes measured as. Nothing here hardcodes a
palette, because it is per-unit -- two same-part-number 2.9" panels differ by
dE00 6.40, and a palette tuned on one measured 19% worse on the other.

Nearest-colour is done in CIELAB with plain Euclidean distance (dE76). dE2000
would be more perceptually even, but these 16 entries are far apart relative to
its correction terms: the closest pair measured dE00 1.22 and most exceed 3, so
the ranking barely changes and dE76 keeps this dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class MultiPassPalette:
    """Palette for one multi-pass mode, as read off a device."""

    lab: np.ndarray  # (N, 3) CIELAB
    codes: np.ndarray  # (N, P) per-pass 2-bit codes
    measured: bool = True

    @classmethod
    def from_calibration(cls, cal, passes: int) -> "MultiPassPalette":
        """Build from a DisplayCalibrationInfo read out of the device config."""
        if not cal.codes:
            raise ValueError(
                "calibration carries no per-pass codes, so a frame cannot be "
                "encoded from it -- the device's 0x30 packet predates the "
                "encoding field or was written without it"
            )
        n = min(len(cal.lab), len(cal.codes))
        if n == 0:
            raise ValueError("calibration has no entries")
        codes = np.array([c[:passes] for c in cal.codes[:n]], dtype=np.uint8)
        if codes.shape[1] != passes:
            raise ValueError(f"calibration codes cover {codes.shape[1]} passes but the mode declares {passes}")
        return cls(lab=np.array(cal.lab[:n], dtype=np.float64), codes=codes, measured=bool(cal.measured))

    @property
    def passes(self) -> int:
        return int(self.codes.shape[1])

    def __len__(self) -> int:
        return int(self.codes.shape[0])


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (H,W,3 uint8) -> CIELAB, D65. Same convention as the measurements."""
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array(
        [[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]]
    )
    xyz = lin @ m.T
    white = np.array([0.95047, 1.0, 1.08883])
    t = xyz / white
    d = 6.0 / 29.0
    f = np.where(t > d**3, np.cbrt(t), t / (3 * d * d) + 4.0 / 29.0)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def quantise(rgb: np.ndarray, palette: MultiPassPalette, dither: bool = True) -> np.ndarray:
    """(H,W,3) uint8 -> (H,W) palette indices.

    Floyd-Steinberg error diffusion is applied in CIELAB. Without it a 16-entry
    palette whose chromatic entries all lie between red and yellow produces
    visible banding on anything smooth -- this panel has no green or blue
    pigment, so large areas have no nearby entry to land on.
    """
    lab = _srgb_to_lab(rgb)
    h, w = lab.shape[:2]
    out = np.zeros((h, w), dtype=np.uint8)
    pal = palette.lab
    work = lab.copy()

    for y in range(h):
        for x in range(w):
            px = work[y, x]
            i = int(np.argmin(((pal - px) ** 2).sum(axis=1)))
            out[y, x] = i
            if not dither:
                continue
            err = px - pal[i]
            # Standard Floyd-Steinberg weights, clipped at the edges.
            for dx, dy, f in ((1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    work[ny, nx] += err * f
    return out


def pack_pass(indices: np.ndarray, palette: MultiPassPalette, pass_no: int, code_map: list[int] | None = None) -> bytes:
    """Palette indices -> one pass's 2bpp frame, MSB-first, row-major.

    DO NOT PASS code_map FOR A MEASURED PALETTE. A palette entry's per-pass
    values are already PANEL 2-bit codes: show16_raw.py, the renderer they were
    derived from, writes them straight to the panel with no mapping. code_map
    (0x31) turns a LOGICAL BWRY colour into a panel code for the 4-colour path;
    applying it to codes that are already panel codes maps 2->3 and 3->2, which
    swaps yellow and red in every pass and silently mis-measures the palette.

    The parameter is kept only for a caller whose indices really are logical
    colours, and defaults to None so the common case cannot get it wrong.
    """
    if not 0 <= pass_no < palette.passes:
        raise ValueError(f"pass {pass_no} out of range for {palette.passes}-pass mode")
    logical = palette.codes[:, pass_no][indices]  # (H, W) 2-bit codes
    if code_map is not None:
        logical = np.asarray(code_map, dtype=np.uint8)[logical]
    h, w = logical.shape
    # Pad the row to a whole byte: 2bpp means 4 pixels per byte.
    pad = (-w) % 4
    if pad:
        logical = np.pad(logical, ((0, 0), (0, pad)))
    flat = logical.reshape(h, -1, 4)
    packed = ((flat[..., 0] & 3) << 6) | ((flat[..., 1] & 3) << 4) | ((flat[..., 2] & 3) << 2) | (flat[..., 3] & 3)
    return packed.astype(np.uint8).tobytes()


def encode_frames(
    rgb: np.ndarray, palette: MultiPassPalette, code_map: list[int] | None = None, dither: bool = True
) -> list[bytes]:
    """(H,W,3) uint8 -> one packed frame per pass, in pass order.

    The device expects these concatenated in a single CMD_DRIVE_WRITE_START
    session and advances pass at each frame boundary.
    """
    if code_map is not None:
        raise ValueError(
            "encode_frames does not take a code_map: a measured palette's codes "
            "are already panel codes, and mapping them again swaps yellow and red"
        )
    idx = quantise(rgb, palette, dither=dither)
    return [pack_pass(idx, palette, p) for p in range(palette.passes)]
