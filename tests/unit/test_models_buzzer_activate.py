"""Tests for BuzzerActivateConfig and helpers."""

from __future__ import annotations

from opendisplay.models.buzzer_activate import (
    BuzzerActivateConfig,
    BuzzerPattern,
    BuzzerStep,
    hz_to_index,
    ms_to_units,
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
