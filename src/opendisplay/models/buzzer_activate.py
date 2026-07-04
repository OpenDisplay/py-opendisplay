"""Typed buzzer activation config for firmware command 0x0077."""

from __future__ import annotations

from dataclasses import dataclass

_MIN_HZ = 400
_MAX_HZ = 12000
_DURATION_UNIT_MS = 5


def hz_to_index(hz: int) -> int:
    """Convert frequency in Hz to firmware tone index (0-255). 0 Hz → silence."""
    if hz <= 0:
        return 0
    idx = round(1 + (hz - _MIN_HZ) * 254 / (_MAX_HZ - _MIN_HZ))
    return max(1, min(255, idx))


def ms_to_units(ms: int) -> int:
    """Convert duration in ms to firmware duration units (5 ms each). Minimum 1 unit."""
    return max(1, min(255, round(ms / _DURATION_UNIT_MS)))


@dataclass(frozen=True, slots=True)
class BuzzerStep:
    """A single tone step: one frequency for one duration."""

    frequency_index: int  # 0=silence, 1–255 → 400–12000 Hz
    duration_units: int  # ×5 ms each; range 1–255


@dataclass(frozen=True, slots=True)
class BuzzerPattern:
    """One pattern of steps played in sequence."""

    steps: tuple[BuzzerStep, ...]

    def to_bytes(self) -> bytes:
        """Serialize pattern to firmware wire format: [n_steps][freq][dur]..."""
        return bytes([len(self.steps)]) + bytes(b for s in self.steps for b in (s.frequency_index, s.duration_units))


@dataclass(frozen=True, slots=True)
class BuzzerActivateConfig:
    """Full buzzer activation payload for command 0x0077."""

    patterns: tuple[BuzzerPattern, ...]
    outer_repeats: int = 1  # 1–255

    @classmethod
    def single_tone(
        cls,
        *,
        frequency_hz: int,
        duration_ms: int,
        repeats: int = 1,
    ) -> BuzzerActivateConfig:
        """Build a simple single-step single-pattern config from Hz and milliseconds."""
        return cls(
            patterns=(
                BuzzerPattern(
                    steps=(
                        BuzzerStep(
                            frequency_index=hz_to_index(frequency_hz),
                            duration_units=ms_to_units(duration_ms),
                        ),
                    )
                ),
            ),
            outer_repeats=max(1, repeats),
        )

    def to_bytes(self) -> bytes:
        """Serialize full config to firmware wire format: [repeats][n_patterns][patterns...]"""
        return bytes([self.outer_repeats, len(self.patterns)]) + b"".join(p.to_bytes() for p in self.patterns)
