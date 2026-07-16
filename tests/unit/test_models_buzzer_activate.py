"""Tests for BuzzerActivateConfig and helpers."""

from __future__ import annotations

import pytest

from opendisplay.models.buzzer_activate import (
    BuzzerActivateConfig,
    BuzzerPattern,
    BuzzerStep,
    hz_to_index,
    ms_to_units,
    note_to_index,
)


class TestHzToIndex:
    def test_silence_at_zero(self):
        assert hz_to_index(0) == 0

    def test_silence_at_negative(self):
        assert hz_to_index(-100) == 0

    def test_minimum_frequency(self):
        assert hz_to_index(400) == 1

    def test_maximum_frequency(self):
        assert hz_to_index(12000) == 255

    def test_midpoint_frequency(self):
        # 6200 Hz is the midpoint → index ~128
        idx = hz_to_index(6200)
        assert 126 <= idx <= 130

    def test_clamps_above_max(self):
        assert hz_to_index(99999) == 255

    def test_clamps_below_min_non_zero(self):
        # Values between 1 and 399 Hz should still produce index 1 (not 0)
        assert hz_to_index(1) == 1
        assert hz_to_index(399) == 1


class TestMsToUnits:
    def test_minimum_one_unit(self):
        assert ms_to_units(1) == 1

    def test_five_ms_is_one_unit(self):
        assert ms_to_units(5) == 1

    def test_ten_ms_is_two_units(self):
        assert ms_to_units(10) == 2

    def test_rounding(self):
        # 7 ms → round(7/5) = round(1.4) = 1
        assert ms_to_units(7) == 1
        # 8 ms → round(8/5) = round(1.6) = 2
        assert ms_to_units(8) == 2

    def test_max_clamp(self):
        assert ms_to_units(99999) == 255

    def test_zero_returns_minimum(self):
        assert ms_to_units(0) == 1


class TestBuzzerPatternToBytes:
    def test_single_step(self):
        step = BuzzerStep(frequency_index=10, duration_units=20)
        pattern = BuzzerPattern(steps=(step,))
        data = pattern.to_bytes()
        assert data == bytes([1, 10, 20])

    def test_two_steps(self):
        s1 = BuzzerStep(frequency_index=5, duration_units=10)
        s2 = BuzzerStep(frequency_index=200, duration_units=50)
        pattern = BuzzerPattern(steps=(s1, s2))
        data = pattern.to_bytes()
        assert data == bytes([2, 5, 10, 200, 50])


class TestBuzzerActivateConfigToBytes:
    def test_single_tone_wire_format(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100, repeats=3)
        data = config.to_bytes()
        # [outer_repeats=3][n_patterns=1][n_steps=1][freq_idx][dur_units]
        assert data[0] == 3  # outer_repeats
        assert data[1] == 1  # 1 pattern
        assert data[2] == 1  # 1 step
        assert len(data) == 5

    def test_silence_tone(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=0, duration_ms=50)
        data = config.to_bytes()
        assert data[2] == 1  # n_steps
        assert data[3] == 0  # frequency_index = 0 (silence)

    def test_repeats_minimum_one(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100, repeats=0)
        assert config.outer_repeats == 1

    def test_default_repeats_is_one(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=440, duration_ms=200)
        assert config.outer_repeats == 1


class TestBuzzerActivateConfigRoundtrip:
    def test_byte_length_matches_structure(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=2000, duration_ms=250)
        data = config.to_bytes()
        # [repeats(1)][n_patterns(1)][n_steps(1)][freq(1)][dur(1)] = 5 bytes
        assert len(data) == 5


