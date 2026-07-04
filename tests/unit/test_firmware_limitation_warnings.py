"""Tests for firmware-limitation warnings on upload prep (C1/C2 mitigations)."""

from __future__ import annotations

import logging

import pytest
from epaper_dithering import ColorScheme

from opendisplay.device import _warn_firmware_upload_limitations


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_bwr_warns_about_lost_color_plane(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.BWR, 800)
    assert any("red/yellow" in m for m in _messages(caplog))


def test_bwy_warns_about_lost_color_plane(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.BWY, 800)
    assert any("red/yellow" in m for m in _messages(caplog))


def test_non_byte_aligned_mono_width_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.MONO, 122)  # 122 % 8 != 0
    assert any("truncate" in m for m in _messages(caplog))


def test_aligned_mono_width_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.MONO, 800)
    assert _messages(caplog) == []


def test_grayscale4_non_aligned_width_is_exempt(caplog: pytest.LogCaptureFixture) -> None:
    # firmware row-pads the 4-gray path, so a non-aligned width is safe there.
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.GRAYSCALE_4, 122)
    assert _messages(caplog) == []
