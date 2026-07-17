"""Typed buzzer activation config for firmware command 0x0077."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

_ANCHOR_HZ = 13.75
_DURATION_UNIT_MS = 5

# Quarter-tone note model (see BUZZER_MUSIC_PROTOCOL_REFERENCE.md §4).
_STEPS_PER_OCTAVE = 24
_C0_INDEX = 6  # firmware index of C0; Cn = _C0_INDEX + _STEPS_PER_OCTAVE * n
# Semitone offset of each natural note from C, before the ×2 quarter-tone scaling.
_NOTE_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"(?P<letter>[A-G])(?P<accidental>[#SB])?(?P<octave>-?\d+)(?P<quarter>[+P])?")

# Melody builder limits (see design doc §3, §5.3, §8).
_MAX_STEPS = 120
_MAX_DURATION_MS = 1275  # dur_unit is one byte → 255 × 5 ms
_NOTE_FRACTIONS = frozenset({1, 2, 4, 8, 16, 32})


def hz_to_index(hz: int) -> int:
    """Convert Hz to firmware quarter-tone index (0-255). 0/negative Hz -> 0 (silence).

    Inverse of the firmware scale Freq(idx) = 13.75 * 2**(idx/24) Hz.

    Examples:
        idx 1   -> ~14.15 Hz    (nAm1p, the bottom note in the table)
        idx 120 -> 440.00 Hz    (nA4, concert pitch A)
        idx 255 -> ~21714.33 Hz (nE10p, the top note in the table)

    Indices outside the firmware's playable window [117, 234] are octave-folded
    by the firmware itself (preserving pitch class) before being driven onto the
    speaker -- this protects the buzzer hardware from being driven outside its
    safe operating range. This helper only produces a valid 0-255 index; it does
    not need to replicate that folding.
    """
    if hz <= 0:
        return 0
    idx = round(_STEPS_PER_OCTAVE * math.log2(hz / _ANCHOR_HZ))
    return max(1, min(255, idx))


def ms_to_units(ms: int) -> int:
    """Convert duration in ms to firmware duration units (5 ms each). Minimum 1 unit."""
    return max(1, min(255, round(ms / _DURATION_UNIT_MS)))


def note_to_index(name: str) -> int:
    """Convert a note name to its firmware quarter-tone index (0-255).

    Grammar (case-insensitive): ``letter accidental? octave quarter?``.

    - ``letter``: ``A``-``G``.
    - ``accidental``: ``#``/``s`` (sharp) or ``b`` (flat, enharmonic).
    - ``octave``: ``-1``..``10`` (octave -1 may also be spelled ``m1`` by the
      firmware enum, but this parser accepts the plain ``-1`` form).
    - ``quarter``: ``+``/``p`` raises the pitch by one quarter-tone (odd index).
    - ``R`` / ``REST`` return 0 (the rest sentinel).

    The index follows ``idx = _C0_INDEX + _STEPS_PER_OCTAVE * octave +
    2 * (semitone ± accidental) + quarter`` (reference §4.4), so ``A4`` → 120,
    ``C5`` → 126, ``A5`` → 144, ``C#5``/``Cs5`` → 128, ``A4+``/``A4p`` → 121, and
    ``As4p`` → 123. Flats are enharmonic (``Bb4`` == ``A#4`` == 122).

    Raises:
        ValueError: unknown note letter, malformed name, octave out of ``-1..10``,
            or a computed index outside ``1..255`` (index 0 is reserved for rests,
            so ``A-1`` — which computes to 0 — is rejected).
    """
    text = name.strip().upper()
    if text in ("R", "REST"):
        return 0
    match = _NOTE_RE.fullmatch(text)
    if match is None:
        first = text[:1]
        if first.isalpha() and first not in _NOTE_SEMITONES:
            raise ValueError(f"unknown note letter {first!r}")
        raise ValueError(f"invalid note name {name!r}")
    semitone = _NOTE_SEMITONES[match.group("letter")]
    accidental = match.group("accidental")
    if accidental in ("#", "S"):
        semitone += 1
    elif accidental == "B":
        semitone -= 1
    octave = int(match.group("octave"))
    if not -1 <= octave <= 10:
        raise ValueError(f"octave {octave} out of range (-1..10)")
    quarter = 1 if match.group("quarter") else 0
    idx = _C0_INDEX + _STEPS_PER_OCTAVE * octave + 2 * semitone + quarter
    if not 1 <= idx <= 255:
        raise ValueError(f"note {name!r} maps to index {idx}, outside 1..255")
    return idx


def _fraction_ms(fraction: int, *, dotted: bool, triplet: bool, tempo: int) -> float:
    """Milliseconds for a note ``fraction`` at ``tempo`` BPM, with optional modifier."""
    ms = (4 * 60000 / tempo) / fraction  # whole_note_ms / fraction
    if dotted:
        ms *= 1.5
    if triplet:
        ms *= 2 / 3
    return ms


def _rel_fraction_ms(marker: str, *, tempo: int) -> float:
    """Resolve a ``/frac[.|t]`` relative-duration marker to milliseconds."""
    match = re.fullmatch(r"/(\d+)([.t]*)", marker)
    if match is None:
        raise ValueError(f"invalid duration {marker!r}")
    fraction = int(match.group(1))
    if fraction not in _NOTE_FRACTIONS:
        raise ValueError(f"invalid note fraction {fraction} (allowed: {sorted(_NOTE_FRACTIONS)})")
    modifier = match.group(2)
    dotted = "." in modifier
    triplet = "t" in modifier
    if dotted and triplet:
        raise ValueError("dotted '.' and triplet 't' modifiers cannot be combined")
    if len(modifier) > 1:
        raise ValueError(f"invalid duration modifier {modifier!r}")
    return _fraction_ms(fraction, dotted=dotted, triplet=triplet, tempo=tempo)


def _resolve_duration_ms(marker: str | None, *, tempo: int, default_ms: int, default_length: int | None) -> float:
    """Resolve a token's duration ``marker`` (with its leading ``:``/``/``) to ms.

    ``marker`` is None when the token carried no explicit duration; the default is
    then ``default_length`` at ``tempo`` if set, otherwise ``default_ms``. Raises
    ValueError on a malformed marker or a duration above ``_MAX_DURATION_MS`` (the
    range check runs here, before :func:`ms_to_units`, which would silently clamp).
    """
    if marker is None:
        if default_length is not None:
            ms = _fraction_ms(default_length, dotted=False, triplet=False, tempo=tempo)
        else:
            ms = float(default_ms)
    elif marker.startswith(":"):
        body = marker[1:]
        if not body.isdigit():
            raise ValueError(f"invalid duration {marker!r}")
        ms = float(int(body))
        if ms < 1:
            raise ValueError(f"duration {marker!r} must be at least 1 ms")
    else:  # marker starts with '/' (the tokenizer only splits on ':' or '/')
        ms = _rel_fraction_ms(marker, tempo=tempo)
    if ms > _MAX_DURATION_MS:
        raise ValueError(f"duration {ms:.0f} ms exceeds {_MAX_DURATION_MS} ms")
    return ms


def _parse_token(token: str, *, tempo: int, default_ms: int, default_length: int | None) -> tuple[int, int]:
    """Parse one ``item[duration]`` token into ``(frequency_index, duration_units)``.

    ``item`` is a raw index (0-255), ``R``/``REST``, or a note name; ``duration`` is
    an optional ``:ms`` or ``/frac`` marker. Raises ValueError on any malformed part.
    """
    split_at = len(token)
    for i, char in enumerate(token):
        if char in ":/":
            split_at = i
            break
    item = token[:split_at]
    marker = token[split_at:] or None
    if not item:
        raise ValueError("missing note before duration")
    if item.upper() in ("R", "REST"):
        index = 0
    elif item[0].isdigit():
        if not item.isdigit():
            raise ValueError(f"invalid raw index {item!r}")
        index = int(item)
        if not 0 <= index <= 255:
            raise ValueError(f"raw index {index} out of range (0..255)")
    else:
        index = note_to_index(item)
    ms = _resolve_duration_ms(marker, tempo=tempo, default_ms=default_ms, default_length=default_length)
    return index, ms_to_units(round(ms))


@dataclass(frozen=True, slots=True)
class BuzzerStep:
    """A single tone step: one frequency for one duration."""

    frequency_index: int  # 0=silence; 1–255 → quarter-tone note, Freq = 13.75 * 2**(idx/24) Hz
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

    @classmethod
    def melody(
        cls,
        notes: str | Sequence[tuple[int | str, int]],
        *,
        tempo: int = 120,
        repeats: int = 1,
        default_ms: int = 200,
        default_length: int | None = None,
    ) -> BuzzerActivateConfig:
        """Build a single-pattern config from a compact melody notation.

        ``notes`` is either a compact string or a sequence of ``(pitch, ms)`` pairs:

        - **String form** — whitespace/comma-separated ``item[duration]`` tokens.
          ``item`` is a raw firmware index (0-255), ``R``/``REST``, or a note name
          (see :func:`note_to_index`). ``duration`` is an optional ``:ms`` (absolute
          milliseconds) or ``/frac`` marker where ``frac`` ∈ {1, 2, 4, 8, 16, 32}
          with an optional ``.`` (dotted, ×1.5) or ``t`` (triplet, ×2/3) modifier.
          An omitted duration uses ``default_length`` at ``tempo`` if set, otherwise
          ``default_ms``. A relative duration resolves to
          ``(4 * 60000 / tempo) / frac`` ms.
        - **Sequence form** — ``(pitch, ms)`` tuples for programmatic callers; ``pitch``
          is a raw index (int) or note name (str), ``ms`` is absolute milliseconds.

        Args:
            notes: The melody, as a compact string or a sequence of pairs.
            tempo: Beats per minute; only affects ``/frac`` durations. Must be > 0.
            repeats: Whole-melody repeat count (wire ``outer_repeat``), 1-255.
            default_ms: Absolute duration for unmarked tokens when ``default_length``
                is unset.
            default_length: Note fraction (1/2/4/8/16/32) used at ``tempo`` for
                unmarked tokens; overrides ``default_ms`` when set.

        Returns:
            A single-pattern :class:`BuzzerActivateConfig`.

        Raises:
            ValueError: on invalid arguments, a malformed token (the message carries
                the 1-based token position and text), a resolved duration above
                1275 ms, an empty melody, or more than 120 steps. The reserved ``|``
                character raises "multi-pattern not supported in compact form".
        """
        if tempo <= 0:
            raise ValueError(f"tempo must be positive, got {tempo}")
        if not 1 <= repeats <= 255:
            raise ValueError(f"repeats must be in 1..255, got {repeats}")
        if default_length is not None and default_length not in _NOTE_FRACTIONS:
            raise ValueError(f"default_length must be one of {sorted(_NOTE_FRACTIONS)}, got {default_length}")

        if isinstance(notes, str):
            steps = cls._steps_from_string(notes, tempo=tempo, default_ms=default_ms, default_length=default_length)
        else:
            steps = cls._steps_from_sequence(notes)

        if not steps:
            raise ValueError("melody is empty")
        if len(steps) > _MAX_STEPS:
            raise ValueError(f"melody has {len(steps)} steps, exceeding the {_MAX_STEPS}-step limit")
        return cls(patterns=(BuzzerPattern(steps=tuple(steps)),), outer_repeats=repeats)

    @staticmethod
    def _steps_from_string(
        notes: str,
        *,
        tempo: int,
        default_ms: int,
        default_length: int | None,
    ) -> list[BuzzerStep]:
        """Tokenize the compact string form into steps, tagging errors with position."""
        if "|" in notes:
            raise ValueError("multi-pattern not supported in compact form")
        tokens = [token for token in re.split(r"[\s,]+", notes.strip()) if token]
        steps: list[BuzzerStep] = []
        for position, token in enumerate(tokens, start=1):
            try:
                index, duration_units = _parse_token(
                    token, tempo=tempo, default_ms=default_ms, default_length=default_length
                )
            except ValueError as err:
                raise ValueError(f"token {position} {token!r}: {err}") from err
            steps.append(BuzzerStep(frequency_index=index, duration_units=duration_units))
        return steps

    @staticmethod
    def _steps_from_sequence(notes: Sequence[tuple[int | str, int]]) -> list[BuzzerStep]:
        """Convert programmatic ``(pitch, ms)`` pairs into steps, tagging errors."""
        steps: list[BuzzerStep] = []
        for position, pair in enumerate(notes, start=1):
            try:
                pitch, ms = pair
                index = note_to_index(pitch) if isinstance(pitch, str) else pitch
                if not 0 <= index <= 255:
                    raise ValueError(f"index {index} out of range (0..255)")
                if not 1 <= ms <= _MAX_DURATION_MS:
                    raise ValueError(f"duration {ms} ms out of range (1..{_MAX_DURATION_MS})")
            except (TypeError, ValueError) as err:
                raise ValueError(f"note {position}: {err}") from err
            steps.append(BuzzerStep(frequency_index=index, duration_units=ms_to_units(ms)))
        return steps

    def to_bytes(self) -> bytes:
        """Serialize full config to firmware wire format: [repeats][n_patterns][patterns...]"""
        return bytes([self.outer_repeats, len(self.patterns)]) + b"".join(p.to_bytes() for p in self.patterns)