class TestNoteToIndex:
    @pytest.mark.parametrize(
        ("name", "index"),
        [
            # 12-TET landmarks from BUZZER_MUSIC_PROTOCOL_REFERENCE.md §4.
            ("A4", 120),  # concert pitch, 440 Hz
            ("C5", 126),
            ("A5", 144),  # exactly one octave (+24) above A4
            ("G5", 140),
            ("G8", 212),
            ("C0", 6),  # the anchor: C0 = _C0_INDEX
            ("B5", 148),
        ],
    )
    def test_landmarks(self, name, index):
        assert note_to_index(name) == index

    def test_octave_is_24_indices(self):
        assert note_to_index("A5") - note_to_index("A4") == 24

    @pytest.mark.parametrize("sharp", ["C#5", "Cs5", "cs5", "CS5"])
    def test_sharp_spellings(self, sharp):
        # C#5 is one semitone (two indices) above C5.
        assert note_to_index(sharp) == 128

    def test_flat_is_enharmonic(self):
        # Bb4 == A#4 == index 122 (flats resolve to the same index as the sharp).
        assert note_to_index("Bb4") == note_to_index("A#4") == note_to_index("As4") == 122

    @pytest.mark.parametrize("quarter", ["A4+", "A4p", "A4P"])
    def test_quarter_tone_marker(self, quarter):
        # A quarter-tone above A4 (120) is the odd index 121.
        assert note_to_index(quarter) == 121

    def test_firmware_enum_spelling(self):
        # Every firmware enum name minus its 'n' prefix parses directly.
        assert note_to_index("As4p") == 123

    @pytest.mark.parametrize("name", ["a4", "A4", "cs5", "REST", "rest", "r"])
    def test_case_insensitive(self, name):
        # Just assert it does not raise; specific values covered elsewhere.
        assert isinstance(note_to_index(name), int)

    @pytest.mark.parametrize("rest", ["R", "r", "REST", "rest", "Rest"])
    def test_rest_is_zero(self, rest):
        assert note_to_index(rest) == 0

    def test_negative_octave(self):
        # A-1 computes to index 0, which collides with the rest sentinel → rejected.
        with pytest.raises(ValueError, match="index 0"):
            note_to_index("A-1")

    def test_index_above_255_rejected(self):
        # B10 computes to 268, outside 1..255 (octave itself is in range).
        with pytest.raises(ValueError, match="outside 1..255"):
            note_to_index("B10")

    def test_octave_out_of_range(self):
        with pytest.raises(ValueError, match="octave 11"):
            note_to_index("A11")

    def test_unknown_letter(self):
        with pytest.raises(ValueError, match="unknown note letter 'H'"):
            note_to_index("H4")

    @pytest.mark.parametrize("garbage", ["", "xyz", "A", "4", "A#", "A4x", "##4"])
    def test_garbage_rejected(self, garbage):
        with pytest.raises(ValueError):
            note_to_index(garbage)


class TestMelody:
    def test_byte_exact_mixed_reference(self):
        # Reference §7.2 (bench T1): note name, rest, raw index — all absolute ms.
        config = BuzzerActivateConfig.melody("A4:200 R:50 144:200")
        assert config.to_bytes() == bytes.fromhex("0101037828000A9028")

    def test_byte_exact_twinkle_explicit(self):
        # Design §9: explicit fractions at tempo 120 (quarter = 500 ms = 100 units).
        config = BuzzerActivateConfig.melody("C5/4 C5/4 G5/4 G5/4 A5/4 A5/4 G5/2", tempo=120)
        assert config.to_bytes() == bytes.fromhex("0101077E647E648C648C64906490648CC8")

    def test_byte_exact_twinkle_default_length(self):
        # Terser default_length=4 form compiles to the identical payload.
        config = BuzzerActivateConfig.melody("C5 C5 G5 G5 A5 A5 G5/2", tempo=120, default_length=4)
        assert config.to_bytes() == bytes.fromhex("0101077E647E648C648C64906490648CC8")

    def test_explicit_and_default_length_are_identical(self):
        explicit = BuzzerActivateConfig.melody("C5/4 C5/4 G5/4 G5/4 A5/4 A5/4 G5/2", tempo=120)
        terse = BuzzerActivateConfig.melody("C5 C5 G5 G5 A5 A5 G5/2", tempo=120, default_length=4)
        assert explicit.to_bytes() == terse.to_bytes()

    def test_absolute_ms_duration(self):
        config = BuzzerActivateConfig.melody("A4:200")
        assert config.patterns[0].steps[0].duration_units == 40

    def test_relative_fraction_duration(self):
        # /4 at 120 BPM = 500 ms = 100 units.
        config = BuzzerActivateConfig.melody("A4/4", tempo=120)
        assert config.patterns[0].steps[0].duration_units == 100

    def test_dotted_duration(self):
        # /4. at 120 BPM = 500 ms × 1.5 = 750 ms = 150 units.
        config = BuzzerActivateConfig.melody("A4/4.", tempo=120)
        assert config.patterns[0].steps[0].duration_units == 150

    def test_triplet_duration(self):
        # /8t at 120 BPM = 250 ms × 2/3 = 166.7 ms → 165 ms = 33 units (design §9).
        config = BuzzerActivateConfig.melody("C5/8t", tempo=120)
        assert config.patterns[0].steps[0].duration_units == 33

    def test_tempo_changes_relative_durations(self):
        # /4 at 60 BPM = 1000 ms = 200 units.
        config = BuzzerActivateConfig.melody("A4/4", tempo=60)
        assert config.patterns[0].steps[0].duration_units == 200

    def test_mixed_duration_modes(self):
        config = BuzzerActivateConfig.melody("A4:200 C5/4 R:50", tempo=120)
        units = [s.duration_units for s in config.patterns[0].steps]
        assert units == [40, 100, 10]

    def test_default_ms_fallback(self):
        # No marker, no default_length → default_ms (200 → 40 units).
        config = BuzzerActivateConfig.melody("A4", default_ms=200)
        assert config.patterns[0].steps[0].duration_units == 40

    def test_default_length_beats_default_ms(self):
        # default_length set → unmarked tokens use it, not default_ms.
        config = BuzzerActivateConfig.melody("A4", tempo=120, default_ms=200, default_length=4)
        assert config.patterns[0].steps[0].duration_units == 100

    def test_explicit_marker_beats_defaults(self):
        config = BuzzerActivateConfig.melody("A4:50", tempo=120, default_ms=200, default_length=4)
        assert config.patterns[0].steps[0].duration_units == 10

    def test_raw_index_and_rest_tokens(self):
        config = BuzzerActivateConfig.melody("0:50 144:200")
        steps = config.patterns[0].steps
        assert steps[0].frequency_index == 0
        assert steps[1].frequency_index == 144

    def test_commas_as_separators(self):
        config = BuzzerActivateConfig.melody("A4:200, R:50, 144:200")
        assert config.to_bytes() == bytes.fromhex("0101037828000A9028")

    def test_repeats_maps_to_outer_repeats(self):
        config = BuzzerActivateConfig.melody("A4:200", repeats=3)
        assert config.outer_repeats == 3
        assert config.to_bytes()[0] == 3

    def test_single_pattern_emitted(self):
        config = BuzzerActivateConfig.melody("A4:200 C5:200")
        assert len(config.patterns) == 1

    def test_sequence_form_equivalence(self):
        string_form = BuzzerActivateConfig.melody("A4:200 R:50 144:200")
        seq_form = BuzzerActivateConfig.melody([("A4", 200), (0, 50), (144, 200)])
        assert string_form.to_bytes() == seq_form.to_bytes()

    def test_sequence_form_note_names_and_indices(self):
        config = BuzzerActivateConfig.melody([("A4", 200), ("R", 50), (144, 200)])
        assert config.to_bytes() == bytes.fromhex("0101037828000A9028")

    # --- error paths -------------------------------------------------------

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            BuzzerActivateConfig.melody("")

    def test_pipe_rejected(self):
        with pytest.raises(ValueError, match="multi-pattern not supported"):
            BuzzerActivateConfig.melody("A4:200 | C5:200")

    def test_absolute_ms_over_limit_rejected(self):
        with pytest.raises(ValueError, match="exceeds 1275 ms"):
            BuzzerActivateConfig.melody("A4:2000")

    def test_tempo_derived_ms_over_limit_rejected(self):
        # /1 at 40 BPM = 6000 ms, over the 1275 ms cap.
        with pytest.raises(ValueError, match="exceeds 1275 ms"):
            BuzzerActivateConfig.melody("A4/1", tempo=40)

    def test_too_many_steps_rejected(self):
        notes = " ".join(["A4:50"] * 121)
        with pytest.raises(ValueError, match="121 steps"):
            BuzzerActivateConfig.melody(notes)

    def test_max_steps_allowed(self):
        notes = " ".join(["A4:50"] * 120)
        config = BuzzerActivateConfig.melody(notes)
        assert len(config.patterns[0].steps) == 120

    def test_dotted_and_triplet_combined_rejected(self):
        with pytest.raises(ValueError, match="cannot be combined"):
            BuzzerActivateConfig.melody("C5/8.t")

    def test_bad_fraction_rejected(self):
        with pytest.raises(ValueError, match="invalid note fraction 3"):
            BuzzerActivateConfig.melody("C5/3")

    def test_raw_index_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="256"):
            BuzzerActivateConfig.melody("256:100")

    def test_error_carries_token_position(self):
        with pytest.raises(ValueError, match=r"token 3 'H4:100': unknown note letter 'H'"):
            BuzzerActivateConfig.melody("A4:100 B4:100 H4:100")

    @pytest.mark.parametrize("tempo", [0, -1])
    def test_non_positive_tempo_rejected(self, tempo):
        with pytest.raises(ValueError, match="tempo must be positive"):
            BuzzerActivateConfig.melody("A4:200", tempo=tempo)

    @pytest.mark.parametrize("repeats", [0, 256])
    def test_repeats_out_of_range_rejected(self, repeats):
        with pytest.raises(ValueError, match="repeats"):
            BuzzerActivateConfig.melody("A4:200", repeats=repeats)

    def test_bad_default_length_rejected(self):
        with pytest.raises(ValueError, match="default_length"):
            BuzzerActivateConfig.melody("A4", default_length=3)

    def test_sequence_form_bad_ms_rejected(self):
        with pytest.raises(ValueError, match="note 1"):
            BuzzerActivateConfig.melody([("A4", 2000)])
